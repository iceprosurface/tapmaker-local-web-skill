from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
import uuid
import zlib

from .config import Project, WorkspaceError, _render_build_info
from .inspector import (
    COMPONENT_TREE_AGENT_PROMPT,
    LOCAL_PROJECT_ENTRY,
    compile_entry_wrapper,
)

ENGINE_RES_BASE_URL = "https://tapcode-sce.spark.xd.com/src/engine-res/"
OFFICIAL_RES_BASE_URL = "https://tapcode-sce.spark.xd.com/src/official-res/"
LOCAL_VERSION_PREFIX = "local"
LOCAL_CLIENT = "local"
LOCAL_ENGINE = "stable"

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
        inspector: bool = False,
    ):
        self.project = project
        self.deployment = project.deployment(deployment)
        self.platform_mock = platform_mock
        self.inspector = inspector
        self._lock = threading.RLock()
        self._cache: dict[str, _CachedAsset] = {}
        self._assets: dict[str, AssetRecord] = {}
        self._manifest: dict[str, object] = {}
        self._fingerprint: tuple[tuple[str, tuple[object, ...]], ...] = ()
        self._missing_meta: tuple[str, ...] = ()
        self._build_epoch = time.time_ns() // 1_000
        self._revision_changed = threading.Condition(self._lock)
        self._component_tree: dict[str, object] | None = None
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
            self._component_tree = None
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
            return {
                "revision": self.build,
                "diagnostics": self.diagnostics(),
                "inspector": {
                    "enabled": self.inspector,
                    "component_tree_ready": self._component_tree is not None,
                },
            }

    def component_tree(self) -> dict[str, object]:
        with self._lock:
            if self._component_tree is not None:
                return self._component_tree
        return {
            "root": None,
            "warning": (
                "未读取到运行时组件树。请确认使用 --inspect 启动，并且项目执行了 "
                "urhox-libs/UI.SetRoot(root)。"
            ),
            "agent_prompt": COMPONENT_TREE_AGENT_PROMPT,
        }

    def update_component_tree(self, value: object) -> None:
        if not isinstance(value, dict):
            raise WorkspaceError("组件树上报必须是 JSON 对象")
        root = value.get("root")
        if not isinstance(root, dict):
            raise WorkspaceError("组件树上报缺少 root 对象")
        with self._lock:
            self._component_tree = value

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

        if self.platform_mock or self.inspector:
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
            wrapper = compile_entry_wrapper(
                self.deployment.entry,
                platform_mock=self.platform_mock,
                inspector=self.inspector,
            )
            candidates[virtual] = (
                source,
                wrapper,
                ("generated-entry-wrapper", wrapper, signature),
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
