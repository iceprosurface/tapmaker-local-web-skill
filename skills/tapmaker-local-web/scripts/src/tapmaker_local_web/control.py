from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hmac
import json
import re
import secrets
import threading
from pathlib import Path
from typing import Iterable


LOG_LIMIT = 2000
LOG_LINES_PER_REPORT = 200
LOG_MESSAGE_LIMIT = 8000
LOG_LEVELS = ("info", "warn", "error")
SCREENSHOT_DATA_LIMIT = 8 * 1024 * 1024
MILESTONE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class ControlError(ValueError):
    """控制面请求内容不合法。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Screenshot:
    payload_id: str
    data: bytes
    taken_at: str


class ControlPlane:
    """预览会话的控制面：令牌鉴权、页面日志、里程碑、截图与页面命令广播。

    深模块说明：HTTP 层只做路由与序列化，全部状态与校验集中在这里；
    CLI 与测试通过 HTTP 接口触达同一套行为。
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        log_path: Path | None = None,
        log_limit: int = LOG_LIMIT,
    ) -> None:
        self._token = token or secrets.token_urlsafe(24)
        self._log_path = log_path
        self._log_limit = log_limit
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._logs: deque[dict[str, object]] = deque(maxlen=log_limit)
        self._total_logs = 0
        self._dropped_logs = 0
        self._log_counts = {"info": 0, "warn": 0, "error": 0}
        self._milestones: dict[str, str] = {}
        self._pages = 0
        self._last_page_seen: str | None = None
        self._command_seq = 0
        self._last_command: dict[str, object] | None = None
        self._screenshot: Screenshot | None = None
        self._started_at = _now()

    @property
    def token(self) -> str:
        return self._token

    def authorize(self, provided: str | None) -> bool:
        return bool(provided) and hmac.compare_digest(provided, self._token)

    # 页面连接 ---------------------------------------------------------------

    def page_opened(self) -> None:
        with self._changed:
            self._pages += 1
            self._last_page_seen = _now()
            self._changed.notify_all()

    def page_closed(self) -> None:
        with self._changed:
            self._pages = max(0, self._pages - 1)
            self._changed.notify_all()

    def note_page_seen(self) -> None:
        with self._changed:
            self._last_page_seen = _now()

    @property
    def connected_pages(self) -> int:
        with self._lock:
            return self._pages

    # 命令广播 ---------------------------------------------------------------

    def broadcast(self, name: str) -> dict[str, object]:
        with self._changed:
            self._command_seq += 1
            payload = {"name": name, "id": secrets.token_hex(8), "seq": self._command_seq}
            self._last_command = payload
            self._changed.notify_all()
            return dict(payload)

    def command_seq(self) -> int:
        with self._lock:
            return self._command_seq

    def last_command(self) -> dict[str, object] | None:
        with self._lock:
            return dict(self._last_command) if self._last_command is not None else None

    def wake(self) -> None:
        with self._changed:
            self._changed.notify_all()

    def sleep(self, timeout: float) -> None:
        with self._changed:
            self._changed.wait(timeout)

    # 页面日志 ---------------------------------------------------------------

    def record_logs(self, lines: object) -> int:
        if not isinstance(lines, list) or not lines:
            raise ControlError("logs.lines 必须是非空数组")
        if len(lines) > LOG_LINES_PER_REPORT:
            raise ControlError(f"单次最多上报 {LOG_LINES_PER_REPORT} 行日志")
        prepared: list[dict[str, object]] = []
        for item in lines:
            if not isinstance(item, dict):
                raise ControlError("日志行必须是对象")
            level = item.get("level", "info")
            if level not in LOG_LEVELS:
                raise ControlError(f"日志级别无效：{level}")
            message = item.get("message")
            if not isinstance(message, str) or not message:
                raise ControlError("日志 message 必须是非空字符串")
            source = item.get("source", "console")
            if not isinstance(source, str) or not source or len(source) > 32:
                raise ControlError("日志 source 无效")
            prepared.append(
                {
                    "ts": _now(),
                    "level": level,
                    "source": source,
                    "message": message[:LOG_MESSAGE_LIMIT],
                }
            )
        with self._changed:
            for line in prepared:
                self._logs.append(line)
                self._total_logs += 1
                self._log_counts[str(line["level"])] += 1
            self._dropped_logs = max(0, self._total_logs - len(self._logs))
            self._last_page_seen = _now()
        self._append_log_file(prepared)
        return len(prepared)

    def logs(self, lines: int = 200) -> dict[str, object]:
        with self._lock:
            tail = list(self._logs)[-max(1, min(lines, self._log_limit)) :]
            return {
                "lines": tail,
                "total": self._total_logs,
                "dropped": self._dropped_logs,
                "limit": self._log_limit,
                "counts": dict(self._log_counts),
            }

    def _append_log_file(self, prepared: Iterable[dict[str, object]]) -> None:
        if self._log_path is None:
            return
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as stream:
                for line in prepared:
                    stream.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # 里程碑 -----------------------------------------------------------------

    def mark(self, name: object) -> None:
        if not isinstance(name, str) or not MILESTONE_PATTERN.match(name):
            raise ControlError(f"里程碑名称无效：{name}")
        with self._changed:
            self._milestones.setdefault(name, _now())
            self._changed.notify_all()

    def milestones(self) -> dict[str, str]:
        with self._lock:
            return dict(self._milestones)

    # 截图 -------------------------------------------------------------------

    def store_screenshot(self, payload_id: object, data_url: object) -> Screenshot:
        if not isinstance(payload_id, str) or not payload_id:
            raise ControlError("截图响应缺少 id")
        if not isinstance(data_url, str):
            raise ControlError("截图 data_url 必须是字符串")
        prefix = "data:image/png;base64,"
        if not data_url.startswith(prefix):
            raise ControlError("截图只支持 data:image/png;base64 数据")
        encoded = data_url[len(prefix) :]
        if len(encoded) > SCREENSHOT_DATA_LIMIT:
            raise ControlError("截图数据过大")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as error:
            raise ControlError("截图 base64 解码失败") from error
        if not data.startswith(PNG_MAGIC):
            raise ControlError("截图不是有效的 PNG 数据")
        with self._changed:
            self._screenshot = Screenshot(payload_id, data, _now())
            self._changed.notify_all()
        return self._screenshot

    def screenshot_status(self) -> dict[str, object]:
        with self._lock:
            shot = self._screenshot
            if shot is None:
                return {"status": "none"}
            return {
                "status": "ready",
                "id": shot.payload_id,
                "taken_at": shot.taken_at,
                "size": len(shot.data),
            }

    def screenshot_bytes(self) -> bytes | None:
        with self._lock:
            return self._screenshot.data if self._screenshot is not None else None

    # 汇总 -------------------------------------------------------------------

    def summary(self) -> dict[str, object]:
        with self._lock:
            return {
                "started_at": self._started_at,
                "connected_pages": self._pages,
                "last_page_seen_at": self._last_page_seen,
                "log": {
                    "lines": len(self._logs),
                    "total": self._total_logs,
                    "dropped": self._dropped_logs,
                    "counts": dict(self._log_counts),
                },
                "milestones": dict(self._milestones),
                "commands": self._command_seq,
            }
