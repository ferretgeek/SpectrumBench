#!/usr/bin/env python3
"""SpectrumBench：额度与可见 token 速度仪表盘。"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from typing import Any
from urllib.parse import quote


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _check_deps() -> None:
    missing = []
    for package in ("fastapi", "uvicorn", "openai", "websockets", "tiktoken"):
        try:
            __import__(package)
        except ImportError:
            missing.append(package)
    if missing:
        missing_list = ", ".join(missing)
        raise SystemExit(f"缺少依赖: {missing_list}\n请先执行: {sys.executable} -m pip install -r requirements.txt")


def _ensure_port_available(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        if sock.connect_ex((host, port)) == 0:
            raise SystemExit(f"启动失败: {host}:{port} 已被占用，无法启动服务。")


def _wait_and_open(server: Any, host: str, port: int, open_browser: bool) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        if getattr(server, "started", False):
            access_host = "127.0.0.1" if host == "0.0.0.0" else host
            from stress_tool.server import SESSION_TOKEN

            access_url = f"http://{access_host}:{port}/#session={quote(SESSION_TOKEN, safe='')}"
            print("\n  SpectrumBench · 光谱测速台已启动")
            print(f"  访问地址（含本次会话令牌，请勿转发）: {access_url}\n")
            if open_browser:
                webbrowser.open(access_url)
            return
        time.sleep(0.1)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 SpectrumBench · 光谱测速台")
    parser.add_argument(
        "--host",
        choices=("127.0.0.1", "0.0.0.0"),
        default="127.0.0.1",
        help="监听地址；默认仅本机，容器内可使用 0.0.0.0",
    )
    parser.add_argument("--port", type=int, default=18976, help="本机监听端口，默认 18976")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="不自动打开浏览器；适合远程服务器配合 SSH 端口转发",
    )
    return parser


def main() -> None:
    _configure_stdio()
    args = _build_parser().parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("port 必须在 1–65535 之间")
    _check_deps()

    import uvicorn

    from stress_tool.server import app

    host = args.host
    port = args.port
    _ensure_port_available(host, port)

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(
        target=_wait_and_open,
        args=(server, host, port, not args.no_browser),
        daemon=True,
    ).start()
    server.run()


if __name__ == "__main__":
    main()
