from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
import tomllib
from urllib.request import urlopen

from . import session
from .config import Project, WorkspaceError, direct_project
from .server import (
    current_web_runtime,
    parse_viewport_size,
    serve_local_web,
    sync_web_runtime,
    web_runtime_cache_root,
)


def _add_project_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--code",
        type=Path,
        action="append",
        required=True,
        help="本地项目或资源目录；多目录时可重复传入",
    )
    command.add_argument(
        "--entry",
        required=True,
        help="入口定位路径：可相对项目根/共同父目录，或相对唯一资源根",
    )


def _add_start_arguments(command: argparse.ArgumentParser, *, detached: bool) -> None:
    _add_project_arguments(command)
    command.add_argument("--host", default="127.0.0.1")
    command.add_argument("--port", type=int, default=8765)
    command.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    command.add_argument("--runtime", choices=("auto", "local", "remote"), default="auto")
    command.add_argument("--runtime-cache", type=Path)
    command.add_argument("--no-platform-mock", action="store_true")
    command.add_argument(
        "--orientation",
        choices=("landscape", "portrait"),
        default="landscape",
        help="预览方向（默认：landscape）",
    )
    command.add_argument(
        "--size",
        help="预览视口尺寸 WxH（100-4096），例如 1260x540；缺省使用方向默认尺寸",
    )
    if detached:
        command.add_argument(
            "--detached",
            action="store_true",
            help="后台运行会话，用 preview 其它动词管理",
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="tapmaker-local-web",
        description="不上传文件的 TapMaker UrhoX Web 预览",
    )
    subcommands = result.add_subparsers(dest="command", required=True)

    web = subcommands.add_parser("web", help="启动本地 Web 预览（preview start 的前台别名）")
    _add_start_arguments(web, detached=False)

    preview = subcommands.add_parser(
        "preview", help="复刻官方 preview 工作流：start/status/stop/refresh/logs/screenshot/check"
    )
    preview_commands = preview.add_subparsers(dest="preview_command", required=True)

    start = preview_commands.add_parser("start", help="启动本地 Web 预览（默认前台）")
    _add_start_arguments(start, detached=True)

    status = preview_commands.add_parser("status", help="查看会话运行状态")
    _add_project_arguments(status)

    stop = preview_commands.add_parser("stop", help="停止运行中的预览会话")
    _add_project_arguments(stop)
    stop.add_argument("--force", action="store_true", help="优雅停止超时后强制结束进程")

    refresh = preview_commands.add_parser("refresh", help="让已连接页面整页重载")
    _add_project_arguments(refresh)

    logs = preview_commands.add_parser("logs", help="查看页面上报的运行日志")
    _add_project_arguments(logs)
    logs.add_argument("--lines", type=int, default=200, help="显示最近 N 行（默认 200）")
    logs.add_argument("--follow", action="store_true", help="持续输出新日志，Ctrl-C 退出")

    screenshot = preview_commands.add_parser("screenshot", help="请求预览页面回传一张 PNG 截图")
    _add_project_arguments(screenshot)
    screenshot.add_argument("--out", type=Path, help="输出文件路径；缺省写入会话目录")
    screenshot.add_argument(
        "--timeout", type=float, default=20.0, help="等待页面回传的秒数（默认 20）"
    )

    check = preview_commands.add_parser("check", help="汇总启动里程碑、错误计数与诊断")
    _add_project_arguments(check)

    runtime = subcommands.add_parser("web-runtime", help="同步和查看本地 Web Runtime")
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_sync = runtime_commands.add_parser("sync", help="从官方 CDN 同步并校验 Runtime")
    runtime_sync.add_argument("--cache", type=Path)
    runtime_status = runtime_commands.add_parser("status", help="查看当前本地 Runtime")
    runtime_status.add_argument("--cache", type=Path)
    return result


def _run_start(args: argparse.Namespace, *, detached: bool) -> int:
    project = direct_project(args.code, args.entry)
    size = parse_viewport_size(args.size) if args.size else None
    if detached:
        argv = [
            "preview",
            "start",
            *(argument for path in args.code for argument in ("--code", str(path))),
            "--entry",
            args.entry,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--runtime",
            args.runtime,
            "--orientation",
            args.orientation,
        ]
        if args.runtime_cache is not None:
            argv += ["--runtime-cache", str(args.runtime_cache)]
        if args.no_platform_mock:
            argv.append("--no-platform-mock")
        if args.size:
            argv += ["--size", args.size]
        if args.no_open:
            argv.append("--no-open")
        record = session.start_detached(project, argv)
        print(f"TapMaker 本地 Web 预览（后台）：{record.url}")
        print(f"控制台：{record.base_url()}/console")
        print(f"PID={record.pid} 会话目录={session.session_dir(project.name)}")
        print("管理：preview status|refresh|logs|screenshot|check|stop（需相同的 --code/--entry）")
        return 0
    serve_local_web(
        project,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        runtime=args.runtime,
        runtime_cache=args.runtime_cache,
        platform_mock=not args.no_platform_mock,
        orientation=args.orientation,
        size=size,
        state_root_dir=session.state_root(),
    )
    return 0


def _require_record(args: argparse.Namespace) -> tuple[Project, session.SessionRecord]:
    project = direct_project(args.code, args.entry)
    record = session.read_record(project.name)
    if record is None or not session.pid_alive(record.pid):
        raise WorkspaceError(f"没有运行中的预览会话（项目 {project.name}）；先运行 preview start")
    return project, record


def _preview_status(args: argparse.Namespace) -> int:
    project = direct_project(args.code, args.entry)
    record = session.read_record(project.name)
    if record is None:
        print(json.dumps({"running": False, "reason": "no_session"}, ensure_ascii=False))
        return 0
    alive = session.pid_alive(record.pid)
    running = alive and session.health(record) is not None
    result = {**record.to_dict(), "token": "***", "pid_alive": alive, "running": running}
    if running:
        result["check"] = session.fetch_json(record.base_url() + "/__tapmaker/check")
    elif not alive:
        result["reason"] = "process_exited"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _preview_stop(args: argparse.Namespace) -> int:
    project, record = _require_record(args)
    status, body = session.control_post(record, "/__tapmaker/control/shutdown")
    if status != 200 and session.pid_alive(record.pid):
        raise WorkspaceError(f"停止请求失败（HTTP {status}）：{body.get('error', body)}")
    if session.wait_for_exit(record.pid, session.STOP_TIMEOUT_SECONDS):
        session.remove_record(project.name)
        print(f"预览已停止（PID {record.pid}）。")
        return 0
    if not args.force:
        raise WorkspaceError(
            f"进程未在 {session.STOP_TIMEOUT_SECONDS}s 内退出；使用 --force 强制结束"
        )
    os.kill(record.pid, signal.SIGTERM)
    if not session.wait_for_exit(record.pid, 3):
        os.kill(record.pid, signal.SIGKILL)
        session.wait_for_exit(record.pid, 3)
    session.remove_record(project.name)
    print(f"已强制结束 PID {record.pid}。")
    return 0


def _preview_refresh(args: argparse.Namespace) -> int:
    _project, record = _require_record(args)
    status, body = session.control_post(record, "/__tapmaker/control/refresh")
    if status != 200:
        raise WorkspaceError(f"刷新失败（HTTP {status}）：{body.get('error', body)}")
    print(f"已通知页面重载（revision={body.get('revision')}）。")
    return 0


def _print_log_lines(lines: list[dict[str, object]]) -> None:
    for line in lines:
        print(f"{line.get('ts', '')} [{line.get('level', 'info')}] {line.get('message', '')}")


def _preview_logs(args: argparse.Namespace) -> int:
    _project, record = _require_record(args)
    next_position = 0
    try:
        while True:
            data = session.fetch_json(
                record.base_url() + f"/__tapmaker/logs?lines={max(1, args.lines)}"
            )
            lines = data.get("lines") or []
            dropped = int(data.get("dropped", 0))
            fresh = [
                lines[index]
                for index in range(len(lines))
                if dropped + index >= next_position
            ]
            _print_log_lines(fresh)  # type: ignore[arg-type]
            next_position = dropped + len(lines)
            if not args.follow:
                return 0
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def _preview_screenshot(args: argparse.Namespace) -> int:
    project, record = _require_record(args)
    status, body = session.control_post(record, "/__tapmaker/control/screenshot")
    if status == 409:
        raise WorkspaceError(str(body.get("error") or "没有已连接的预览页面，无法截图"))
    if status != 200:
        raise WorkspaceError(f"截图请求失败（HTTP {status}）：{body.get('error', body)}")
    requested_id = str(body.get("id"))
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        info = session.fetch_json(record.base_url() + "/__tapmaker/screenshot")
        if info.get("status") == "ready" and info.get("id") == requested_id:
            with urlopen(record.base_url() + "/__tapmaker/screenshot.png", timeout=10) as response:
                data = response.read()
            destination = args.out or session.session_dir(project.name) / (
                f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            print(f"截图已保存：{destination}（{len(data)} 字节）")
            return 0
        time.sleep(0.3)
    raise WorkspaceError(f"截图超时：页面未在 {args.timeout}s 内回传")


def _preview_check(args: argparse.Namespace) -> int:
    _project, record = _require_record(args)
    check = session.fetch_json(record.base_url() + "/__tapmaker/check")
    print(json.dumps(check, ensure_ascii=False, indent=2))
    return 0 if check.get("started") else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "web-runtime":
            cache = args.cache or web_runtime_cache_root()
            if args.runtime_command == "sync":
                print(sync_web_runtime(cache))
                return 0
            runtime = current_web_runtime(cache)
            print(runtime if runtime is not None else f"未同步（缓存目录：{cache}）")
            return 0

        if args.command == "web":
            return _run_start(args, detached=False)

        handlers = {
            "start": lambda: _run_start(args, detached=args.detached),
            "status": lambda: _preview_status(args),
            "stop": lambda: _preview_stop(args),
            "refresh": lambda: _preview_refresh(args),
            "logs": lambda: _preview_logs(args),
            "screenshot": lambda: _preview_screenshot(args),
            "check": lambda: _preview_check(args),
        }
        return handlers[args.preview_command]()
    except (WorkspaceError, KeyError, OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"tapmaker-local-web: {error}", file=sys.stderr)
        return 2
