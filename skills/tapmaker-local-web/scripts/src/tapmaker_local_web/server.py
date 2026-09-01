from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import shutil
import sys
import threading
from urllib.parse import quote, urlsplit
import webbrowser

from watchfiles import watch

from .config import Project, WorkspaceError
from .inspector import COMPONENT_TREE_AGENT_PROMPT
from .page import ORIENTATION_SIZES, render_index
from .project import AssetRecord, LOCAL_ENGINE, LocalWebProject, _is_operating_system_metadata
from .prototype_workbench import render_workbench
from .runtime import (
    ENGINE_BASE_URL,
    RUNTIME_FILES,
    _runtime_etags,
    current_web_runtime,
    sync_web_runtime,
    web_runtime_cache_root,
)

DYNAMIC_CACHE_CONTROL = "no-store"
RUNTIME_CACHE_CONTROL = "private, no-cache"
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"

class LocalWebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        state: LocalWebProject,
        runtime_dir: Path | None = None,
        *,
        orientation: str = "landscape",
        inspector: bool | None = None,
    ):
        if orientation not in ORIENTATION_SIZES:
            available = ", ".join(ORIENTATION_SIZES)
            raise WorkspaceError(f"未知预览方向：{orientation}；可用方向：{available}")
        self.state = state
        self.runtime_dir = runtime_dir
        self.orientation = orientation
        self.inspector = state.inspector if inspector is None else inspector
        self.index_html = render_index(orientation, inspector=self.inspector)
        self.runtime_etags = _runtime_etags(runtime_dir)
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        super().__init__(address, _LocalWebHandler)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        query = f"skip_login&verbose=true&screen_orientation={self.orientation}"
        query += f"&entry={quote(self.state.deployment.entry, safe='/')}"
        if self.runtime_dir is not None:
            query += "&local_engine=true"
        return f"http://{display_host}:{port}/?{query}"

    @property
    def workbench_url(self) -> str:
        host, port = self.server_address[:2]
        display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        return f"http://{display_host}:{port}/__tapmaker/prototype/workbench?variant=A"

    def workbench_html(self) -> bytes:
        assets = [
            str(item["fs_path"])
            for item in self.state.manifest()["files"]
            if item["fs_path"] != "__tapmaker_project_entry.lua"
        ]
        return render_workbench(
            f"{self.url}&workbench_session=stage-main",
            project_name=self.state.project.name,
            entry=self.state.deployment.entry,
            assets=assets,
        )

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self._start_watcher()
        try:
            super().serve_forever(poll_interval)
        finally:
            self._stop_watcher()

    def shutdown(self) -> None:
        self._watch_stop.set()
        super().shutdown()

    def server_close(self) -> None:
        self._stop_watcher()
        super().server_close()

    def _start_watcher(self) -> None:
        if self._watch_thread is not None:
            return
        paths = self._watch_paths()
        if not paths:
            return
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            args=(paths,),
            name="tapmaker-file-watcher",
            daemon=True,
        )
        self._watch_thread.start()

    def _stop_watcher(self) -> None:
        self._watch_stop.set()
        thread = self._watch_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._watch_thread = None

    def _watch_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for mount in self.state.project.mounts:
            paths.append(mount.source if mount.source.is_dir() else mount.source.parent)
        settings = (
            self.state.deployment.cache / ".project/settings.json"
            if self.state.deployment.cache is not None
            else None
        )
        for source in (settings, self.state.project.version_file):
            if source is not None:
                parent = source.parent
                if parent.is_dir():
                    paths.append(parent)
        return tuple(dict.fromkeys(path.resolve() for path in paths))

    def _watch_filter(self, _change: object, changed: str) -> bool:
        candidate = Path(changed).resolve()
        for mount in self.state.project.mounts:
            source = mount.source.resolve()
            if source.is_dir() and candidate.is_relative_to(source):
                return not _is_operating_system_metadata(candidate.relative_to(source))
            if candidate in (source, source.with_name(f"{source.name}.meta")):
                return True
        settings = (
            self.state.deployment.cache / ".project/settings.json"
            if self.state.deployment.cache is not None
            else None
        )
        return candidate in {
            source.resolve()
            for source in (settings, self.state.project.version_file)
            if source is not None
        }

    def _watch_loop(self, paths: tuple[Path, ...]) -> None:
        try:
            for _changes in watch(
                *paths,
                watch_filter=self._watch_filter,
                debounce=200,
                step=50,
                stop_event=self._watch_stop,
                raise_interrupt=False,
            ):
                previous_missing = tuple(self.state.diagnostics()["missing_meta"])
                changed = self._refresh_after_change()
                if changed:
                    current_missing = tuple(self.state.diagnostics()["missing_meta"])
                    if current_missing != previous_missing:
                        _print_missing_meta(current_missing)
        except Exception as error:
            if not self._watch_stop.is_set():
                print(f"tapmaker-local-web: 文件监听失败：{error}", file=sys.stderr)

    def _refresh_after_change(self) -> bool:
        error: OSError | WorkspaceError | None = None
        for attempt in range(3):
            try:
                return self.state.refresh()
            except (OSError, WorkspaceError) as caught:
                error = caught
                if attempt < 2 and not self._watch_stop.wait(0.05):
                    continue
                break
        if not self._watch_stop.is_set():
            print(f"tapmaker-local-web: 文件变化后刷新失败：{error}", file=sys.stderr)
        return False


class _LocalWebHandler(BaseHTTPRequestHandler):
    server: LocalWebServer

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:  # noqa: N802
        self._handle(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(head_only=True)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._common_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path != "/__tapmaker/component-tree" or not self.server.inspector:
            self._send(404, b"not found\n", "text/plain; charset=utf-8", False)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise WorkspaceError("组件树上报大小无效")
            value = json.loads(self.rfile.read(length))
            self.server.state.update_component_tree(value)
            self._send_json({"ok": True}, False)
        except (ValueError, json.JSONDecodeError, WorkspaceError) as error:
            self._send(
                400,
                (json.dumps({"error": str(error)}, ensure_ascii=False) + "\n").encode("utf-8"),
                "application/json; charset=utf-8",
                False,
            )

    def _handle(self, *, head_only: bool) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/":
                self._send(200, self.server.index_html, "text/html; charset=utf-8", head_only)
                return
            if path == "/__tapmaker/prototype/workbench":
                self._send(
                    200,
                    self.server.workbench_html(),
                    "text/html; charset=utf-8",
                    head_only,
                )
                return
            if path == "/__tapmaker/revision":
                self._send_json({"revision": self.server.state.build}, head_only)
                return
            if path == "/__tapmaker/status":
                self._send_json(self.server.state.status(), head_only)
                return
            if path == "/__tapmaker/component-tree":
                if not self.server.inspector:
                    self._send_json(
                        {
                            "root": None,
                            "warning": "组件树功能未启用；请使用 --inspect 启动本地预览。",
                            "agent_prompt": COMPONENT_TREE_AGENT_PROMPT,
                        },
                        head_only,
                    )
                    return
                self._send_json(self.server.state.component_tree(), head_only)
                return
            if path == "/__tapmaker/events" and not head_only:
                self._send_events()
                return
            if path in ("/latest.json", f"/{self.server.state.version}/version.json"):
                self._send_json(self.server.state.latest(), head_only)
                return
            if path in ("/project.json", f"/{self.server.state.version}/project.json"):
                self._send_json(self.server.state.project_info(), head_only)
                return
            if path == "/env.json":
                self._send_json({"env": "local"}, head_only)
                return
            if path.removeprefix("/") in RUNTIME_FILES and self.server.runtime_dir is not None:
                runtime_name = path.removeprefix("/")
                runtime_file = self.server.runtime_dir / runtime_name
                if runtime_file.is_file():
                    media_type = mimetypes.guess_type(runtime_file.name)[0]
                    if runtime_file.suffix == ".wasm":
                        media_type = "application/wasm"
                    self._send_file(
                        runtime_file,
                        media_type or "application/octet-stream",
                        head_only,
                        cache_control=RUNTIME_CACHE_CONTROL,
                        etag=self.server.runtime_etags.get(runtime_name),
                    )
                    return
            if path == f"/{self.server.state.version}/engine-{LOCAL_ENGINE}.json":
                self._send_json({"tag": "stable", "base_url": ENGINE_BASE_URL}, head_only)
                return
            if path == f"/{self.server.state.version}/manifest-{self.server.state.client}.json":
                self._send_json(self.server.state.manifest(), head_only)
                return
            if path.startswith("/assets/") and path.count("/") == 2:
                asset = self.server.state.asset(path.removeprefix("/assets/"))
                if asset is not None:
                    media_type = mimetypes.guess_type(asset.fs_path)[0] or "application/octet-stream"
                    self._send_asset(asset, media_type, head_only)
                    return
            self._send(404, b"not found\n", "text/plain; charset=utf-8", head_only)
        except (BrokenPipeError, ConnectionResetError):
            return
        except (OSError, WorkspaceError) as error:
            try:
                self._send(
                    500,
                    f"{error}\n".encode("utf-8"),
                    "text/plain; charset=utf-8",
                    head_only,
                )
            except (BrokenPipeError, ConnectionResetError):
                return

    def _send_events(self) -> None:
        self.send_response(200)
        self._common_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        previous = -1
        while not self.server._watch_stop.is_set():
            revision = (
                self.server.state.build
                if previous < 0
                else self.server.state.wait_for_revision(previous, timeout=15)
            )
            if revision == previous:
                self.wfile.write(b": keep-alive\n\n")
            else:
                payload = json.dumps({"revision": revision}, separators=(",", ":"))
                self.wfile.write(f"event: revision\ndata: {payload}\n\n".encode("utf-8"))
                previous = revision
            self.wfile.flush()

    def _send_json(self, value: object, head_only: bool) -> None:
        body = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        self._send(200, body, "application/json; charset=utf-8", head_only)

    def _send_asset(self, asset: AssetRecord, media_type: str, head_only: bool) -> None:
        etag = f'"asset-{asset.crc32}"'
        if self._send_not_modified(etag, IMMUTABLE_CACHE_CONTROL):
            return
        self._send(
            200,
            asset.read(),
            media_type,
            head_only,
            cache_control=IMMUTABLE_CACHE_CONTROL,
            etag=etag,
        )

    def _send_file(
        self,
        path: Path,
        media_type: str,
        head_only: bool,
        *,
        cache_control: str,
        etag: str | None,
    ) -> None:
        if etag is not None and self._send_not_modified(etag, cache_control):
            return
        size = path.stat().st_size
        self.send_response(200)
        self._common_headers(cache_control)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(size))
        if etag is not None:
            self.send_header("ETag", etag)
        self.end_headers()
        if not head_only:
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile)

    def _send_not_modified(self, etag: str, cache_control: str) -> bool:
        candidates = {
            candidate.strip() for candidate in self.headers.get("If-None-Match", "").split(",")
        }
        if etag not in candidates and "*" not in candidates:
            return False
        self.send_response(304)
        self._common_headers(cache_control)
        self.send_header("ETag", etag)
        self.end_headers()
        return True

    def _send(
        self,
        status: int,
        body: bytes,
        media_type: str,
        head_only: bool,
        *,
        cache_control: str = DYNAMIC_CACHE_CONTROL,
        etag: str | None = None,
    ) -> None:
        self.send_response(status)
        self._common_headers(cache_control)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        if etag is not None:
            self.send_header("ETag", etag)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _common_headers(self, cache_control: str = DYNAMIC_CACHE_CONTROL) -> None:
        self.send_header("Cache-Control", cache_control)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin")

    def log_message(self, format: str, *args: object) -> None:
        # The player can request hundreds of local assets during startup. Keep
        # the terminal useful; request failures are surfaced in the browser.
        return


def _print_missing_meta(paths: tuple[str, ...]) -> None:
    if not paths:
        print("资源检查：所有已加载文件均存在对应 .meta")
        return
    print(f"警告：{len(paths)} 个已加载文件缺少对应 .meta：", file=sys.stderr)
    for path in paths:
        print(f"  - {path}", file=sys.stderr)


def serve_local_web(
    project: Project,
    *,
    deployment: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    runtime: str = "auto",
    runtime_cache: Path | None = None,
    platform_mock: bool = True,
    orientation: str = "landscape",
    inspector: bool = False,
    workbench: bool = False,
) -> None:
    inspector = inspector or workbench
    state = LocalWebProject(
        project,
        deployment,
        platform_mock=platform_mock,
        inspector=inspector,
    )
    runtime_dir = None if runtime == "remote" else current_web_runtime(runtime_cache)
    if runtime == "local" and runtime_dir is None:
        raise WorkspaceError("尚未同步 Web Runtime；请先运行 bin/tapmaker web-runtime sync")
    server = LocalWebServer(
        (host, port),
        state,
        runtime_dir,
        orientation=orientation,
        inspector=inspector,
    )
    print(f"TapMaker 本地 Web 预览：{server.url}")
    print(
        f"项目={project.name} target={state.deployment.name} entry={state.deployment.entry} "
        f"files={len(state.manifest()['files'])}"
    )
    _print_missing_meta(tuple(state.diagnostics()["missing_meta"]))
    print("源码变化后页面会自动重新加载；按 Ctrl-C 停止。")
    print(f"Runtime={'本地 ' + str(runtime_dir) if runtime_dir else '远程 CDN'}")
    print(f"平台能力={'本地 mock' if platform_mock else 'Runtime 原始能力'}")
    width, height = ORIENTATION_SIZES[orientation]
    print(f"预览方向={orientation} ({width}x{height})")
    print(f"组件树调试桥={'已启用（支持 TSCN Workbench）' if inspector else '未启用'}")
    if workbench:
        print(f"TSCN Workbench 原型={server.workbench_url}")
    if open_browser:
        webbrowser.open(server.workbench_url if workbench else server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
