from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tomllib

from .config import WorkspaceError, direct_project
from .offline import OfflineBundle, sync_offline
from .server import (
    LocalWebProject,
    current_web_runtime,
    serve_local_web,
    sync_web_runtime,
    web_runtime_cache_root,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="tapmaker-local-web",
        description="不上传文件的 TapMaker UrhoX Web 预览",
    )
    subcommands = result.add_subparsers(dest="command", required=True)

    web = subcommands.add_parser("web", help="启动本地 Web 预览")
    web.add_argument(
        "--code",
        type=Path,
        action="append",
        required=True,
        help="本地项目或资源目录；多目录时可重复传入",
    )
    web.add_argument(
        "--entry",
        required=True,
        help="入口定位路径：可相对项目根/共同父目录，或相对唯一资源根；运行时去掉资源根前缀",
    )
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    web.add_argument("--runtime", choices=("auto", "local", "remote"), default="auto")
    web.add_argument("--runtime-cache", type=Path)
    web.add_argument("--offline-cache", type=Path, help="使用完整离线缓存包（拒绝外联）")
    web.add_argument("--offline-snapshot", help="固定离线缓存包 SHA256")
    web.add_argument("--no-platform-mock", action="store_true")
    web.add_argument(
        "--orientation",
        choices=("landscape", "portrait"),
        default="landscape",
        help="预览方向（默认：landscape）",
    )

    runtime = subcommands.add_parser("web-runtime", help="同步和查看本地 Web Runtime")
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_sync = runtime_commands.add_parser("sync", help="从官方 CDN 同步并校验 Runtime")
    runtime_sync.add_argument("--cache", type=Path)
    runtime_status = runtime_commands.add_parser("status", help="查看当前本地 Runtime")
    runtime_status.add_argument("--cache", type=Path)
    offline = subcommands.add_parser("web-offline", help="准备、校验完整离线缓存包")
    commands = offline.add_subparsers(dest="offline_command", required=True)
    sync = commands.add_parser("sync")
    sync.add_argument("--cache", type=Path, required=True)
    sync.add_argument("--code", type=Path, action="append")
    sync.add_argument("--entry")
    sync.add_argument("--official-resource", action="append", default=[])
    sync.add_argument("--official-group", action="append", default=[])
    sync.add_argument("--max-bytes", type=int, default=512 * 1024 * 1024)
    status = commands.add_parser("status")
    status.add_argument("--cache", type=Path, required=True)
    status.add_argument("--snapshot")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "web-offline":
            if args.offline_command == "status":
                bundle = OfflineBundle(args.cache, args.snapshot)
                print(f"Verified offline snapshot {bundle.id}; {len(bundle.files)} files")
                return 0
            if bool(args.code) != bool(args.entry):
                raise WorkspaceError("--code and --entry must be supplied together")
            texts = ""
            if args.code:
                state = LocalWebProject(direct_project(args.code, args.entry))
                for item in state.manifest()["files"]:
                    if item["ext"] in (".lua", ".json", ".xml", ".material", ".prefab"):
                        asset = state.asset(f"{item['uuid']}-{item['hash']}{item['ext']}")
                        texts += asset.read().decode("utf-8", errors="replace") + "\n"
            print(sync_offline(args.cache, texts=texts, resources=args.official_resource,
                               groups=args.official_group, max_bytes=args.max_bytes))
            return 0
        if args.command == "web-runtime":
            cache = args.cache or web_runtime_cache_root()
            if args.runtime_command == "sync":
                print(sync_web_runtime(cache))
                return 0
            runtime = current_web_runtime(cache)
            print(runtime if runtime is not None else f"未同步（缓存目录：{cache}）")
            return 0 if runtime is not None else 1

        if args.offline_snapshot and not args.offline_cache:
            raise WorkspaceError("--offline-snapshot requires --offline-cache")
        if args.offline_cache and (args.runtime != "auto" or args.runtime_cache):
            raise WorkspaceError("--offline-cache cannot be combined with --runtime/--runtime-cache")
        project = direct_project(args.code, args.entry)
        serve_local_web(
            project,
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
            runtime=args.runtime,
            runtime_cache=args.runtime_cache,
            platform_mock=not args.no_platform_mock,
            orientation=args.orientation,
            offline=OfflineBundle(args.offline_cache, args.offline_snapshot) if args.offline_cache else None,
        )
        return 0
    except (WorkspaceError, KeyError, ValueError, OSError, tomllib.TOMLDecodeError) as error:
        print(f"tapmaker-local-web: {error}", file=sys.stderr)
        return 2
