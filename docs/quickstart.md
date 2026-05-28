# Quickstart

## 1. 先决条件

- Python 3.10+
- 一台公网/内网可达的机器跑 `server`（可以是你的开发机）
- 飞书自建应用：拿到 `App ID` / `App Secret`，开通「机器人」能力，订阅 `im.message.receive_v1` 事件
- 用户电脑：装好 `claude` CLI 且能直接命令行登录使用

## 2. 装 server

```bash
git clone <repo> feishu-cc
cd feishu-cc
python3 -m venv venv && source venv/bin/activate
pip install -e .

cp .env.example .env
# 编辑 .env：填 FEISHU_APP_ID / FEISHU_APP_SECRET / BRIDGE_PAIR_TOKENS
openssl rand -hex 24      # 生成一个 pair_token 填到 BRIDGE_PAIR_TOKENS
```

启动：

```bash
feishu-cc-server
```

看到日志 `bridge ws 监听在 ws://0.0.0.0:8765/ws/bridge` 即可。

## 3. 装 bridge（在你电脑上）

```bash
# 同一份 repo 安装即可，或单独 pip install -e .
export FEISHU_CC_SERVER_WS="ws://你的server:8765/ws/bridge"
export FEISHU_CC_PAIR_TOKEN="刚才生成的 token"

feishu-cc-bridge run
```

看到日志 `已发送 hello, bridge_id=bridge-xxx` 表示握手成功。

## 4. 在飞书里玩

跟机器人私聊（或在群里 @机器人）：

```
/cc start
/cc list
/cc bridges
```

启动会话后直接发文本就是 prompt。Claude 的回复会一段段流回飞书。

## 5. 常用命令

```
/cc start [cwd]                 启动新会话，可指定工作目录
/cc list                        列出你的活跃会话
/cc use <session_id>            切换默认会话
/cc resume <claude_session_id?> 恢复历史 claude 会话；不传 id 则 --continue
/cc stop [session_id?]          关闭当前/指定会话
/cc bridges                     列出已连接的本地 bridge
/cc help                        显示帮助
其他任意文本                    发给当前会话作为 prompt
```

## 6. 安全注意

- `BRIDGE_PAIR_TOKENS` 是 bridge 配对的唯一凭据，按设备分发不同 token，泄漏立即更换
- `FEISHU_ALLOWED_USERS` 强烈建议配置成你自己的 `open_id`，避免群里别人随手 `/cc start` 跑你电脑上的命令
- bridge 默认会用你登录态的 `claude` CLI 执行任何 prompt，相当于把电脑的 shell 权限放给飞书消息源，**不要把机器人加进公开群**

## 7. 已知限制（MVP）

- `/compact` 暂未实现（headless 模式无对应入口；下一版会通过自动重启 + summary 模拟）
- 工具权限提示尚未走飞书交互卡片，bridge 端默认按 claude 配置走
- 长消息没做合并/编辑，会切成多条飞书消息
