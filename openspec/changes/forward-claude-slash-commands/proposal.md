# Forward Claude Slash Commands

## Why

飞书里输入 Claude Code 内置命令 `/compact` 时，当前命令分发器把所有 `/...` 文本都当作 feishu-cc 命令解析；未知命令回退到帮助，导致 Claude 无法执行压缩。

## What Changes

- 仅拦截 feishu-cc 已知命令（如 `/help`、`/list`）和 `/cc ...` 命令。
- 未知斜杠命令（如 `/compact`、`/resume` 等 Claude 内置命令）按普通文本转发给当前 Claude 会话。
- 帮助文案说明 Claude 内置斜杠命令会转发给当前会话。

## Non-goals

- 不新增 feishu-cc 自己的 `/compact` 实现。
- 不改变 `projects`、`start`、`list`、`use`、`stop`、`help` 等已有命令行为。
