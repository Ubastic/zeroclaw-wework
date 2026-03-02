# WeCom-Zeroclaw Bridge (WebSocket 版本)

企业微信与 zeroclaw 的 WebSocket 集成桥接，支持实时进度更新。

## 功能特性

- ✅ **实时进度更新** - 通过 WebSocket 接收 AI 处理进度
- ✅ **长时间任务支持** - 适合复杂的研究、分析任务
- ✅ **自动进度提示** - 每 30 秒自动发送处理进度
- ✅ **异步处理** - 不阻塞企业微信回调
- ✅ **完整的错误处理** - 友好的错误提示

## 工作流程

```
用户发送消息
  ↓
企业微信 → wecom_zeroclaw.py
  ↓
立即回复："收到您的问题，AI 正在思考中..."
  ↓
建立 WebSocket 连接到 zeroclaw
  ↓
接收实时内容片段
  ↓
每 30 秒发送进度更新："[处理中] ..."
  ↓
完成后发送最终结果
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
AI:   "今天北京天气晴朗，温度 15-25°C..."
```

### 复杂任务
```
用户: "帮我研究 zeroclaw vs openclaw 的区别"
AI:   "收到您的问题，AI 正在思考中..."
      [30秒后]
AI:   "[处理中] 我正在分析 zeroclaw 项目结构..."
      [60秒后]
AI:   "[处理中] 已完成 zeroclaw 分析，正在研究 openclaw..."
      [90秒后]
AI:   "[处理中] 正在对比两个项目的差异..."
      [120秒后]
AI:   "完整的对比报告：
            1. zeroclaw 是 100% Rust 实现...
            2. openclaw 是 Python/TypeScript...
            3. 主要差异：..."
```

## WebSocket 消息格式

zeroclaw WebSocket 发送的消息类型：

```json
// 内容片段
{"type": "content", "content": "部分回复内容..."}

// 工具使用
{"type": "tool_use", "tool": "git_operations", "args": {...}}

// 错误
{"type": "error", "message": "错误描述"}

// 完成
{"type": "done"}
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

## 与 HTTP 版本的对比

| 特性 | HTTP 版本 | WebSocket 版本 |
|------|----------|----------------|
| 实时进度 | ❌ | ✅ |
| 长任务支持 | ⚠️ 受超时限制 | ✅ 更好 |
| 用户体验 | 长时间等待 | 实时反馈 |
| 实现复杂度 | 简单 | 中等 |
| 资源占用 | 低 | 稍高（保持连接） |

## 许可证

MIT OR Apache-2.0
