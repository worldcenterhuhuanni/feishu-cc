# feishu-cc

在飞书里管理本机 Claude Code 会话：启停、对话、Claude 内置斜杠命令、流式输出回显。

## 架构

```
飞书用户 ──▶ 飞书开放平台 ──▶ server/           ──WebSocket──▶ bridge/         ──▶ claude
                              (云端，自己部署)                  (用户电脑，常驻)      (本机 CLI)
                              事件接入                          子进程管理
                              会话路由                          stream-json IO
                              发消息回飞书
```

- `server/`：跑在云上（或任意公网/内网可达机器）。订阅飞书事件、维护「飞书用户 → bridge → claude 会话」三段映射、把 stream-json 事件转成飞书消息。
- `bridge/`：跑在你电脑上的小守护进程。通过 WebSocket 跟 server 长连，按指令 spawn `claude` 子进程，转发 IO。
- `mcp/`：（规划中）独立的 MCP server，让本机 Claude 主动往飞书发通知 / 弹问题，复用给任何 Claude Code 用户。

## 飞书侧命令

飞书里可以直接发命令，也可以加 `/cc` 前缀；下面两种写法等价：

```text
help
/cc help
```

### 会话入口

```text
projects              列出本机 Claude 历史项目，按编号进入项目 / 会话向导
start [路径]          在指定路径启动新会话；不传路径时使用最近的 Claude 项目
```

推荐先用 `projects`：它会列出 `~/.claude/projects/` 里的历史项目，选择项目后还能选择历史会话或新建会话。

### 会话管理

```text
list                  列出你的活跃会话
ls                    list 的简写
use <session_id>      切换默认会话
stop [session_id]     关闭当前会话；传 session_id 时关闭指定会话
kill [session_id]     stop 的别名
help                  显示帮助
```

### 对话与 Claude 内置命令

启动会话后，其他任意文本都会发给当前会话作为 prompt。Claude Code 内置斜杠命令也直接发送给当前会话，例如：

```
/compact              压缩当前 Claude 会话上下文
/clear                交给 Claude Code 处理
帮我总结一下当前项目
```

注意：未知的 `/xxx` 不会显示 feishu-cc 帮助，而是原样转发给 Claude；只有已知 feishu-cc 命令（如 `/help`、`/list`）会被本项目拦截处理。

## 快速开始

见 [docs/quickstart.md](docs/quickstart.md)。

## 状态

MVP — 功能基本完整，UX 还在打磨。不建议生产使用。
