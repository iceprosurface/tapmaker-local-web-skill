from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import Project, WorkspaceError


SESSION_FILE = "session.json"
START_TIMEOUT_SECONDS = 20.0
STOP_TIMEOUT_SECONDS = 5.0
REQUEST_TIMEOUT = 5.0


def state_root() -> Path:
    override = os.environ.get("TAPMAKER_LOCAL_WEB_STATE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/TapMaker/local-web"
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return base / "tapmaker/local-web"


def session_dir(project_name: str, *, root: Path | None = None) -> Path:
    return (root or state_root()) / project_name


@dataclass(frozen=True)
class SessionRecord:
    pid: int
    port: int
    token: str
    url: str
    project: str
    entry: str
    started_at: str
    test_server: dict[str, object] | None = None

    def base_url(self, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}"

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "pid": self.pid,
            "port": self.port,
            "token": self.token,
            "url": self.url,
            "project": self.project,
            "entry": self.entry,
            "started_at": self.started_at,
        }
        if self.test_server is not None:
            value["test_server"] = self.test_server
        return value

    @classmethod
    def from_dict(cls, value: object) -> "SessionRecord":
        if not isinstance(value, dict):
            raise WorkspaceError("会话记录格式无效")
        try:
            test_server = value.get("test_server")
            return cls(
                pid=int(value["pid"]),
                port=int(value["port"]),
                token=str(value["token"]),
                url=str(value["url"]),
                project=str(value["project"]),
                entry=str(value["entry"]),
                started_at=str(value.get("started_at") or ""),
                test_server=test_server if isinstance(test_server, dict) else None,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise WorkspaceError("会话记录字段无效") from error


def write_record(record: SessionRecord, *, root: Path | None = None) -> Path:
    directory = session_dir(record.project, root=root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SESSION_FILE
    path.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def read_record(project_name: str, *, root: Path | None = None) -> SessionRecord | None:
    path = session_dir(project_name, root=root) / SESSION_FILE
    try:
        return SessionRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, WorkspaceError):
        return None


def remove_record(project_name: str, *, root: Path | None = None) -> None:
    (session_dir(project_name, root=root) / SESSION_FILE).unlink(missing_ok=True)


def new_record(
    project: str,
    entry: str,
    url: str,
    port: int,
    token: str,
    *,
    test_server: dict[str, object] | None = None,
) -> SessionRecord:
    return SessionRecord(
        pid=os.getpid(),
        port=port,
        token=token,
        url=url,
        project=project,
        entry=entry,
        started_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        test_server=test_server,
    )


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_exited(pid: int) -> bool:
    """判断进程是否已退出；对本体进程的僵尸子进程会顺便回收。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
    except OSError:
        return False
    return reaped != 0


def wait_for_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process_exited(pid):
            return True
        time.sleep(0.05)
    return _process_exited(pid)


def fetch_json(url: str, timeout: float = REQUEST_TIMEOUT) -> dict[str, object]:
    with urlopen(url, timeout=timeout) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise WorkspaceError(f"接口返回的不是 JSON 对象：{url}")
    return value


def health(record: SessionRecord, timeout: float = REQUEST_TIMEOUT) -> dict[str, object] | None:
    try:
        return fetch_json(record.base_url() + "/__tapmaker/health", timeout)
    except (OSError, ValueError):
        return None


def control_post(
    record: SessionRecord,
    path: str,
    payload: object | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> tuple[int, dict[str, object]]:
    request = Request(
        record.base_url() + path,
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-TapMaker-Control": record.token},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, _json_body(response.read())
    except HTTPError as error:
        return error.code, _json_body(error.read())


def _json_body(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"error": data.decode("utf-8", "replace")[:500]}
    return value if isinstance(value, dict) else {"error": str(value)[:500]}


def start_detached(project: Project, argv: Sequence[str]) -> SessionRecord:
    """以后台子进程启动预览，并等待会话记录与健康检查就绪。"""
    directory = session_dir(project.name)
    directory.mkdir(parents=True, exist_ok=True)
    remove_record(project.name)
    log_path = directory / "server.log"
    environment = dict(
        os.environ,
        TAPMAKER_LOCAL_WEB_STATE_ROOT=str(state_root()),
        TAPMAKER_LOCAL_WEB_DETACHED="1",
    )
    command = [sys.executable, "-m", "tapmaker_local_web", *argv]
    with log_path.open("ab") as log:
        subprocess.Popen(
            command,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=environment,
        )
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        record = read_record(project.name)
        if record is not None and health(record) is not None:
            return record
        time.sleep(0.1)
    raise WorkspaceError(f"后台预览启动超时；服务日志：{log_path}")
