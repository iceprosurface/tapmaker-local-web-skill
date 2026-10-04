"""One prepared offline cache. Only sync performs network I/O."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import re
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import WorkspaceError
from .cache import download, file_checks, matches

ORIGIN = "https://tapcode-sce.spark.xd.com"
PREFIX = "/__tapmaker/offline"
SOURCES = ("engine", "engine-res", "engine-startup", "urhox-libs", "official-res")
PLAYER_PATHS = (
    "/src/web/src/index.min.js",
    "/src/web/libs/qrcode.min.js",
    "/src/web/libs/eruda.min.js",
    "/src/web/libs/mp4-muxer.mjs",
)
DOCUMENTATION_PATHS = frozenset({"Fonts/read_me.html"})
DEFAULT_GROUPS = ("official-shaders", "official-shadercache")
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
    "connect-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; media-src 'self' blob:; worker-src 'self' blob:; "
    "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


class _OfficialRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(ORIGIN + "/src/"):
            raise WorkspaceError(
                "Offline download redirect outside the official HTTPS asset origin"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get(path: str, *, limit: int = 256 * 1024 * 1024) -> bytes:
    # Paths come from fixed endpoints or validated manifest identifiers, never
    # from a browser request. TLS verification stays at urllib's secure default.
    if not path.startswith("/src/") or ".." in path or "?" in path or "#" in path:
        raise WorkspaceError(f"Invalid official resource path: {path}")
    request = Request(ORIGIN + path, headers={"Accept-Encoding": "identity"})
    with build_opener(_OfficialRedirects()).open(request, timeout=60) as response:
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


def cache_path(root: Path, path: str) -> Path:
    if (
        not path.startswith("/src/")
        or any(p in ("", ".", "..") for p in path[1:].split("/"))
        or "\\" in path
    ):
        raise WorkspaceError(f"Invalid cache path: {path}")
    result = root / path[1:]
    if not result.resolve().is_relative_to(root.resolve()):
        raise WorkspaceError(f"Cache path escapes directory: {path}")
    return result


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
        if item.get("fs_path") not in DOCUMENTATION_PATHS
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
) -> Path:
    """Invalidate readiness first; publish cache.json only after complete sync."""
    root.mkdir(parents=True, exist_ok=True)
    ready = root / "cache.json"
    ready.unlink(missing_ok=True)
    groups = list(DEFAULT_GROUPS) + (groups or [])
    versions, manifests, files = {}, {}, {}

    def add(path: str, data: bytes, source_path: str | None = None) -> None:
        destination = cache_path(root, path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        temporary.write_bytes(data)
        temporary.replace(destination)
        files[path] = {
            **file_checks(destination),
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
        for dependency, specification in manifest.get("sources", {}).items():
            expected = f"{ORIGIN}/src/{dependency}/"
            if specification.get("base_url", expected).rstrip("/") != expected.rstrip(
                "/"
            ):
                raise WorkspaceError(f"Unsupported source origin: {name}/{dependency}")
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

    def fetch_asset(pair):
        path, item = pair
        url = ORIGIN + path
        expected = {"size": item.get("size", 0), "hash": item["hash"]}
        info = download(
            url,
            cache_path(root, path),
            expected,
            opener=build_opener(_OfficialRedirects()).open,
        )
        return path, {**info, "source_url": url}

    with ThreadPoolExecutor(max_workers=8) as pool:
        files.update(pool.map(fetch_asset, downloads.items()))
    for path in PLAYER_PATHS:
        add(path, _get(path, limit=8 * 1024 * 1024))
    metadata = {
        "format": 2,
        "origin": ORIGIN,
        "versions": versions,
        "files": files,
        "selected": sorted([list(pair) for pair in selected]),
        "selection": {
            "groups": sorted(set(groups)),
            "resources": sorted(resources or []),
        },
    }
    temporary = root / "cache.json.part"
    temporary.write_bytes(json_bytes(metadata))
    temporary.replace(ready)
    return root


def signature(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


class OfflineCache:
    def __init__(self, root: Path):
        self.root = root
        self.ready = root / "cache.json"
        self.rendered = {}
        self.signatures = {}
        try:
            self.metadata = json.loads(self.ready.read_bytes())
            self.ready_signature = signature(self.ready)
            if self.metadata["format"] != 2 or self.metadata["origin"] != ORIGIN:
                raise WorkspaceError("Unsupported offline cache; run web-offline sync")
            self.files = self.metadata["files"]
            for path, item in self.files.items():
                file = cache_path(root, path)
                if not {
                    "size",
                    "hash",
                    "sha256",
                    "source_url",
                } <= item.keys() or not matches(file_checks(file), item):
                    raise WorkspaceError(
                        f"Offline cache corrupt: {path}; run web-offline sync"
                    )
                self.signatures[path] = signature(file)
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
                            item = self.files.get(asset_path(name, variant), {})
                            if (
                                item.get("size") != variant.get("size", 0)
                                or item.get("hash") != variant["hash"]
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
                self.file(path)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise WorkspaceError(
                f"Offline cache incomplete/invalid: {error}; run web-offline sync"
            ) from error

    def file(self, path: str) -> Path:
        if path not in self.files:
            raise WorkspaceError(
                f"Offline resource not prepared: {path}; resync the resource selection"
            )
        file = cache_path(self.root, path)
        try:
            if (
                signature(self.ready) != self.ready_signature
                or signature(file) != self.signatures[path]
            ):
                raise OSError("cache changed")
        except OSError as error:
            raise WorkspaceError(
                f"Offline cache changed or missing: {path}; sync and restart"
            ) from error
        return file

    def raw(self, path: str) -> bytes:
        return self.file(path).read_bytes()

    def render(self, path: str, base: str) -> bytes:
        file = self.file(path)
        key = (path, base)
        if key in self.rendered:
            return self.rendered[key]
        data = file.read_bytes()
        if path.endswith((".js", ".mjs", ".json")):
            data = data.replace(ORIGIN.encode(), base.encode())
            if path.endswith(".json") and "/manifest-" in path:
                manifest = json.loads(data)
                # Paks can contain unselected assets; use the verified per-file
                # graph, without implicitly fetching multi-gigabyte archives.
                manifest["assets_pak"] = 0
                manifest["paks"] = []
                # Documentation is excluded from the executable offline graph,
                # including default-group preload lists derived by the Runtime.
                if not path.startswith("/src/official-res/"):
                    manifest["files"] = [
                        f
                        for f in manifest["files"]
                        if f.get("fs_path") not in DOCUMENTATION_PATHS
                    ]
                data = json_bytes(manifest)
        self.rendered[key] = data
        return data
