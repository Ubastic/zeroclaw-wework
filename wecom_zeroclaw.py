#!/usr/bin/env python3
import base64
import hashlib
import logging
import os
import struct
import threading
import time
import xml.etree.ElementTree as ET
from typing import List

import requests
import websocket
import json as json_lib
import sseclient
from flask import Flask, request, make_response
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from dotenv import load_dotenv
load_dotenv()  # 默认读取当前目录 .env

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('wecom_bridge.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# WebSocket URL for zeroclaw
ZEROCLAW_WS_URL = os.getenv("ZEROCLAW_WS_URL", "ws://127.0.0.1:42617/ws/chat").strip()
ZEROCLAW_WEBHOOK_BEARER = os.getenv("ZEROCLAW_WEBHOOK_BEARER", "").strip()

# Fallback HTTP URL (not used with WebSocket)
ZEROCLAW_WEBHOOK_URL = os.getenv("ZEROCLAW_WEBHOOK_URL", "http://127.0.0.1:42617/api/chat").strip()
ZEROCLAW_WEBHOOK_SECRET = os.getenv("ZEROCLAW_WEBHOOK_SECRET", "").strip()

WECOM_TOKEN = os.getenv("WECOM_TOKEN", "").strip()
WECOM_AES_KEY = os.getenv("WECOM_AES_KEY", "").strip()
WECOM_CORP_ID = os.getenv("WECOM_CORP_ID", "").strip()
WECOM_AGENT_ID = os.getenv("WECOM_AGENT_ID", "").strip()
WECOM_APP_SECRET = os.getenv("WECOM_APP_SECRET", "").strip()

LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8080"))

# 消息分割阈值（字节）
# 企业微信客户端：2048 字节
# 微信端（企业微信插件）：约 600-800 字节（实测）
# 默认：800 字节（兼顾微信端和企业微信端）
WECOM_MAX_MESSAGE_BYTES = int(os.getenv("WECOM_MAX_MESSAGE_BYTES", "800"))

ALLOWED_USERS = {
    u.strip()
    for u in os.getenv("WECOM_ALLOWED_USERS", "").split(",")
    if u.strip()
}

_token_cache = {"token": None, "expires_at": 0}


def ensure_config():
    missing = []
    for name, value in {
        "WECOM_TOKEN": WECOM_TOKEN,
        "WECOM_AES_KEY": WECOM_AES_KEY,
        "WECOM_CORP_ID": WECOM_CORP_ID,
        "WECOM_AGENT_ID": WECOM_AGENT_ID,
        "WECOM_APP_SECRET": WECOM_APP_SECRET,
    }.items():
        if not value:
            missing.append(name)
    if missing:
        raise RuntimeError("Missing env: " + ", ".join(missing))


def is_allowed_user(user_id: str) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


def sha1_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    items = [token, timestamp, nonce, encrypt]
    items.sort()
    raw = "".join(items)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def verify_signature(signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
    if not all([signature, timestamp, nonce, encrypt]):
        return False
    expected = sha1_signature(WECOM_TOKEN, timestamp, nonce, encrypt)
    return expected == signature


def aes_key_bytes() -> bytes:
    key = WECOM_AES_KEY
    if len(key) == 43:
        key += "="
    return base64.b64decode(key)


def decrypt_xml(encrypted_b64: str) -> str:
    """
    企业微信 AES 解密
    格式：random(16B) + msg_len(4B) + msg + receiveid
    """
    key = aes_key_bytes()
    iv = key[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)

    encrypted = base64.b64decode(encrypted_b64)
    plain = cipher.decrypt(encrypted)
    
    # 企业微信使用 PKCS7 padding，手动去除
    # padding 值是最后一个字节的值，表示 padding 的长度
    pad_len = plain[-1]
    if not isinstance(pad_len, int):
        pad_len = ord(plain[-1])
    
    # 验证 padding 是否有效
    if pad_len < 1 or pad_len > 32:
        logger.error(f"Invalid padding length: {pad_len}, data length: {len(plain)}")
        raise ValueError(f"Invalid padding: {pad_len}")
    
    # 验证 padding 字节是否一致
    padding = plain[-pad_len:]
    if not all(b == pad_len if isinstance(b, int) else ord(b) == pad_len for b in padding):
        logger.error(f"Inconsistent padding bytes, expected all {pad_len}")
        raise ValueError("Padding is incorrect")
    
    # 去除 padding
    plain = plain[:-pad_len]

    # 解析消息结构：16字节随机 + 4字节长度 + 消息 + ReceiveId
    if len(plain) < 20:
        raise ValueError(f"Decrypted data too short: {len(plain)} bytes")
    
    msg_len = struct.unpack(">I", plain[16:20])[0]
    
    if len(plain) < 20 + msg_len:
        raise ValueError(f"Message length mismatch: expected {msg_len}, available {len(plain) - 20}")
    
    xml_bytes = plain[20:20 + msg_len]
    corp_id = plain[20 + msg_len:].decode("utf-8", errors="ignore").rstrip('\x00')

    if corp_id != WECOM_CORP_ID:
        logger.error(f"CorpId mismatch: expected {WECOM_CORP_ID}, got {corp_id}")
        raise ValueError(f"corp id mismatch: {corp_id}")
    
    return xml_bytes.decode("utf-8")


def parse_xml(xml_str: str) -> dict:
    root = ET.fromstring(xml_str)
    return {child.tag: (child.text or "") for child in root}


def get_access_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    resp = requests.get(
        url,
        params={"corpid": WECOM_CORP_ID, "corpsecret": WECOM_APP_SECRET},
        timeout=10,
    )
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"gettoken failed: {data}")
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 7200))
    return _token_cache["token"]


def listen_sse_progress(from_user: str, chat_id: str, stop_event: threading.Event):
    """监听 SSE 事件流，实时推送进度"""
    sse_url = "http://127.0.0.1:42617/api/events"
    headers = {}
    if ZEROCLAW_WEBHOOK_BEARER:
        headers["Authorization"] = f"Bearer {ZEROCLAW_WEBHOOK_BEARER}"
    
    try:
        logger.info("Starting SSE listener for progress updates")
        response = requests.get(sse_url, headers=headers, stream=True, timeout=None)
        client = sseclient.SSEClient(response)
        
        for event in client.events():
            if stop_event.is_set():
                break
            
            try:
                data = json_lib.loads(event.data)
                event_type = data.get("type")
                
                if event_type == "agent_start":
                    logger.info("Agent started")
                    
                elif event_type == "tool_call_start":
                    tool = data.get("tool", "unknown")
                    logger.info(f"Tool started: {tool}")
                    try:
                        send_wecom_text(from_user, chat_id, f"🔧 正在使用工具: {tool}")
                    except:
                        pass
                    
                elif event_type == "tool_call":
                    tool = data.get("tool", "unknown")
                    duration_ms = data.get("duration_ms", 0)
                    success = data.get("success", True)
                    status = "✅" if success else "❌"
                    logger.info(f"Tool completed: {tool} ({duration_ms}ms, success={success})")
                    try:
                        send_wecom_text(
                            from_user, 
                            chat_id, 
                            f"{status} {tool} 完成 ({duration_ms}ms)"
                        )
                    except:
                        pass
                    
                elif event_type == "agent_end":
                    logger.info("Agent completed")
                    stop_event.set()
                    break
                    
                elif event_type == "error":
                    error_msg = data.get("message", "Unknown error")
                    logger.error(f"SSE error event: {error_msg}")
                    
            except json_lib.JSONDecodeError:
                logger.debug(f"Non-JSON SSE event: {event.data[:100]}")
                
    except Exception as e:
        logger.error(f"SSE listener error: {e}")
    finally:
        logger.info("SSE listener stopped")


def call_zeroclaw_ws(message: str, session_id: str, from_user: str, chat_id: str):
    """使用 WebSocket 调用 Zeroclaw，配合 SSE 实时进度更新"""
    logger.info(f"Connecting to Zeroclaw WebSocket for session {session_id}")
    
    # 构建 WebSocket URL（带 session_id 和 token）
    ws_url = ZEROCLAW_WS_URL
    if "?" not in ws_url:
        ws_url += f"?session_id={session_id}"
    else:
        ws_url += f"&session_id={session_id}"
    
    if ZEROCLAW_WEBHOOK_BEARER:
        ws_url += f"&token={ZEROCLAW_WEBHOOK_BEARER}"
    
    # 启动 SSE 监听线程
    sse_stop_event = threading.Event()
    sse_thread = threading.Thread(
        target=listen_sse_progress,
        args=(from_user, chat_id, sse_stop_event),
        daemon=True
    )
    sse_thread.start()
    
    full_response = []
    
    def on_message(ws, message):
        try:
            # zeroclaw WebSocket 只发送 JSON 消息
            data = json_lib.loads(message)
            msg_type = data.get("type")
            logger.debug(f"Received WebSocket message type: {msg_type}")
            
            if msg_type == "history":
                # 连接建立时的历史记录，忽略
                logger.debug(f"Received history for session {session_id}")
                
            elif msg_type == "done":
                # 处理完成，包含完整响应
                logger.info(f"WebSocket conversation completed for session {session_id}")
                final_response = data.get("full_response", "").strip()
                logger.info(f"Received full_response length: {len(final_response)} chars")
                if final_response:
                    full_response.clear()
                    full_response.append(final_response)
                    logger.info(f"Stored response: {final_response[:100]}...")
                else:
                    logger.warning("Received done message without full_response")
                    logger.debug(f"Done message data: {data}")
                
                # 主动关闭 WebSocket 连接
                logger.info("Closing WebSocket connection after receiving done message")
                ws.close()
                
            elif msg_type == "error":
                # 错误消息
                error_msg = data.get("message", "Unknown error")
                logger.error(f"WebSocket error from zeroclaw:")
                logger.error(f"Error message field: {error_msg}")
                logger.error(f"Complete error data (all fields): {json_lib.dumps(data, ensure_ascii=False)}")
                # 尝试获取其他可能包含完整错误的字段
                for key in data.keys():
                    if key != "type" and key != "message":
                        logger.error(f"Additional field '{key}': {data[key]}")
                full_response.append(f"处理出错：{error_msg}")
                
            else:
                # 未知消息类型
                logger.warning(f"Received unknown message type: {msg_type}")
                logger.debug(f"Message data: {data}")
                
        except json_lib.JSONDecodeError as e:
            logger.error(f"Failed to parse WebSocket message as JSON: {e}")
            logger.debug(f"Raw message: {message[:200]}")
    
    def on_error(ws, error):
        logger.error(f"WebSocket error: {error}")
    
    def on_close(ws, close_status_code, close_msg):
        logger.info(f"WebSocket closed: {close_status_code} - {close_msg}")
    
    def on_open(ws):
        logger.info(f"WebSocket connected for session {session_id}")
        # 发送消息（zeroclaw 要求 JSON 格式）
        payload = json_lib.dumps({
            "type": "message",
            "content": message
        })
        ws.send(payload)
    
    try:
        # 创建 WebSocket 连接
        ws = websocket.WebSocketApp(
            ws_url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            on_open=on_open
        )
        
        # 运行 WebSocket（阻塞直到连接关闭）
        # ping_interval: 每 60 秒发送一次 ping
        # ping_timeout: 等待 pong 的超时时间 30 秒
        ws.run_forever(ping_interval=60, ping_timeout=30)
        
        # 停止 SSE 监听
        sse_stop_event.set()
        sse_thread.join(timeout=2)
        
        # 返回完整响应
        response = "".join(full_response).strip()
        logger.info(f"Final response length: {len(response)} chars")
        logger.info(f"Final response preview: {response[:200]}...")
        
        if not response:
            response = "处理完成，但未收到响应。"
            logger.warning("Empty response from zeroclaw")
        
        logger.info(f"Zeroclaw WebSocket response received for session {session_id}")
        return response
        
    except Exception as e:
        # 停止 SSE 监听
        sse_stop_event.set()
        logger.error(f"WebSocket connection failed: {e}")
        raise


def split_message_by_bytes(content: str, max_bytes: int = None) -> List[str]:
    """
    按字节数分割消息，确保每段不超过指定字节数。
    
    限制说明：
    - 企业微信客户端：2048 字节
    - 微信端（企业微信插件）：约 600-800 字节（实测）
    - 默认：800 字节（约 266 个中文字符）
    
    Args:
        content: 要分割的消息内容
        max_bytes: 每段最大字节数（None 则使用环境变量 WECOM_MAX_MESSAGE_BYTES）
    
    Returns:
        分割后的消息列表
    """
    if max_bytes is None:
        max_bytes = WECOM_MAX_MESSAGE_BYTES
    
    if not content:
        return [""]
    
    # 检查是否需要分割
    content_bytes = content.encode('utf-8')
    if len(content_bytes) <= max_bytes:
        return [content]
    
    chunks = []
    current_chunk = ""
    current_bytes = 0
    
    # 按行分割，保持内容完整性
    lines = content.split('\n')
    
    for i, line in enumerate(lines):
        line_with_newline = line + ('\n' if i < len(lines) - 1 else '')
        line_bytes = line_with_newline.encode('utf-8')
        line_byte_len = len(line_bytes)
        
        # 如果单行就超过限制，需要按字符分割
        if line_byte_len > max_bytes:
            # 先保存当前块
            if current_chunk:
                chunks.append(current_chunk.rstrip('\n'))
                current_chunk = ""
                current_bytes = 0
            
            # 按字符分割超长行
            temp_line = ""
            temp_bytes = 0
            for char in line:
                char_bytes = char.encode('utf-8')
                char_byte_len = len(char_bytes)
                
                if temp_bytes + char_byte_len > max_bytes:
                    chunks.append(temp_line)
                    temp_line = char
                    temp_bytes = char_byte_len
                else:
                    temp_line += char
                    temp_bytes += char_byte_len
            
            if temp_line:
                current_chunk = temp_line + ('\n' if i < len(lines) - 1 else '')
                current_bytes = len(current_chunk.encode('utf-8'))
        
        # 如果加上这行会超过限制，先保存当前块
        elif current_bytes + line_byte_len > max_bytes:
            if current_chunk:
                chunks.append(current_chunk.rstrip('\n'))
            current_chunk = line_with_newline
            current_bytes = line_byte_len
        
        # 否则追加到当前块
        else:
            current_chunk += line_with_newline
            current_bytes += line_byte_len
    
    # 保存最后一块
    if current_chunk:
        chunks.append(current_chunk.rstrip('\n'))
    
    return chunks if chunks else [""]


def send_wecom_text(user_id: str, chat_id: str, content: str):
    """
    发送企业微信文本消息，自动处理超长消息分割。
    企业微信限制：单条消息最大 2048 字节。
    """
    try:
        token = get_access_token()
        
        # 分割消息
        chunks = split_message_by_bytes(content)
        total_chunks = len(chunks)
        
        # 发送每一段
        for idx, chunk in enumerate(chunks, 1):
            # 如果消息被分割，添加序号标记
            if total_chunks > 1:
                chunk_with_marker = f"[{idx}/{total_chunks}]\n{chunk}"
            else:
                chunk_with_marker = chunk
            
            # 构建请求
            if chat_id:
                url = f"https://qyapi.weixin.qq.com/cgi-bin/appchat/send?access_token={token}"
                payload = {
                    "chatid": chat_id,
                    "msgtype": "text",
                    "text": {"content": chunk_with_marker},
                }
            else:
                url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
                payload = {
                    "touser": user_id,
                    "msgtype": "text",
                    "agentid": int(WECOM_AGENT_ID),
                    "text": {"content": chunk_with_marker},
                }
            
            # 发送请求
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            if data.get("errcode") != 0:
                logger.error(f"WeCom send failed (chunk {idx}/{total_chunks}): {data}")
                raise RuntimeError(f"send failed: {data}")
            
            # 多段消息之间添加短暂延迟，避免消息顺序混乱
            if idx < total_chunks:
                time.sleep(0.3)
        
        logger.info(f"Message sent to user={user_id}, chat={chat_id} ({total_chunks} chunk(s))")
    except Exception as e:
        logger.error(f"Failed to send WeCom message: {e}")
        raise


def async_process_message(from_user: str, chat_id: str, content: str, session_id: str):
    """异步处理消息，使用 WebSocket 支持实时进度更新"""
    try:
        logger.info(f"Processing message from {from_user}: {content[:50]}...")
        
        # 发送初始提示
        try:
            send_wecom_text(from_user, chat_id, "收到您的问题，AI 正在思考中...")
        except:
            logger.warning("Failed to send initial message")
        
        # 使用 WebSocket 调用 zeroclaw
        reply = call_zeroclaw_ws(content, session_id, from_user, chat_id)
        
        logger.info(f"Got reply from zeroclaw: {len(reply)} chars")
        
        if not reply:
            reply = "处理完成。"
            logger.warning("Empty reply from call_zeroclaw_ws")
        
        # 发送最终结果
        logger.info(f"Sending final reply to user {from_user}")
        send_wecom_text(from_user, chat_id, reply)
        logger.info(f"Final reply sent successfully")
        
    except Exception as e:
        logger.error(f"Async message processing failed: {e}", exc_info=True)
        try:
            send_wecom_text(
                from_user, 
                chat_id, 
                f"抱歉，处理消息时出现错误：{str(e)[:100]}\n请稍后重试或简化问题。"
            )
        except:
            logger.error("Failed to send error message to user")


@app.route("/wecom/callback", methods=["GET", "POST"])
def wecom_callback():
    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")

    if request.method == "GET":
        echostr = request.args.get("echostr", "")
        logger.info("Received WeCom verification request")
        if not verify_signature(msg_signature, timestamp, nonce, echostr):
            logger.warning("Invalid signature in verification request")
            return make_response("invalid signature", 401)
        try:
            plain = decrypt_xml(echostr)
            logger.info("WeCom verification successful")
            return make_response(plain, 200)
        except Exception as e:
            logger.error(f"Decryption failed during verification: {e}")
            return make_response("decrypt failed", 400)

    # POST
    try:
        outer = parse_xml(request.data.decode("utf-8"))
        encrypt = outer.get("Encrypt", "")
        if not verify_signature(msg_signature, timestamp, nonce, encrypt):
            logger.warning("Invalid signature in message callback")
            return make_response("invalid signature", 401)

        decrypted_xml = decrypt_xml(encrypt)
        msg = parse_xml(decrypted_xml)

        msg_type = msg.get("MsgType", "")
        if msg_type != "text":
            logger.debug(f"Ignoring non-text message type: {msg_type}")

        from_user = msg.get("FromUserName", "")
        chat_id = msg.get("ChatId", "")
        content = msg.get("Content", "").strip()

        if not from_user or not content:
            logger.debug("Empty user or content, ignoring")
            return make_response("success", 200)

        if not is_allowed_user(from_user):
            logger.warning(f"User {from_user} not in allowed list")
            return make_response("success", 200)

        session_id = f"wecom_{chat_id or from_user}"
        
        # 异步处理消息，立即返回避免企业微信 5 秒超时
        thread = threading.Thread(
            target=async_process_message,
            args=(from_user, chat_id, content, session_id),
            daemon=True
        )
        thread.start()
        
        logger.info(f"Message queued for async processing: session={session_id}")
        return make_response("success", 200)
    except Exception as e:
        logger.error(f"Error processing callback: {e}", exc_info=True)
        return make_response("success", 200)


def verify_zeroclaw_webhook():
    """验证 Zeroclaw webhook URL 是否可访问"""
    try:
        logger.info(f"Verifying Zeroclaw webhook URL: {ZEROCLAW_WEBHOOK_URL}")
        # 尝试访问 health endpoint 或直接访问 webhook URL
        base_url = ZEROCLAW_WEBHOOK_URL.rsplit('/webhook', 1)[0] if '/webhook' in ZEROCLAW_WEBHOOK_URL else ZEROCLAW_WEBHOOK_URL
        
        # 尝试多个可能的健康检查端点
        for endpoint in ['/health', '/status', '']:
            try:
                test_url = base_url + endpoint
                resp = requests.get(test_url, timeout=5)
                if resp.status_code < 500:  # 任何非服务器错误都算可访问
                    logger.info(f"Zeroclaw endpoint accessible at {test_url}")
                    return True
            except:
                continue
        
        logger.warning(f"Could not verify Zeroclaw webhook at {ZEROCLAW_WEBHOOK_URL}")
        logger.warning("Please ensure Zeroclaw is running. The bridge will continue but may fail on message processing.")
        return False
    except Exception as e:
        logger.warning(f"Webhook verification error: {e}")
        return False


if __name__ == "__main__":
    logger.info("Starting WeCom-Zeroclaw Bridge...")
    ensure_config()
    verify_zeroclaw_webhook()
    logger.info(f"Listening on {LISTEN_HOST}:{LISTEN_PORT}")
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
