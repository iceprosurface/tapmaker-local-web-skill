"""Explicit, immutable offline snapshots. Only sync performs network I/O.

Objects contain unmodified upstream bytes; the loopback server translates the
one supported CDN origin in textual responses. No arbitrary URL proxy exists.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import gzip
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen
import zlib

from .config import WorkspaceError

ORIGIN = "https://tapcode-sce.spark.xd.com"
PREFIX = "/__tapmaker/offline"
SOURCES = ("engine", "engine-res", "engine-startup", "urhox-libs", "official-res")
PLAYER_PATHS = (
    "/src/web/src/index.min.js",
    "/src/web/libs/qrcode.min.js",
    "/src/web/libs/eruda.min.js",
    "/src/web/libs/mp4-muxer.mjs",
)
DEFAULT_GROUPS = ("official-shaders", "official-shadercache")
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
    "connect-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; media-src 'self' blob:; worker-src 'self' blob:; "
    "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _get(path: str, *, limit: int = 256 * 1024 * 1024) -> bytes:
    # Paths come from fixed endpoints or validated manifest identifiers, never
    # from a browser request. TLS verification stays at urllib's secure default.
    if not path.startswith("/src/") or ".." in path or "?" in path or "#" in path:
        raise WorkspaceError(f"Invalid official resource path: {path}")
    request = Request(ORIGIN + path, headers={"Accept-Encoding": "identity"})
    with urlopen(request, timeout=60) as response:
        if not response.url.startswith(ORIGIN + "/src/"):
            raise WorkspaceError("Unexpected offline download redirect")
        content = (
            gzip.GzipFile(fileobj=response)
            if response.headers.get("Content-Encoding") == "gzip"
            else response
        )
        data = content.read(limit + 1)
    if len(data) > limit:
        raise WorkspaceError(f"Offline download exceeds limit: {path}")
    return data


def _identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[\w.\-]+", value)
        or ".." in value
    ):
        raise WorkspaceError(f"Invalid manifest identifier: {value!r}")
    return value


def asset_path(source: str, item: dict) -> str:
    return f"/src/{source}/assets/{_identifier(item['uuid'])}-{_identifier(item['hash'])}{_identifier(item['ext']) if item['ext'] else ''}"


def variants(item: dict):
    """Web Runtime can request manifest platform variants (including windows)."""
    yield item
    for key, checksum in item.items():
        if key.startswith("hash@"):
            platform = key.split("@", 1)[1]
            if "size@" + platform not in item:
                raise WorkspaceError(
                    f"Missing variant size: {item.get('fs_path')}@{platform}"
                )
            yield {**item, "hash": checksum, "size": item["size@" + platform]}


def _object(root: Path, checksum: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise WorkspaceError("Invalid offline object checksum")
    return root / "objects" / checksum


def _store(root: Path, data: bytes) -> str:
    checksum = digest(data)
    path = _object(root, checksum)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or digest(path.read_bytes()) != checksum:
        temporary = path.with_suffix(".part")
        temporary.write_bytes(data)
        temporary.replace(path)
    return checksum


def _read(root: Path, checksum: str) -> bytes:
    path = _object(root, checksum)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise WorkspaceError(
            f"Offline cache missing: {checksum}; run web-offline sync again"
        ) from error
    if digest(data) != checksum:
        raise WorkspaceError(
            f"Offline cache corrupt: {checksum}; run web-offline sync again"
        )
    return data


def _selection(
    manifests: dict, texts: str, resources: list[str], groups: list[str]
) -> set[tuple[str, str]]:
    indices = {
        name: {f["uuid"]: f for f in manifest["files"]}
        for name, manifest in manifests.items()
    }
    official = indices["official-res"]
    known_groups = {g for f in official.values() for g in f.get("groups", [])}
    unknown = set(groups) - known_groups
    if unknown:
        raise WorkspaceError(f"Unknown official groups: {sorted(unknown)}")
    # Documentation is not an executable engine dependency. Keep its manifest
    # records for diagnostics, but do not prefetch HTML documentation.
    selected = {
        (name, key)
        for name in ("engine", "engine-res", "engine-startup", "urhox-libs")
        for key, item in indices[name].items()
        if item.get("ext") != ".html"
    }
    matches = set()
    literals = set(re.findall(r"[\w./\-]+", texts))
    literals.update(re.findall(r"""["']([^"'\n]+)["']""", texts))
    for key, item in official.items():
        path = item.get("fs_path", "")
        if key in resources or path in resources:
            matches.update({key, path} & set(resources))
            selected.add(("official-res", key))
        if (
            (path and path in literals)
            or key in literals
            or set(item.get("groups", [])) & set(groups)
        ):
            selected.add(("official-res", key))
    if set(resources) - matches:
        raise WorkspaceError(
            f"Unknown official resources: {sorted(set(resources) - matches)}"
        )
    # Follow explicit UUID references across the bounded, known source graph.
    pending = list(selected)
    while pending:
        source, key = pending.pop()
        item = indices[source][key]
        dependencies = [(source, ref) for ref in item.get("refs", [])]
        if item.get("source"):
            target = item["source"]
            if target not in indices:
                raise WorkspaceError(f"Unsupported manifest source: {target}")
            match = next(
                (
                    f["uuid"]
                    for f in indices[target].values()
                    if f.get("fs_path") == item.get("fs_path")
                ),
                None,
            )
            if match is None:
                raise WorkspaceError(
                    f"Unresolved source alias: {source}/{item.get('fs_path')}"
                )
            dependencies.append((target, match))
        for target, ref in dependencies:
            if ref not in indices[target]:
                candidates = [n for n in SOURCES if ref in indices[n]]
                if len(candidates) != 1:
                    raise WorkspaceError(
                        f"Unresolved resource reference: {source}/{ref}"
                    )
                target = candidates[0]
            pair = (target, ref)
            if pair not in selected:
                selected.add(pair)
                pending.append(pair)
    return selected


def sync_offline(
    root: Path,
    *,
    texts: str = "",
    resources: list[str] | None = None,
    groups: list[str] | None = None,
    max_bytes: int = 512 * 1024 * 1024,
) -> str:
    """Prepare one complete selected snapshot, then atomically publish its ID.

    The default is engine runtime resources (excluding HTML documentation), official shaders, and literal project
    references. Dynamic official resource names require explicit selectors.
    """
    groups = list(DEFAULT_GROUPS) + (groups or [])
    versions, manifests, files = {}, {}, {}

    def add(path: str, data: bytes, source_path: str | None = None) -> None:
        files[path] = {
            "sha256": _store(root, data),
            "size": len(data),
            "url": ORIGIN + path,
            "source_url": ORIGIN + (source_path or path),
        }

    for name in SOURCES:
        tag = "latest" if name == "engine" else "stable"
        if name == "engine-res":
            abi = ".".join(versions["engine"]["version"].split(".")[:2])
            tag = f"wasm-{abi}"
        version_path = f"/src/{name}/{tag}.json"
        version_data = _get(version_path, limit=1024 * 1024)
        version = json.loads(version_data)
        ver, client = _identifier(version["version"]), _identifier(version["client"])
        path = f"/src/{name}/{ver}/manifest-{client}.json"
        manifest_data = _get(path, limit=32 * 1024 * 1024)
        manifest = json.loads(manifest_data)
        if set(manifest.get("sources", {})) - set(SOURCES):
            raise WorkspaceError(f"Unsupported dependencies in {name}")
        versions[name], manifests[name] = version, manifest
        add(path, manifest_data)
        add(version_path, version_data)
        for alias in ("stable.json", "latest.json", f"{ver}/version.json"):
            add(f"/src/{name}/{alias}", version_data, version_path)

    selected = _selection(manifests, texts, resources or [], groups)
    downloads = {}
    for name, manifest in manifests.items():
        for item in manifest["files"]:
            if (name, item["uuid"]) not in selected or item.get("source"):
                continue
            if "size" not in item and item.get("hash") != "00000000":
                raise WorkspaceError(
                    f"Missing resource size: {name}/{item.get('fs_path')}"
                )
            for variant in variants(item):
                downloads[asset_path(name, variant)] = variant
    total = sum(item.get("size", 0) for item in downloads.values())
    if max_bytes <= 0 or total > max_bytes:
        raise WorkspaceError(
            f"Offline selection is {total} bytes ({len(downloads)} assets), exceeds --max-bytes {max_bytes}"
        )
    print(
        f"Offline selection: {len(downloads)} assets, {total} bytes; official {sum(n == 'official-res' for n, _ in selected)}",
        flush=True,
    )

    # Reuse only verified raw content. The URL index is a hint, never authority.
    index_path = root / "downloads.json"
    try:
        old_index = json.loads(index_path.read_text())
    except (OSError, ValueError):
        old_index = {}

    def download(pair: tuple[str, dict]) -> tuple[str, bytes]:
        path, item = pair
        data = None
        if path in old_index:
            try:
                data = _read(root, old_index[path])
            except (WorkspaceError, TypeError):
                pass
        size = item.get("size", 0)
        if data is None:
            data = (
                b""
                if size == 0 and item["hash"] == "00000000"
                else _get(path, limit=size)
            )
        if len(data) != size or f"{zlib.crc32(data) & 0xFFFFFFFF:08x}" != item["hash"]:
            raise WorkspaceError(f"Offline resource checksum mismatch: {path}")
        return path, data

    with ThreadPoolExecutor(max_workers=8) as pool:
        for path, data in pool.map(download, downloads.items()):
            add(path, data)
            old_index[path] = files[path]["sha256"]
            if len(old_index) % 32 == 0:
                journal = root / "downloads.part"
                journal.write_bytes(json_bytes(old_index))
                journal.replace(index_path)
    index_path.write_bytes(json_bytes(old_index))
    for path in PLAYER_PATHS:
        add(path, _get(path, limit=8 * 1024 * 1024))
    # Immutable bundle ID pins the Player digest, every source version and the
    # resource selection together. No floating CDN tags are read when serving.
    bundle = {
        "format": 1,
        "origin": ORIGIN,
        "versions": versions,
        "files": files,
        "selected": sorted([list(pair) for pair in selected]),
        "selection": {
            "groups": sorted(set(groups)),
            "resources": sorted(resources or []),
        },
    }
    body = json_bytes(bundle)
    bundle_id = digest(body)
    snapshots = root / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / f"{bundle_id}.json").write_bytes(body)
    temporary = root / "current.part"
    temporary.write_text(bundle_id + "\n")
    temporary.replace(root / "current")
    return bundle_id


class OfflineBundle:
    def __init__(self, root: Path, bundle_id: str | None = None):
        self.root = root
        try:
            self.id = bundle_id or (root / "current").read_text().strip()
            _object(root, self.id)  # Validate before constructing a path.
            body = (root / "snapshots" / f"{self.id}.json").read_bytes()
            if digest(body) != self.id:
                raise WorkspaceError("Offline snapshot metadata checksum mismatch")
            self.metadata = json.loads(body)
            if self.metadata["format"] != 1 or self.metadata["origin"] != ORIGIN:
                raise WorkspaceError("Unsupported offline snapshot format/origin")
            self.files = self.metadata["files"]
            for path, item in self.files.items():
                if (
                    not path.startswith("/src/")
                    or ".." in path
                    or item["url"] != ORIGIN + path
                ):
                    raise WorkspaceError(f"Invalid offline source: {path}")
                if len(_read(root, item["sha256"])) != item["size"]:
                    raise WorkspaceError(f"Offline size mismatch: {path}")
            self.runtime_paths = {}
            selected = {tuple(pair) for pair in self.metadata["selected"]}
            for name in SOURCES:
                v = self.metadata["versions"][name]
                stem = f"/src/{name}/"
                version = json.loads(self.raw(stem + "stable.json"))
                if version != v or json.loads(self.raw(stem + "latest.json")) != v:
                    raise WorkspaceError(f"Offline version mismatch: {name}")
                manifest = json.loads(
                    self.raw(f"{stem}{v['version']}/manifest-{v['client']}.json")
                )
                if json.loads(self.raw(f"{stem}{v['version']}/version.json")) != v:
                    raise WorkspaceError(f"Offline version mismatch: {name}")
                for f in manifest["files"]:
                    if (name, f["uuid"]) in selected and not f.get("source"):
                        for variant in variants(f):
                            raw = self.raw(asset_path(name, variant))
                            if (
                                len(raw) != variant.get("size", 0)
                                or f"{zlib.crc32(raw) & 0xFFFFFFFF:08x}"
                                != variant["hash"]
                            ):
                                raise WorkspaceError(
                                    f"Offline manifest checksum mismatch: {name}/{f['fs_path']}"
                                )
                if name == "engine":
                    for f in manifest["files"]:
                        if f["fs_path"] in (
                            "UrhoXRuntime.js",
                            "UrhoXRuntime.wasm",
                            "UrhoXRuntime.data",
                        ):
                            self.runtime_paths[f["fs_path"]] = asset_path(name, f)
            abi = ".".join(
                self.metadata["versions"]["engine"]["version"].split(".")[:2]
            )
            if (
                json.loads(self.raw(f"/src/engine-res/wasm-{abi}.json"))
                != self.metadata["versions"]["engine-res"]
            ):
                raise WorkspaceError("Offline WASM ABI version mismatch")
            if len(self.runtime_paths) != 3:
                raise WorkspaceError("Offline Runtime incomplete")
            for path in (*PLAYER_PATHS, *self.runtime_paths.values()):
                self.raw(path)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise WorkspaceError(
                f"Offline snapshot unavailable/invalid: {error}; run web-offline sync"
            ) from error

    def raw(self, path: str) -> bytes:
        item = self.files.get(path)
        if item is None:
            raise WorkspaceError(
                f"Offline resource not prepared: {path}; resync with --official-resource or --official-group"
            )
        return _read(self.root, item["sha256"])

    def render(self, path: str, base: str) -> bytes:
        data = self.raw(path)
        if path.endswith((".js", ".mjs", ".json")):
            data = data.replace(ORIGIN.encode(), base.encode())
            if path.endswith(".json") and "/manifest-" in path:
                manifest = json.loads(data)
                # Paks can contain unselected assets; use the verified per-file
                # graph, without implicitly fetching multi-gigabyte archives.
                manifest.pop("assets_pak", None)
                manifest.pop("paks", None)
                # Documentation is excluded from the executable offline graph,
                # including default-group preload lists derived by the Runtime.
                if not path.startswith("/src/official-res/"):
                    manifest["files"] = [f for f in manifest["files"] if f.get("ext") != ".html"]
                data = json_bytes(manifest)
        return data
