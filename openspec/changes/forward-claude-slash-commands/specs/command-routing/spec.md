# command-routing Specification

## MODIFIED Requirements

### Requirement: 飞书命令分发 MUST distinguish feishu-cc commands from Claude slash commands

飞书文本入口 MUST only route known feishu-cc commands to feishu-cc handlers. Unknown slash-prefixed text MUST be forwarded to the current Claude session unchanged.

#### Scenario: Forward Claude compact command

- **GIVEN** 用户已有当前 Claude 会话
- **WHEN** 用户在飞书发送 `/compact`
- **THEN** 系统 forwards `/compact` to the current Claude session as prompt text
- **AND** 系统 MUST NOT return feishu-cc help text

#### Scenario: Keep known feishu-cc slash command

- **WHEN** 用户在飞书发送 `/help`
- **THEN** 系统 routes the command to feishu-cc help handler

#### Scenario: Keep explicit feishu-cc namespace

- **WHEN** 用户在飞书发送 `/cc help`
- **THEN** 系统 routes the command to feishu-cc help handler
