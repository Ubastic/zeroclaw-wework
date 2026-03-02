# WeCom-Zeroclaw Bridge (WebSocket + SSE 双通道版本)

企业微信与 zeroclaw 的集成桥接，使用 **WebSocket + SSE 双通道**实现真正的实时进度更新。

## 功能特性

- ✅ **真正的实时进度更新** - 通过 SSE 接收工具调用进度
- ✅ **长时间任务支持** - WebSocket 长连接，无超时限制
- ✅ **工具级别透明** - 实时显示每个工具的调用和完成状态
- ✅ **异步处理** - 不阻塞企业微信回调
- ✅ **完整的错误处理** - 友好的错误提示

## 工作流程（双通道架构）

```
用户发送消息
  ↓
企业微信 → wecom_zeroclaw.py
  ↓
立即回复："收到您的问题，AI 正在思考中..."
  ↓
【通道 1】启动 SSE 监听线程（监听进度事件）
  ↓
【通道 2】建立 WebSocket 连接（发起任务并等待结果）
  ↓
SSE 实时接收事件：
  - agent_start
  - tool_call_start: web_fetch → "🔧 正在使用工具: web_fetch"
  - tool_call: web_fetch (完成) → "✅ web_fetch 完成 (4523ms)"
  - tool_call_start: git_operations
  - tool_call: git_operations (完成)
  - agent_end
  ↓
WebSocket 返回最终结果
  ↓
发送完整回复给用户
```

## 安装依赖

```bash
pip install -r requirements.txt
```

## 配置

复制 `.env.example` 为 `.env` 并填写配置：

```bash
cp .env.example .env
```

### 关键配置项

```env
# Zeroclaw WebSocket URL
ZEROCLAW_WS_URL=ws://127.0.0.1:42617/ws/chat

# 如果启用了认证，填写 Bearer Token
ZEROCLAW_WEBHOOK_BEARER=

# 企业微信配置
WECOM_TOKEN=your_token
WECOM_AES_KEY=your_aes_key
WECOM_CORP_ID=your_corp_id
WECOM_AGENT_ID=your_agent_id
WECOM_APP_SECRET=your_app_secret
```

## Zeroclaw 配置

确保 zeroclaw 配置文件 (`~/.config/zeroclaw/config.toml`) 包含：

```toml
[gateway]
require_pairing = false  # 或配置 Bearer Token
host = "127.0.0.1"
port = 42617
request_timeout_secs = 600  # 10 分钟，适合超大任务

[mcp]
enabled = true

# 添加 git 支持（可选，用于 clone 仓库）
[[mcp.servers]]
name = "git"
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-git"]
tool_timeout_secs = 300

[subagents]
enabled = true
max_concurrent = 5  # 允许最多 5 个并发 Agent
auto_activate = true
```

## 运行

```bash
python wecom_zeroclaw.py
```

## 用户体验

### 简单问题
```
用户: "今天天气怎么样？"
AI:   "收到您的问题，AI 正在思考中..."
      [2秒后]
AI:   "🔧 正在使用工具: web_search_tool"
      [3秒后]
AI:   "✅ web_search_tool 完成 (1234ms)"
      [5秒后]
AI:   "今天北京天气晴朗，温度 15-25°C..."
```

### 复杂任务（真实进度透明）
```
用户: "帮我研究 zeroclaw vs openclaw 的区别"
AI:   "收到您的问题，AI 正在思考中..."
      [5秒后]
AI:   "🔧 正在使用工具: web_fetch"
      [10秒后]
AI:   "✅ web_fetch 完成 (4523ms)"
      [12秒后]
AI:   "🔧 正在使用工具: web_search_tool"
      [20秒后]
AI:   "✅ web_search_tool 完成 (7891ms)"
      [25秒后]
AI:   "🔧 正在使用工具: read_file"
      [27秒后]
AI:   "✅ read_file 完成 (1456ms)"
      [30秒后]
AI:   "🔧 正在使用工具: grep_search"
      [35秒后]
AI:   "✅ grep_search 完成 (4234ms)"
      [120秒后]
AI:   "完整的对比报告：
            1. zeroclaw 是 100% Rust 实现...
            2. openclaw 是 Python/TypeScript...
            3. 主要差异：..."
```

## 技术架构

### WebSocket 协议（任务通道）

zeroclaw WebSocket 发送的消息类型：

```json
// 连接建立时的历史记录
{"type": "history", "session_id": "...", "messages": [...]}

// 处理完成（包含完整响应）
{"type": "done", "full_response": "完整的回复内容"}

// 错误
{"type": "error", "message": "错误描述"}
```

客户端发送格式：
```json
{"type": "message", "content": "用户的问题"}
```

### SSE 事件流（进度通道）

zeroclaw SSE (`GET /api/events`) 实时推送的事件：

```json
// Agent 开始工作
{"type": "agent_start", "provider": "openai", "model": "gpt-4"}

// 工具调用开始
{"type": "tool_call_start", "tool": "web_fetch", "timestamp": "..."}

// 工具调用完成
{"type": "tool_call", "tool": "web_fetch", "duration_ms": 4523, "success": true}

// Agent 完成工作
{"type": "agent_end", "provider": "openai", "model": "gpt-4", "duration_ms": 120000}

// 错误
{"type": "error", "component": "...", "message": "..."}
```

## 故障排除

### WebSocket 连接失败

1. 检查 zeroclaw 是否运行：
   ```bash
   curl http://127.0.0.1:42617/health
   ```

2. 检查 WebSocket 端点：
   ```bash
   wscat -c ws://127.0.0.1:42617/ws/chat
   ```

3. 查看日志：
   ```bash
   tail -f wecom_bridge.log
   ```

### 认证问题

如果 zeroclaw 启用了 pairing：

1. 获取 pairing code（启动 zeroclaw 时显示）
2. 配对获取 token：
   ```bash
   curl -X POST http://127.0.0.1:42617/pair \
     -H "X-Pairing-Code: <配对码>"
   ```
3. 将返回的 token 填入 `.env`：
   ```env
   ZEROCLAW_WEBHOOK_BEARER=zc_xxxxxxxxxxxxx
   ```

## 性能优化

- **进度更新间隔**：默认 30 秒，可在代码中调整 `update_interval`
- **WebSocket ping**：默认 20 秒，保持连接活跃
- **超大任务**：zeroclaw 配置 `request_timeout_secs = 600`（10 分钟）

## 与其他方案的对比

| 特性 | HTTP 单通道 | WebSocket 单通道 | **WebSocket + SSE 双通道** |
|------|-------------|------------------|---------------------------|
| 实时进度 | ❌ | ❌ | ✅ **工具级别透明** |
| 长任务支持 | ⚠️ 120秒超时 | ✅ 无超时 | ✅ 无超时 |
| 用户体验 | 长时间等待 | 长时间等待 | **实时反馈每个工具** |
| 进度粒度 | 无 | 无 | **每个工具调用** |
| 实现复杂度 | 简单 | 简单 | 中等 |
| 资源占用 | 低 | 中 | 稍高（双连接） |

### 核心差异

**HTTP/WebSocket 单通道的本质：**
- 都调用 `run_gateway_chat_with_tools().await`
- 都是**同步等待完成**
- 区别只是超时时间和连接稳定性
- **中间过程不透明**

**WebSocket + SSE 双通道的本质：**
- WebSocket: 发起任务并等待最终结果
- SSE: 实时接收 `agent_start`, `tool_call_start`, `tool_call`, `agent_end` 事件
- **中间过程完全透明**

## 许可证

MIT OR Apache-2.0
