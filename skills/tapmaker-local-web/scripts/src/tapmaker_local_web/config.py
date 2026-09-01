from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import tomllib
from typing import Sequence


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


def direct_project(code_root: Path | Sequence[Path], entry: str) -> Project:
    """根据一个或多个本地资源目录和入口创建只读预览项目。"""
    requested_roots = (code_root,) if isinstance(code_root, Path) else tuple(code_root)
    if not requested_roots:
        raise WorkspaceError("至少需要一个本地项目目录")
    requested = tuple(sorted((path.expanduser().resolve() for path in requested_roots), key=str))
    for directory in requested:
        if not directory.is_dir():
            raise WorkspaceError(f"本地项目目录不存在：{directory}")
    for index, left in enumerate(requested):
        for right in requested[index + 1 :]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise WorkspaceError(f"本地项目目录不能重复或互相包含：{left}、{right}")

    root = requested[0] if len(requested) == 1 else Path(os.path.commonpath(requested))
    entry_path = Path(entry)
    if entry_path.is_absolute() or not entry or ".." in entry_path.parts:
        raise WorkspaceError(f"入口必须是项目目录内的相对路径：{entry}")
    roots = _direct_resource_roots(root, requested)
    entry_candidates = {
        candidate.resolve()
        for candidate in (root / entry_path, *(directory / entry_path for directory in roots))
        if candidate.resolve().is_file()
        and any(candidate.resolve().is_relative_to(directory) for directory in roots)
    }
    if len(entry_candidates) != 1:
        reason = "存在歧义" if entry_candidates else "不存在或越出已选资源目录"
        raise WorkspaceError(f"入口文件{reason}：{entry}")
    resolved_entry = entry_candidates.pop()
    entry_roots = [
        code_directory
        for code_directory in roots
        if resolved_entry.is_relative_to(code_directory)
    ]
    if len(entry_roots) != 1:
        raise WorkspaceError(f"入口文件无法唯一归属 Maker 资源根：{resolved_entry}")
    normalized_entry = resolved_entry.relative_to(entry_roots[0]).as_posix()
    namespace_source = "\0".join(
        (*[str(code_directory) for code_directory in roots], normalized_entry)
    ).encode("utf-8")
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
        mounts=tuple(
            Mount(
                source=code_directory,
                target=(
                    Path("project")
                    if code_directory == root
                    else Path(code_directory.name)
                ),
            )
            for code_directory in roots
        ),
        deployments=(deployment,),
        default_deployment="local",
        version_file=None,
        build_info_target=None,
    )


def _direct_resource_roots(
    project_root: Path,
    requested_roots: tuple[Path, ...],
) -> tuple[Path, ...]:
    if len(requested_roots) > 1:
        roots = requested_roots
    else:
        configured = _maker_asset_dirs(project_root)
        conventional = tuple(
            directory
            for directory in (project_root / "assets", project_root / "scripts")
            if directory.is_dir()
        )
        roots = configured or conventional or requested_roots

    roots = tuple(sorted(roots, key=str))
    names: set[str] = set()
    for index, left in enumerate(roots):
        if not left.is_dir():
            raise WorkspaceError(f"Maker 资源目录不存在：{left}")
        if left != project_root and left.name in names:
            raise WorkspaceError(f"Maker 资源目录名称重复，无法建立独立资源根：{left.name}")
        names.add(left.name)
        for right in roots[index + 1 :]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise WorkspaceError(f"Maker 资源目录不能重复或互相包含：{left}、{right}")
    return roots


def _maker_asset_dirs(project_root: Path) -> tuple[Path, ...]:
    settings_path = project_root / ".project/settings.json"
    if not settings_path.is_file():
        return ()
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"无法读取 Maker 构建配置：{settings_path}") from error
    build = settings.get("build") if isinstance(settings, dict) else None
    asset_dirs = build.get("asset_dirs") if isinstance(build, dict) else None
    if asset_dirs is None:
        return ()
    if not isinstance(asset_dirs, list) or not asset_dirs or not all(
        isinstance(value, str) and value for value in asset_dirs
    ):
        raise WorkspaceError(f"Maker build.asset_dirs 无效：{settings_path}")
    return tuple(
        _inside(project_root, settings_path.parent / value, "Maker 资源目录")
        for value in asset_dirs
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
