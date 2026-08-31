from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import tomllib


CONTROL_NAMES = {".git", ".maker-mcp", ".project"}


class WorkspaceError(RuntimeError):
    """工作区配置或安全约束不满足。"""


@dataclass(frozen=True)
class Mount:
    source: Path
    target: Path


@dataclass(frozen=True)
class Deployment:
    name: str
    entry: str
    channel: str
    cache: Path | None
    maker_project_id: str | None = None


@dataclass(frozen=True)
class Project:
    workspace_root: Path
    root: Path
    name: str
    entry: str
    mounts: tuple[Mount, ...]
    deployments: tuple[Deployment, ...]
    default_deployment: str
    version_file: Path | None
    build_info_target: Path | None

    def deployment(self, name: str | None = None) -> Deployment:
        selected_name = name or self.default_deployment
        for item in self.deployments:
            if item.name == selected_name:
                return item
        available = ", ".join(item.name for item in self.deployments)
        raise WorkspaceError(f"未知部署目标：{selected_name}；可用目标：{available}")

    def current_version(self) -> int:
        if self.version_file is None:
            return 0
        try:
            with self.version_file.open("rb") as stream:
                state = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise WorkspaceError(f"无法读取版本文件：{self.version_file}") from error
        version = state.get("version")
        if state.get("schema") != 1 or not isinstance(version, int) or version < 0:
            raise WorkspaceError(f"版本文件无效：{self.version_file}")
        return version


def direct_project(code_root: Path, entry: str) -> Project:
    """根据本地项目目录和入口创建只读预览项目。"""
    root = code_root.expanduser().resolve()
    if not root.is_dir():
        raise WorkspaceError(f"本地项目目录不存在：{root}")
    entry_path = Path(entry)
    if entry_path.is_absolute() or not entry or ".." in entry_path.parts:
        raise WorkspaceError(f"入口必须是项目目录内的相对路径：{entry}")
    resolved_entry = (root / entry_path).resolve()
    if not resolved_entry.is_relative_to(root) or not resolved_entry.is_file():
        raise WorkspaceError(f"入口文件不存在或越出项目目录：{resolved_entry}")
    normalized_entry = entry_path.as_posix()
    namespace_source = f"{root}\0{normalized_entry}".encode("utf-8")
    local_namespace = f"local-{sha256(namespace_source).hexdigest()[:16]}"
    deployment = Deployment(
        name="local",
        entry=normalized_entry,
        channel="local",
        cache=None,
        maker_project_id=local_namespace,
    )
    return Project(
        workspace_root=root,
        root=root,
        name=local_namespace,
        entry=normalized_entry,
        mounts=(Mount(source=root, target=Path("project")),),
        deployments=(deployment,),
        default_deployment="local",
        version_file=None,
        build_info_target=None,
    )


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        config_path = self.root / "tapmaker.workspace.toml"
        if not config_path.is_file():
            raise WorkspaceError(f"找不到工作区配置：{config_path}")
        with config_path.open("rb") as stream:
            self.config = tomllib.load(stream)
        if self.config.get("schema") != 1:
            raise WorkspaceError("不支持的工作区配置版本")

    @classmethod
    def discover(cls, start: Path) -> "Workspace":
        current = start.expanduser().resolve()
        for candidate in (current, *current.parents):
            if (candidate / "tapmaker.workspace.toml").is_file():
                return cls(candidate)
        raise WorkspaceError("当前路径不在 TapMaker 工作区中")

    def project_names(self) -> tuple[str, ...]:
        projects = self.config.get("projects", {})
        if not isinstance(projects, dict):
            raise WorkspaceError("工作区 projects 必须是表")
        return tuple(sorted(str(name) for name in projects))

    def project(self, name: str) -> Project:
        projects = self.config.get("projects", {})
        project_config = projects.get(name) if isinstance(projects, dict) else None
        if not isinstance(project_config, dict):
            raise WorkspaceError(f"未知项目：{name}")
        path_value = project_config.get("path")
        if not isinstance(path_value, str):
            raise WorkspaceError(f"项目 {name} 缺少字符串 path")
        project_root = _inside(self.root, self.root / path_value, "项目路径")
        manifest_path = project_root / "tapmaker.toml"
        try:
            with manifest_path.open("rb") as stream:
                manifest = tomllib.load(stream)
        except OSError as error:
            raise WorkspaceError(f"无法读取项目清单：{manifest_path}") from error
        if manifest.get("schema") != 1 or manifest.get("name") != name:
            raise WorkspaceError(f"项目清单无效：{manifest_path}")
        entry = manifest.get("entry")
        if not isinstance(entry, str) or not entry:
            raise WorkspaceError(f"项目 entry 无效：{manifest_path}")

        mounts: list[Mount] = []
        mount_items = manifest.get("mounts", [])
        if not isinstance(mount_items, list):
            raise WorkspaceError(f"mounts 必须是数组：{manifest_path}")
        for item in mount_items:
            if not isinstance(item, dict):
                raise WorkspaceError(f"mount 必须是表：{manifest_path}")
            source_value = item.get("source")
            target_value = item.get("target")
            if not isinstance(source_value, str) or not isinstance(target_value, str):
                raise WorkspaceError(f"mount source/target 必须是字符串：{manifest_path}")
            source = _inside(self.root, project_root / source_value, "挂载源")
            target = _relative_target(target_value)
            if not source.exists():
                raise WorkspaceError(f"挂载源不存在：{source}")
            mounts.append(Mount(source=source, target=target))

        deployments, default, version_file, build_info = _deployment_config(
            self.root, project_root, manifest_path, entry, manifest.get("deploy")
        )
        return Project(
            workspace_root=self.root,
            root=project_root,
            name=name,
            entry=entry,
            mounts=tuple(mounts),
            deployments=deployments,
            default_deployment=default,
            version_file=version_file,
            build_info_target=build_info,
        )


def _inside(root: Path, candidate: Path, label: str) -> Path:
    resolved = candidate.expanduser().resolve()
    if not resolved.is_relative_to(root):
        raise WorkspaceError(f"{label}越出工作区：{resolved}")
    return resolved


def _relative_target(value: str) -> Path:
    target = Path(value)
    if target.is_absolute() or not value or ".." in target.parts or target.parts[0] in CONTROL_NAMES:
        raise WorkspaceError(f"非法挂载目标：{value}")
    return target


def _deployment_config(
    workspace_root: Path,
    project_root: Path,
    manifest_path: Path,
    base_entry: str,
    value: object,
) -> tuple[tuple[Deployment, ...], str, Path | None, Path | None]:
    if value is None:
        return (Deployment("default", base_entry, "development", None),), "default", None, None
    if not isinstance(value, dict):
        raise WorkspaceError(f"部署配置必须是表：{manifest_path}")

    cache_root_value = os.environ.get("TAPMAKER_DEPLOY_CACHE_ROOT", value.get("cache_root"))
    if cache_root_value is not None and not isinstance(cache_root_value, str):
        raise WorkspaceError(f"deploy.cache_root 必须是字符串：{manifest_path}")
    cache_root = (
        _external_cache(workspace_root, cache_root_value)
        if isinstance(cache_root_value, str) and cache_root_value
        else None
    )
    targets = value.get("targets")
    if targets is None:
        legacy = value.get("target")
        if not isinstance(legacy, str):
            raise WorkspaceError(f"部署配置必须包含 target 或 targets：{manifest_path}")
        return (
            Deployment("default", base_entry, "development", _external_cache(workspace_root, legacy, cache_root)),
        ), "default", None, None
    if not isinstance(targets, dict) or not targets:
        raise WorkspaceError(f"deploy.targets 必须是非空表：{manifest_path}")

    deployments: list[Deployment] = []
    for name, item in sorted(targets.items()):
        if not isinstance(name, str) or not isinstance(item, dict):
            raise WorkspaceError(f"部署目标配置无效：{manifest_path}")
        entry = item.get("entry", base_entry)
        channel = item.get("channel", name)
        cache_value = item.get("cache")
        project_id = item.get("maker_project_id")
        if not isinstance(entry, str) or not entry or not isinstance(channel, str) or not channel:
            raise WorkspaceError(f"部署目标 {name} 的 entry/channel 无效")
        if cache_value is not None and not isinstance(cache_value, str):
            raise WorkspaceError(f"部署目标 {name} 的 cache 必须是字符串")
        if project_id is not None and (not isinstance(project_id, str) or not project_id.strip()):
            raise WorkspaceError(f"部署目标 {name} 的 maker_project_id 无效")
        cache = _external_cache(workspace_root, cache_value, cache_root) if cache_value else None
        deployments.append(Deployment(name, entry, channel, cache, project_id))

    default = value.get("default", deployments[0].name)
    if not isinstance(default, str) or default not in {item.name for item in deployments}:
        raise WorkspaceError(f"默认部署目标无效：{default}")
    version_value = value.get("version_file")
    build_info_value = value.get("build_info")
    if not isinstance(version_value, str) or not isinstance(build_info_value, str):
        raise WorkspaceError(f"多目标部署必须配置 version_file 与 build_info：{manifest_path}")
    version_file = _inside(workspace_root, project_root / version_value, "版本文件")
    if not version_file.is_file():
        raise WorkspaceError(f"版本文件不存在：{version_file}")
    return tuple(deployments), default, version_file, _relative_target(build_info_value)


def _external_cache(root: Path, value: str, cache_root: Path | None = None) -> Path:
    configured = Path(value).expanduser()
    if not configured.is_absolute():
        if cache_root is None:
            raise WorkspaceError("相对 Maker 部署缓存必须配置 deploy.cache_root")
        configured = cache_root / configured
    cache = configured.resolve()
    if cache.is_relative_to(root):
        raise WorkspaceError("Maker 部署缓存不能位于 monorepo 内部")
    return cache


def _render_build_info(project: str, deployment: Deployment, version: int) -> str:
    def lua(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    return "\n".join(
        (
            "-- Generated by tapmaker-local-web; this file is only served from memory.",
            "return {",
            "    schema = 1,",
            f"    project = {lua(project)},",
            f"    target = {lua(deployment.name)},",
            f"    channel = {lua(deployment.channel)},",
            f"    version = {version},",
            f"    is_test = {str(deployment.channel == 'test').lower()},",
            f"    is_production = {str(deployment.channel == 'production').lower()},",
            "}",
            "",
        )
    )
