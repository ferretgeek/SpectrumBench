"""Render Chinese launcher messages outside cmd.exe's batch-file parser."""

import sys


def render_message(key, *args):
    """Return one user-facing launcher message block."""
    if key == "start":
        url = args[0]
        no_browser = len(args) > 1 and args[1] == "1"
        action = "正在启动本地服务，不自动打开浏览器。" if no_browser else "正在启动本地服务，浏览器会自动打开。"
        return (
            "\n==========================================\n"
            "  大模型接口测速台\n"
            "==========================================\n"
            f"  {action}\n"
            f"  地址：{url}\n"
        )
    if key == "already_running":
        return f"\n本地服务已经在运行，不会重复启动。\n地址：{args[0]}"
    if key == "dry_run":
        return "启动前检查通过：Python、版本与依赖均可用。"
    if key == "normal_end":
        return "\n服务已正常结束。"
    if key == "stopped_by_user":
        return "\n已收到结束服务请求，服务已正常关闭。"
    if key == "unsupported_python":
        return (
            "\n启动失败：Python 版本低于 3.10，无法运行本工具。\n"
            f"当前解释器：{args[0]}\n"
            "请安装 Python 3.10 或更高版本。"
        )
    if key == "missing_dependencies":
        return (
            "\n启动失败：Python 依赖不完整或无法导入。\n"
            "请在当前目录执行：\n"
            f'  "{args[0]}" -m pip install -r "requirements.txt"'
        )
    if key == "run_failed":
        return f"\n服务启动或运行失败，退出码：{args[0]}\n请检查上方错误；常见原因是 18976 端口被其他程序占用。"
    if key == "stop_header":
        return (
            "\n==========================================\n"
            "  结束大模型接口测速台\n"
            "==========================================\n"
            f"  只处理本机端口 {args[0]} 的监听进程。\n"
        )
    if key == "stopping_pid":
        return f"正在结束 PID {args[0]} ..."
    if key == "stop_success":
        return f"服务已结束，端口 {args[0]} 已释放。"
    if key == "nothing_to_stop":
        return f"未发现占用端口 {args[0]} 的监听服务，无需处理。"
    if key == "missing_netstat":
        return "\n结束失败：系统找不到 netstat，无法安全定位监听进程。"
    if key == "identity_refused":
        return f"\n安全保护：端口 {args[0]} 上的服务无法验证为 SpectrumBench，未结束任何进程。"
    if key == "stop_failed":
        return f"\n部分监听进程未能结束。请用管理员终端复查端口 {args[0]}。"
    if key == "verify_failed":
        return f"\n结束后端口 {args[0]} 仍在监听，请用 netstat -ano 手动复查。"
    if key == "taskkill_failed":
        return f"结束 PID {args[0]} 失败，taskkill 退出码：{args[1]}"
    if key == "fatal_start":
        return "\n启动失败：无法进入脚本所在目录。"
    if key == "fatal_stop":
        return "\n结束失败：无法进入脚本所在目录。"
    raise ValueError(f"未知消息类型：{key}")


def main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    if not values:
        print("缺少启动消息类型。", file=sys.stderr)
        return 64
    try:
        message = render_message(values[0], *values[1:])
    except (IndexError, ValueError) as exc:
        print(f"启动消息参数错误：{exc}", file=sys.stderr)
        return 64
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
