from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import json
import mimetypes
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import uuid
import webbrowser
import zlib

from watchfiles import watch

from .config import Project, WorkspaceError, _render_build_info


ENGINE_BASE_URL = "https://tapcode-sce.spark.xd.com/src/engine/"
ENGINE_RES_BASE_URL = "https://tapcode-sce.spark.xd.com/src/engine-res/"
OFFICIAL_RES_BASE_URL = "https://tapcode-sce.spark.xd.com/src/official-res/"
PLAYER_SCRIPT_URL = "https://tapcode-sce.spark.xd.com/src/web/src/index.min.js"
LOCAL_VERSION_PREFIX = "local"
LOCAL_CLIENT = "local"
LOCAL_ENGINE = "stable"
RUNTIME_FILES = ("UrhoXRuntime.js", "UrhoXRuntime.wasm", "UrhoXRuntime.data")
LOCAL_PROJECT_ENTRY = "__tapmaker_project_entry.lua"
LOCAL_USER_ID = 900000001
DYNAMIC_CACHE_CONTROL = "no-store"
RUNTIME_CACHE_CONTROL = "private, no-cache"
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
ORIENTATION_SIZES = {
    "landscape": (844, 390),
    "portrait": (390, 844),
}

BLOCKING_EXTENSIONS = {
    ".lua",
    ".json",
    ".xml",
    ".material",
    ".prefab",
    ".effect",
    ".fsm",
    ".blendspace",
}


def _is_operating_system_metadata(relative: Path) -> bool:
    for part in relative.parts:
        if part in {".git", ".maker-mcp", ".project", ".venv", "node_modules", "__pycache__"}:
            return True
        if part.startswith(".") and not part.startswith("._"):
            return True
        if part in {".DS_Store", "Thumbs.db", "desktop.ini", "__MACOSX"}:
            return True
        if part.startswith("._"):
            return True
    return False


def web_runtime_cache_root() -> Path:
    override = os.environ.get("TAPMAKER_WEB_RUNTIME_CACHE")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/TapMaker/web-runtime"
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return base / "tapmaker/web-runtime"


def current_web_runtime(cache_root: Path | None = None) -> Path | None:
    root = cache_root or web_runtime_cache_root()
    try:
        marker = json.loads((root / "current.json").read_text(encoding="utf-8"))
        relative = marker.get("directory")
        if not isinstance(relative, str) or Path(relative).name != relative:
            return None
        runtime = root / relative
        if all((runtime / name).is_file() for name in RUNTIME_FILES):
            return runtime
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _runtime_etags(runtime_dir: Path | None) -> dict[str, str]:
    if runtime_dir is None:
        return {}
    hashes: dict[str, str] = {}
    try:
        metadata = json.loads((runtime_dir / "runtime.json").read_text(encoding="utf-8"))
        files = metadata.get("files", {})
        if isinstance(files, dict):
            for name in RUNTIME_FILES:
                item = files.get(name)
                checksum = item.get("hash") if isinstance(item, dict) else None
                if (
                    isinstance(checksum, str)
                    and len(checksum) == 8
                    and all(character in "0123456789abcdefABCDEF" for character in checksum)
                ):
                    hashes[name] = f'"runtime-{checksum.lower()}"'
    except (OSError, json.JSONDecodeError):
        pass
    for name in RUNTIME_FILES:
        if name in hashes:
            continue
        try:
            stat = (runtime_dir / name).stat()
        except OSError:
            continue
        hashes[name] = f'W/"runtime-{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    return hashes


def sync_web_runtime(
    cache_root: Path | None = None,
    *,
    engine_base_url: str = ENGINE_BASE_URL,
) -> Path:
    root = cache_root or web_runtime_cache_root()
    base = engine_base_url.rstrip("/") + "/"
    latest = _read_json_url(base + "latest.json")
    version = str(latest.get("version") or "")
    client = str(latest.get("client") or "")
    if not version or not client:
        raise WorkspaceError("Web Runtime latest.json 缺少 version/client")
    manifest = _read_json_url(f"{base}{version}/manifest-{client}.json")
    files = {
        item.get("fs_path"): item
        for item in manifest.get("files", [])
        if isinstance(item, dict) and item.get("fs_path") in RUNTIME_FILES
    }
    missing = [name for name in RUNTIME_FILES if name not in files]
    if missing:
        raise WorkspaceError(f"Web Runtime 清单缺少文件：{', '.join(missing)}")

    runtime = root / f"{version}-{client}"
    runtime.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_FILES:
        item = files[name]
        extension = str(item.get("ext") or Path(name).suffix)
        asset_name = f"{item['uuid']}-{item['hash']}{extension}"
        destination = runtime / name
        expected_size = int(item["size"])
        if destination.is_file() and destination.stat().st_size == expected_size:
            continue
        temporary = runtime / f".{name}.part"
        request = Request(base + "assets/" + asset_name, headers={"Accept-Encoding": "identity"})
        try:
            with urlopen(request) as response, temporary.open("wb") as output:
                content = (
                    gzip.GzipFile(fileobj=response)
                    if response.headers.get("Content-Encoding") == "gzip"
                    else response
                )
                shutil.copyfileobj(content, output)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
        data = temporary.read_bytes()
        actual_hash = f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
        if len(data) != expected_size or actual_hash != str(item["hash"]):
            temporary.unlink(missing_ok=True)
            raise WorkspaceError(f"Web Runtime 文件校验失败：{name}")
        temporary.replace(destination)

    metadata = {"version": version, "client": client, "files": files}
    (runtime / "runtime.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "current.json").write_text(
        json.dumps({"directory": runtime.name}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return runtime


def _read_json_url(url: str) -> dict[str, object]:
    request = Request(url, headers={"Accept-Encoding": "identity"})
    with urlopen(request) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise WorkspaceError(f"Web Runtime 返回的不是 JSON 对象：{url}")
    return value

INDEX_HTML = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>TapMaker 本地预览</title>
  <style>
    :root {{
      --tapmaker-viewport-width: __TAPMAKER_VIEWPORT_WIDTH__;
      --tapmaker-viewport-height: __TAPMAKER_VIEWPORT_HEIGHT__;
    }}
    html, body {{ margin: 0; width: 100%; height: 100%; overflow: hidden; background: #080808; }}
    #canvas {{
      position: fixed !important; left: 50% !important; top: 50% !important;
      width: min(calc(var(--tapmaker-viewport-width) * 1px), 100vw,
        calc(100vh * var(--tapmaker-viewport-width) / var(--tapmaker-viewport-height))) !important;
      height: min(calc(var(--tapmaker-viewport-height) * 1px), 100vh,
        calc(100vw * var(--tapmaker-viewport-height) / var(--tapmaker-viewport-width))) !important;
      transform: translate(-50%, -50%) !important;
      border: 0; outline: 0; box-shadow: 0 0 0 1px #282828;
    }}
    #loading-screen {{ position: fixed; inset: 0; z-index: 10000; display: flex;
      align-items: center; justify-content: center; flex-direction: column; gap: 14px;
      color: #fff; background: #080808; font: 15px system-ui, sans-serif; }}
    #loading-screen.hidden {{ display: none; }}
    #loading-progress-bg {{ width: min(560px, 70vw); height: 8px; background: #333; }}
    #loading-progress-bar {{ width: 0; height: 100%; background: #4a9eff; }}
    #dialog-overlay {{ position: fixed; inset: 0; z-index: 20000; display: none;
      align-items: center; justify-content: center; background: rgba(0,0,0,.75); }}
    #dialog-overlay.visible {{ display: flex; }}
    #dialog-box {{ min-width: 320px; padding: 24px; color: #fff; background: #353545;
      font: 16px system-ui, sans-serif; text-align: center; }}
    #dialog-cancel.hidden {{ display: none; }}
    #tapmaker-diagnostics {{ position: fixed; top: 10px; left: 50%; z-index: 11000;
      display: none; width: min(720px, calc(100vw - 20px)); max-height: 35vh;
      box-sizing: border-box; overflow: auto; padding: 10px 14px; border: 1px solid #f7b955;
      border-radius: 6px; color: #fff3d6; background: rgba(91, 52, 0, .94);
      font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
      transform: translateX(-50%); white-space: pre-wrap; }}
    #tapmaker-diagnostics.visible {{ display: block; }}
  </style>
</head>
<body oncontextmenu="return false">
  <div id="loading-screen">
    <div id="loading-status">正在启动本地预览…</div>
    <div id="loading-progress-bg"><div id="loading-progress-bar"></div></div>
    <div id="loading-percent">0%</div>
  </div>
  <canvas id="canvas" tabindex="0"></canvas>
  <div id="version-info"></div>
  <div id="tapmaker-diagnostics" role="status"></div>
  <div id="dialog-overlay"><div id="dialog-box">
    <h2 id="dialog-title"></h2><p id="dialog-message"></p>
    <button id="dialog-confirm">重新加载</button><button id="dialog-cancel" class="hidden"></button>
  </div></div>
  <script>
    (() => {{
      let revision = null;
      const diagnostics = document.getElementById('tapmaker-diagnostics');

      function applyRevision(next) {{
        if (revision === null) revision = next;
        else if (next !== revision) location.reload();
      }}

      function renderDiagnostics(status) {{
        const missing = status?.diagnostics?.missing_meta || [];
        if (!missing.length) {{
          diagnostics.classList.remove('visible');
          diagnostics.textContent = '';
          return;
        }}
        diagnostics.textContent =
          `缺少 .meta（${{missing.length}}）\\n` + missing.map(path => `• ${{path}}`).join('\\n');
        diagnostics.classList.add('visible');
      }}

      async function loadStatus() {{
        const response = await fetch('/__tapmaker/status', {{ cache: 'no-store' }});
        const status = await response.json();
        renderDiagnostics(status);
        applyRevision(status.revision);
      }}

      async function pollRevision() {{
        try {{
          const response = await fetch('/__tapmaker/revision', {{ cache: 'no-store' }});
          const next = await response.json();
          applyRevision(next.revision);
        }} catch (_) {{
        }} finally {{
          setTimeout(pollRevision, 1000);
        }}
      }}

      loadStatus().catch(() => {{}}).finally(() => {{
        if ('EventSource' in window) {{
          const events = new EventSource('/__tapmaker/events');
          events.addEventListener('revision', event => {{
            try {{ applyRevision(JSON.parse(event.data).revision); }} catch (_) {{}}
          }});
        }} else {{
          pollRevision();
        }}
      }});
    }})();
  </script>
  <script src="{PLAYER_SCRIPT_URL}"></script>
</body>
</html>
""".encode("utf-8")


def _index_html(orientation: str) -> bytes:
    width, height = ORIENTATION_SIZES[orientation]
    return INDEX_HTML.replace(
        b"__TAPMAKER_VIEWPORT_WIDTH__", str(width).encode()
    ).replace(
        b"__TAPMAKER_VIEWPORT_HEIGHT__", str(height).encode()
    )


@dataclass(frozen=True)
class AssetRecord:
    virtual_path: str
    fs_path: str
    source: Path | None
    content: bytes | None
    uuid: str
    extension: str
    crc32: str
    size: int
    groups: tuple[str, ...]

    @property
    def asset_name(self) -> str:
        return f"{self.uuid}-{self.crc32}{self.extension}"

    def read(self) -> bytes:
        if self.content is not None:
            return self.content
        assert self.source is not None
        return self.source.read_bytes()

    def manifest_entry(self) -> dict[str, object]:
        return {
            "uuid": self.uuid,
            "ext": self.extension,
            "hash": self.crc32,
            "size": self.size,
            "groups": list(self.groups),
            "fs_path": self.fs_path,
        }


@dataclass(frozen=True)
class _CachedAsset:
    signature: tuple[object, ...]
    record: AssetRecord


@dataclass(frozen=True)
class _CandidateCollection:
    files: tuple[tuple[str, Path | None, bytes | None, tuple[object, ...]], ...]
    missing_meta: tuple[str, ...]


class LocalWebProject:
    """将 TapMaker 挂载树暴露为 UrhoX Web Player 可读取的本地清单。"""

    def __init__(
        self,
        project: Project,
        deployment: str | None = None,
        *,
        platform_mock: bool = True,
    ):
        self.project = project
        self.deployment = project.deployment(deployment)
        self.platform_mock = platform_mock
        self._lock = threading.RLock()
        self._cache: dict[str, _CachedAsset] = {}
        self._assets: dict[str, AssetRecord] = {}
        self._manifest: dict[str, object] = {}
        self._fingerprint: tuple[tuple[str, tuple[object, ...]], ...] = ()
        self._missing_meta: tuple[str, ...] = ()
        self._build_epoch = time.time_ns() // 1_000
        self._revision_changed = threading.Condition(self._lock)
        self.revision = 0
        self.refresh()

    @property
    def project_id(self) -> str:
        project_info = self._project_info()
        return str(project_info.get("project_id") or self.deployment.maker_project_id or self.project.name)

    def refresh(self) -> bool:
        with self._lock:
            collection = self._collect_candidates()
            candidates = collection.files
            fingerprint = tuple((path, signature) for path, _, _, signature in candidates)
            if fingerprint == self._fingerprint:
                return False

            records: list[AssetRecord] = []
            cache: dict[str, _CachedAsset] = {}
            fs_paths: set[str] = set()
            asset_names: set[str] = set()
            for virtual_path, source, content, signature in candidates:
                cached = self._cache.get(virtual_path)
                if cached is not None and cached.signature == signature:
                    record = cached.record
                else:
                    record = self._build_record(virtual_path, source, content)
                if record.fs_path in fs_paths:
                    raise WorkspaceError(f"本地 Web 资源路径冲突：{record.fs_path}")
                if record.asset_name in asset_names:
                    raise WorkspaceError(f"本地 Web 资源标识冲突：{record.asset_name}")
                fs_paths.add(record.fs_path)
                asset_names.add(record.asset_name)
                records.append(record)
                cache[virtual_path] = _CachedAsset(signature, record)

            records.sort(key=lambda item: item.fs_path)
            self._cache = cache
            self._assets = {record.asset_name: record for record in records}
            self._manifest = self._build_manifest(records)
            self._fingerprint = fingerprint
            self._missing_meta = collection.missing_meta
            self.revision += 1
            self._revision_changed.notify_all()
            return True

    def manifest(self) -> dict[str, object]:
        with self._lock:
            return self._manifest

    def asset(self, name: str) -> AssetRecord | None:
        with self._lock:
            return self._assets.get(name)

    def latest(self) -> dict[str, object]:
        return {
            "format": 1,
            "version": self.version,
            "build": self.build,
            "client": self.client,
            "server": self.client,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "engine": LOCAL_ENGINE,
        }

    def diagnostics(self) -> dict[str, object]:
        with self._lock:
            missing = list(self._missing_meta)
        return {"missing_meta": missing, "missing_meta_count": len(missing)}

    def status(self) -> dict[str, object]:
        with self._lock:
            return {"revision": self.build, "diagnostics": self.diagnostics()}

    def wait_for_revision(self, previous: int, timeout: float) -> int:
        with self._revision_changed:
            self._revision_changed.wait_for(lambda: self.build != previous, timeout=timeout)
            return self.build

    @property
    def build(self) -> int:
        return self._build_epoch + self.revision

    @property
    def version(self) -> str:
        return LOCAL_VERSION_PREFIX

    @property
    def client(self) -> str:
        return f"{LOCAL_CLIENT}-{self.build}"

    def project_info(self) -> dict[str, object]:
        info = self._project_info()
        info["entry"] = self.deployment.entry
        info["version"] = self.version
        return info

    def settings_source(self) -> Path | None:
        cache = self.deployment.cache
        if cache is None:
            return None
        candidate = cache / ".project/settings.json"
        return candidate if candidate.is_file() else None

    def _collect_candidates(
        self,
    ) -> _CandidateCollection:
        candidates: dict[str, tuple[Path | None, bytes | None, tuple[object, ...]]] = {}
        missing_meta: set[str] = set()
        for mount in self.project.mounts:
            sources = (
                sorted(path for path in mount.source.rglob("*") if path.is_file())
                if mount.source.is_dir()
                else [mount.source]
            )
            for source in sources:
                relative = source.relative_to(mount.source) if mount.source.is_dir() else Path()
                if _is_operating_system_metadata(relative):
                    continue
                virtual = (mount.target / relative).as_posix()
                if virtual.endswith(".meta"):
                    continue
                if virtual in candidates:
                    raise WorkspaceError(f"本地 Web 挂载冲突：{virtual}")
                candidates[virtual] = (source, None, self._source_signature(source))
                if not source.with_name(f"{source.name}.meta").is_file():
                    missing_meta.add(self._filesystem_path(virtual))

        if self.project.build_info_target is not None:
            virtual = self.project.build_info_target.as_posix()
            content = _render_build_info(
                self.project.name,
                self.deployment,
                self.project.current_version(),
            ).encode("utf-8")
            source = candidates.get(virtual, (None, None, ()))[0]
            meta_signature = self._meta_signature(source)
            candidates[virtual] = (source, content, ("generated", content, meta_signature))

        settings_source = self.settings_source()
        if settings_source is None:
            settings = {
                "sources": {
                    "engine": {"tag": "stable"},
                    "engine-res": {"tag": "stable"},
                    "official-res": {"tag": "stable"},
                },
                "build": {
                    "generate_fs_path": True,
                    "asset_dirs": ["../assets", "../scripts"],
                    "asset_ignores": [],
                },
                "@runtime": {},
            }
            content = (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            candidates["settings.json"] = (None, content, ("generated-settings", content))
        else:
            candidates["settings.json"] = (
                settings_source,
                None,
                self._source_signature(settings_source),
            )

        if self.platform_mock:
            if LOCAL_PROJECT_ENTRY in candidates:
                raise WorkspaceError(
                    f"本地平台 mock 的源码别名与项目资源冲突：{LOCAL_PROJECT_ENTRY}"
                )
            matching = [
                virtual
                for virtual in candidates
                if self._filesystem_path(virtual) == self.deployment.entry
            ]
            if len(matching) != 1:
                raise WorkspaceError(
                    f"无法唯一定位本地平台 mock 的 Lua 入口：{self.deployment.entry}"
                )
            virtual = matching[0]
            source, original_content, signature = candidates[virtual]
            content = (
                original_content
                if original_content is not None
                else source.read_bytes()  # type: ignore[union-attr]
            )
            candidates[LOCAL_PROJECT_ENTRY] = (
                None,
                content,
                ("generated-project-entry", content, signature),
            )
            wrapper = self._platform_mock_source()
            candidates[virtual] = (
                source,
                wrapper,
                ("generated-platform-mock", wrapper, signature),
            )

        return _CandidateCollection(
            files=tuple(
                (virtual, source, content, signature)
                for virtual, (source, content, signature) in sorted(candidates.items())
            ),
            missing_meta=tuple(sorted(missing_meta)),
        )

    def _source_signature(self, source: Path) -> tuple[object, ...]:
        stat = source.stat()
        return (stat.st_mtime_ns, stat.st_size, self._meta_signature(source))

    @staticmethod
    def _meta_signature(source: Path | None) -> tuple[int, int] | None:
        if source is None:
            return None
        meta = source.with_name(f"{source.name}.meta")
        if not meta.is_file():
            return None
        stat = meta.stat()
        return (stat.st_mtime_ns, stat.st_size)

    def _build_record(
        self,
        virtual_path: str,
        source: Path | None,
        content: bytes | None,
    ) -> AssetRecord:
        data = content if content is not None else source.read_bytes()  # type: ignore[union-attr]
        extension = Path(virtual_path).suffix.lower()
        groups = ["default"]
        if extension in BLOCKING_EXTENSIONS:
            groups.append("#blocking")
        if virtual_path == "settings.json":
            groups.insert(0, "#config")
        fs_path = self._filesystem_path(virtual_path)
        return AssetRecord(
            virtual_path=virtual_path,
            fs_path=fs_path,
            source=source,
            content=content,
            uuid=self._resource_uuid(virtual_path, source),
            extension=extension,
            crc32=f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
            size=len(data),
            groups=tuple(groups),
        )

    def _filesystem_path(self, virtual_path: str) -> str:
        path = Path(virtual_path)
        prefixes = {mount.target.parts[0] for mount in self.project.mounts}
        if path.parts and path.parts[0] in prefixes and len(path.parts) > 1:
            return Path(*path.parts[1:]).as_posix()
        return path.as_posix()

    def _resource_uuid(self, virtual_path: str, source: Path | None) -> str:
        if source is not None:
            meta = source.with_name(f"{source.name}.meta")
            try:
                value = json.loads(meta.read_text(encoding="utf-8")).get("uuid")
                if isinstance(value, str) and value:
                    return value
            except (OSError, json.JSONDecodeError):
                pass
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tapmaker-local:{self.project.name}:{virtual_path}"))

    def _build_manifest(self, records: list[AssetRecord]) -> dict[str, object]:
        total_size = sum(record.size for record in records)
        blocking = [record for record in records if "#blocking" in record.groups]
        config = [record for record in records if "#config" in record.groups]
        prefixes = list(dict.fromkeys(mount.target.parts[0] for mount in self.project.mounts))
        return {
            "format": 1,
            "target": "client",
            "project_id": self.project_id,
            "sources": {
                "engine-res": {"tag": "stable", "base_url": ENGINE_RES_BASE_URL},
                "official-res": {"tag": "stable", "base_url": OFFICIAL_RES_BASE_URL},
            },
            "asset_prefixes": prefixes,
            "entry": self.deployment.entry,
            "preload_groups": ["#config", "#config#refs"],
            "files": [record.manifest_entry() for record in records],
            "metadata": {
                "total_files": len(records),
                "local_files": len(records),
                "remote_files": 0,
                "total_size": total_size,
                "preload_files": len(config),
                "preload_size": sum(record.size for record in config),
                "groups": {
                    "#blocking": {
                        "files": len(blocking),
                        "size": sum(record.size for record in blocking),
                    },
                    "default": {"files": len(records), "size": total_size},
                    "#config": {
                        "files": len(config),
                        "size": sum(record.size for record in config),
                    },
                },
            },
        }

    def _platform_mock_source(self) -> bytes:
        entry = self.deployment.entry
        if not entry.endswith(".lua"):
            raise WorkspaceError(f"本地平台 mock 只支持 Lua 入口：{entry}")
        module = LOCAL_PROJECT_ENTRY.removesuffix(".lua")
        source = f'''-- Generated by tapmaker web. This file is never written to the project.
local localUserId = {LOCAL_USER_ID}

lobby = {{}}
function lobby.GetMyUserId()
    return localUserId
end

local cloudValues = {{}}
clientCloud = {{}}
function clientCloud:Get(key, events)
    if events and events.ok then events.ok({{ [key] = cloudValues[key] }}, {{}}) end
end
function clientCloud:Set(key, value, events)
    cloudValues[key] = value
    if events and events.ok then events.ok() end
end

function GetUserNickname(options)
    if options and options.onSuccess then
        options.onSuccess({{ {{ userId = localUserId, nickname = "本地测试玩家" }} }})
    end
end

print("[tapmaker-local] platform mock user_id=" .. tostring(localUserId))
require("{module}")
'''
        return source.encode("utf-8")

    def _project_info(self) -> dict[str, object]:
        cache = self.deployment.cache
        if cache is not None:
            candidate = cache / ".project/project.json"
            try:
                value = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "project_id": self.deployment.maker_project_id or self.project.name,
            "name": self.project.name,
        }


class LocalWebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        state: LocalWebProject,
        runtime_dir: Path | None = None,
        *,
        orientation: str = "landscape",
    ):
        if orientation not in ORIENTATION_SIZES:
            available = ", ".join(ORIENTATION_SIZES)
            raise WorkspaceError(f"未知预览方向：{orientation}；可用方向：{available}")
        self.state = state
        self.runtime_dir = runtime_dir
        self.orientation = orientation
        self.index_html = _index_html(orientation)
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

    def _handle(self, *, head_only: bool) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/":
                self._send(200, self.server.index_html, "text/html; charset=utf-8", head_only)
                return
            if path == "/__tapmaker/revision":
                self._send_json({"revision": self.server.state.build}, head_only)
                return
            if path == "/__tapmaker/status":
                self._send_json(self.server.state.status(), head_only)
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
) -> None:
    state = LocalWebProject(project, deployment, platform_mock=platform_mock)
    runtime_dir = None if runtime == "remote" else current_web_runtime(runtime_cache)
    if runtime == "local" and runtime_dir is None:
        raise WorkspaceError("尚未同步 Web Runtime；请先运行 bin/tapmaker web-runtime sync")
    server = LocalWebServer((host, port), state, runtime_dir, orientation=orientation)
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
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
