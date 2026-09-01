from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.request import Request, urlopen
import zlib

from .config import WorkspaceError

ENGINE_BASE_URL = "https://tapcode-sce.spark.xd.com/src/engine/"
RUNTIME_FILES = ("UrhoXRuntime.js", "UrhoXRuntime.wasm", "UrhoXRuntime.data")

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
