"""bridge 命令行入口：``feishu-cc-bridge run``（v2 远程模式）。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from bridge.client import BridgeClient
from bridge.config import load


# 解析命令行参数；环境变量是默认值，CLI 可覆盖
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feishu-cc-bridge",
        description="feishu-cc 本地守护进程：连接 server，按指令启停本机 claude 会话",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="启动 bridge")
    run_p.add_argument("--server-ws", dest="server_ws_url", help="server WebSocket URL")
    run_p.add_argument("--pair-token", help="配对 token")
    run_p.add_argument("--bridge-id", help="bridge 标识，留空自动生成")
    run_p.add_argument("--claude-bin", help="claude 可执行文件路径")
    run_p.add_argument("--cwd", dest="default_cwd", help="默认工作目录")
    run_p.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return parser


# 主入口
def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if args.cmd != "run":
        parser.error("未知子命令")

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    overrides = {
        k: v for k, v in {
            "server_ws_url": args.server_ws_url,
            "pair_token": args.pair_token,
            "bridge_id": args.bridge_id,
            "claude_bin": args.claude_bin,
            "default_cwd": args.default_cwd,
        }.items() if v
    }
    config = load(overrides)
    client = BridgeClient(config)
    try:
        asyncio.run(client.run_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
