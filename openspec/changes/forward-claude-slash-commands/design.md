# Design

## Command Routing

`server.commands._parse()` 保持 `/cc ...` 作为显式 feishu-cc 命名空间。裸命令和已知 `/命令` 继续进入 feishu-cc 路由，方便用户在飞书里快速操作本项目会话。

对于未知 `/...` 文本，解析结果返回 `None`，由 `dispatch()` 走现有 `manager.send_prompt()` 路径转发给当前 Claude 会话。这样不需要在 feishu-cc 维护 Claude Code 内置命令白名单，也能兼容未来新增的 Claude 斜杠命令。

## Compatibility

已知 feishu-cc 命令行为不变；只有未知斜杠命令从“显示帮助”改为“转发给 Claude”。这符合 README 中“其他任意文本发送给当前会话”的语义。
