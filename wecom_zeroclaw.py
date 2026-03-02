#!/usr/bin/env python3
import base64
import hashlib
import logging
import os
import struct
import threading
import time
import xml.etree.ElementTree as ET

import requests
import websocket
import json as json_lib
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


def call_zeroclaw_ws(message: str, session_id: str, from_user: str, chat_id: str):
    """使用 WebSocket 调用 Zeroclaw，支持实时进度更新"""
    logger.info(f"Connecting to Zeroclaw WebSocket for session {session_id}")
    
    # 构建 WebSocket URL（带 session_id 和 token）
    ws_url = ZEROCLAW_WS_URL
    if "?" not in ws_url:
        ws_url += f"?session_id={session_id}"
    else:
        ws_url += f"&session_id={session_id}"
    
    if ZEROCLAW_WEBHOOK_BEARER:
        ws_url += f"&token={ZEROCLAW_WEBHOOK_BEARER}"
    
    full_response = []
    last_update_time = time.time()
    update_interval = 30  # 每 30 秒发送一次进度更新
    
    def on_message(ws, message):
        nonlocal last_update_time
        try:
            data = json_lib.loads(message)
            msg_type = data.get("type")
            
            if msg_type == "content":
                # 收到内容片段
                content = data.get("content", "")
                if content:
                    full_response.append(content)
                    
                    # 定期发送进度更新
                    current_time = time.time()
                    if current_time - last_update_time > update_interval:
                        progress = "".join(full_response)
                        if len(progress) > 100:
                            preview = progress[:100] + "..."
                            try:
                                send_wecom_text(from_user, chat_id, f"[处理中] {preview}")
                                last_update_time = current_time
                            except:
                                pass
            
            elif msg_type == "tool_use":
                # Agent 正在使用工具
                tool_name = data.get("tool", "unknown")
                logger.info(f"Agent using tool: {tool_name}")
                
            elif msg_type == "error":
                # 错误消息
                error_msg = data.get("message", "Unknown error")
                logger.error(f"WebSocket error: {error_msg}")
                full_response.append(f"\n[错误: {error_msg}]")
            
            elif msg_type == "done":
                # 完成
                logger.info(f"WebSocket conversation completed for session {session_id}")
                
        except json_lib.JSONDecodeError as e:
            logger.warning(f"Failed to parse WebSocket message: {e}")
    
    def on_error(ws, error):
        logger.error(f"WebSocket error: {error}")
    
    def on_close(ws, close_status_code, close_msg):
        logger.info(f"WebSocket closed: {close_status_code} - {close_msg}")
    
    def on_open(ws):
        logger.info(f"WebSocket connected for session {session_id}")
        # 发送消息
        ws.send(message)
    
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
        ws.run_forever(ping_interval=20, ping_timeout=10)
        
        # 返回完整响应
        response = "".join(full_response).strip()
        if not response:
            response = "处理完成，但未收到响应。"
        
        logger.info(f"Zeroclaw WebSocket response received for session {session_id}")
        return response
        
    except Exception as e:
        logger.error(f"WebSocket connection failed: {e}")
        raise


def send_wecom_text(user_id: str, chat_id: str, content: str):
    """发送企业微信文本消息"""
    try:
        token = get_access_token()
        if chat_id:
            url = f"https://qyapi.weixin.qq.com/cgi-bin/appchat/send?access_token={token}"
            payload = {
                "chatid": chat_id,
                "msgtype": "text",
                "text": {"content": content},
            }
        else:
            url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
            payload = {
                "touser": user_id,
                "msgtype": "text",
                "agentid": int(WECOM_AGENT_ID),
                "text": {"content": content},
            }

        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") != 0:
            logger.error(f"WeCom send failed: {data}")
            raise RuntimeError(f"send failed: {data}")
        logger.info(f"Message sent to user={user_id}, chat={chat_id}")
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
        
        if not reply:
            reply = "处理完成。"
        
        # 发送最终结果
        send_wecom_text(from_user, chat_id, reply)
        
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
