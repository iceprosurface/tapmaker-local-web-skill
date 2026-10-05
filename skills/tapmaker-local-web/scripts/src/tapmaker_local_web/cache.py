"""Streaming download and integrity checks for the opt-in offline cache."""

import gzip
import hashlib
from pathlib import Path
from urllib.request import Request, urlopen
import zlib

from .config import WorkspaceError


def _copy(source, output=None, limit=None):
    sha, crc, size = hashlib.sha256(), 0, 0
    for block in iter(lambda: source.read(1024 * 1024), b""):
        size += len(block)
        if limit is not None and size > limit:
            raise WorkspaceError("Download exceeds declared size/budget")
        sha.update(block)
        crc = zlib.crc32(block, crc)
        if output is not None:
            output.write(block)
    return {"size": size, "hash": f"{crc & 0xFFFFFFFF:08x}", "sha256": sha.hexdigest()}


def file_checks(path: Path) -> dict:
    with path.open("rb") as source:
        return _copy(source)


def matches(actual: dict, expected: dict) -> bool:
    return all(actual[key] == expected[key] for key in actual if key in expected)


def download(url: str, destination: Path, expected: dict, *, opener=urlopen) -> dict:
    """Reuse verified files; check downloads while streaming, then rename .part."""
    try:
        info = file_checks(destination)
        if matches(info, expected):
            return info
    except OSError:
        pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        if expected.get("size") == 0 and expected.get("hash") == "00000000":
            temporary.write_bytes(b"")
            info = file_checks(temporary)
        else:
            request = Request(url, headers={"Accept-Encoding": "identity"})
            with (
                opener(request, timeout=60) as response,
                temporary.open("wb") as output,
            ):
                content = (
                    gzip.GzipFile(fileobj=response)
                    if response.headers.get("Content-Encoding") == "gzip"
                    else response
                )
                info = _copy(content, output, expected["size"])
        if not matches(info, expected):
            raise WorkspaceError(f"Cache checksum mismatch: {url}")
        temporary.replace(destination)
        return info
    finally:
        temporary.unlink(missing_ok=True)
