# Zeroclaw 通信架构分析

## 核心发现

通过深入分析 zeroclaw 源码，发现了以下关键事实：

### 1. 所有同步端点本质相同

| 端点 | 实现 | 本质 |
|------|------|------|
| `POST /api/chat` | `run_gateway_chat_with_tools().await` | 同步等待 |
| `POST /v1/chat/completions` | `run_gateway_chat_with_tools().await` | 同步等待 |
| `GET /ws/chat` | `run_gateway_chat_with_tools().await` | 同步等待 |

**区别只有：**
- HTTP: 可配置超时（默认 120 秒）
- WebSocket: 长连接，更稳定，但仍然同步等待

**中间过程：** ❌ 不透明

### 2. SSE 是唯一的进度透明方案

`GET /api/events` - Server-Sent Events 实时广播：

```rust
// 来自 src/gateway/sse.rs
observer.record_event(AgentStart { ... })
observer.record_event(ToolCallStart { tool: "web_fetch" })
observer.record_event(ToolCall { tool: "web_fetch", duration_ms: 4523, success: true })
observer.record_event(AgentEnd { ... })
```

**特点：**
- ✅ 实时事件流
- ✅ 工具级别透明
- ⚠️ 只能观察，不能发起任务

### 3. zeroclaw 没有真正的异步任务 API

**不存在：**
- ❌ 后台任务队列
- ❌ 任务状态查询 API
- ❌ 异步任务 ID 返回

**所有端点都是同步阻塞的。**

## 最优方案：WebSocket + SSE 双通道

### 架构图

```
┌─────────────────────────────────────────────────────────┐
│                    wecom_zeroclaw.py                    │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────────┐              ┌──────────────┐       │
│  │ 通道 1: SSE  │              │ 通道 2: WS   │       │
│  │ (监听进度)   │              │ (发起任务)   │       │
│  └──────┬───────┘              └──────┬───────┘       │
│         │                             │               │
└─────────┼─────────────────────────────┼───────────────┘
          │                             │
          │                             │
          ▼                             ▼
┌─────────────────────────────────────────────────────────┐
│                    zeroclaw gateway                     │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  GET /api/events        POST /ws/chat                  │
│       │                        │                        │
│       │  ┌─────────────────────┘                       │
│       │  │                                              │
│       │  │  run_gateway_chat_with_tools()              │
│       │  │           │                                  │
│       │  │           ├─> tool: web_fetch               │
│       ▼  │           │                                  │
│   broadcast_event()  ├─> tool: web_search              │
│       │              │                                  │
│       │              ├─> tool: read_file               │
│       │              │                                  │
│       │              └─> return final_response          │
│       │                         │                       │
│       └─────────────────────────┘                       │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 时序图

```
用户          wecom_zeroclaw.py          zeroclaw
 │                   │                      │
 ├─ 发送消息 ────────>│                      │
 │                   │                      │
 │<─ "正在思考..." ───┤                      │
 │                   │                      │
 │                   ├─ SSE 连接 ─────────>│
 │                   │                      │
 │                   ├─ WS 连接 ──────────>│
 │                   │                      │
 │                   │<─ {"type":"history"} ┤
 │                   │                      │
 │                   │      [Agent 开始工作] │
 │                   │<─ SSE: agent_start ──┤
 │                   │                      │
 │                   │      [调用 web_fetch] │
 │                   │<─ SSE: tool_call_start
 │<─ "🔧 web_fetch" ──┤                      │
 │                   │                      │
 │                   │      [web_fetch 完成] │
 │                   │<─ SSE: tool_call ────┤
 │<─ "✅ 完成 4523ms" ┤                      │
 │                   │                      │
 │                   │      [调用 web_search]│
 │                   │<─ SSE: tool_call_start
 │<─ "🔧 web_search" ─┤                      │
 │                   │                      │
 │                   │      [web_search 完成]│
 │                   │<─ SSE: tool_call ────┤
 │<─ "✅ 完成 7891ms" ┤                      │
 │                   │                      │
 │                   │      [Agent 完成工作] │
 │                   │<─ SSE: agent_end ────┤
 │                   │                      │
 │                   │<─ WS: {"type":"done",│
 │                   │    "full_response":..}
 │                   │                      │
 │<─ "完整报告..." ────┤                      │
 │                   │                      │
```

### 代码实现

```python
def call_zeroclaw_ws(message, session_id, from_user, chat_id):
    # 1. 启动 SSE 监听线程
    sse_stop_event = threading.Event()
    sse_thread = threading.Thread(
        target=listen_sse_progress,
        args=(from_user, chat_id, sse_stop_event),
        daemon=True
    )
    sse_thread.start()
    
    # 2. 建立 WebSocket 连接（同步等待结果）
    ws = websocket.WebSocketApp(ws_url, ...)
    ws.run_forever()
    
    # 3. 停止 SSE 监听
    sse_stop_event.set()
    
    return final_response

def listen_sse_progress(from_user, chat_id, stop_event):
    # 订阅 SSE 事件流
    response = requests.get("http://127.0.0.1:42617/api/events", stream=True)
    client = sseclient.SSEClient(response)
    
    for event in client.events():
        if stop_event.is_set():
            break
        
        data = json.loads(event.data)
        
        if data["type"] == "tool_call_start":
            send_wecom_text(from_user, chat_id, f"🔧 {data['tool']}")
        
        elif data["type"] == "tool_call":
            send_wecom_text(from_user, chat_id, f"✅ {data['tool']} ({data['duration_ms']}ms)")
        
        elif data["type"] == "agent_end":
            break
```

## 为什么不能只用一个通道？

### 只用 HTTP/WebSocket？
- ❌ 中间过程不透明
- ❌ 用户只能等待
- ❌ 无法知道 AI 在做什么

### 只用 SSE？
- ❌ SSE 只能观察，不能发起任务
- ❌ 需要另一个通道发送消息

### 为什么必须双通道？
- ✅ WebSocket 发起任务并等待最终结果
- ✅ SSE 实时监听进度事件
- ✅ 两者配合实现完全透明的进度更新

## 用户体验对比

### 单通道（HTTP 或 WebSocket）
```
用户: "研究 zeroclaw vs openclaw"
AI:   "收到您的问题，AI 正在思考中..."
      [等待 2-5 分钟...]
AI:   "完整报告：..."
```

### 双通道（WebSocket + SSE）
```
用户: "研究 zeroclaw vs openclaw"
AI:   "收到您的问题，AI 正在思考中..."
      [5秒] "🔧 正在使用工具: web_fetch"
      [10秒] "✅ web_fetch 完成 (4523ms)"
      [12秒] "🔧 正在使用工具: web_search_tool"
      [20秒] "✅ web_search_tool 完成 (7891ms)"
      [25秒] "🔧 正在使用工具: read_file"
      [27秒] "✅ read_file 完成 (1456ms)"
      ...
      [120秒] "完整报告：..."
```

## 总结

1. **HTTP 和 WebSocket 本质相同** - 都是同步等待，只有超时时间不同
2. **SSE 是唯一的进度透明方案** - 实时广播工具调用事件
3. **双通道是必需的** - WebSocket 发起任务，SSE 监听进度
4. **zeroclaw 没有异步任务 API** - 所有端点都是同步阻塞的

这就是为什么 `wecom_zeroclaw.py` 必须使用 **WebSocket + SSE 双通道架构**。
