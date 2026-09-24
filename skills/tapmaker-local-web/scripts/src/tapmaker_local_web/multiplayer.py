from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
import base64
import json
import os
import re
import socket
import ssl
import struct
import sys
import time
from urllib.request import Request, urlopen

from .config import WorkspaceError


MAKER_PAT_ENV = "MAKER_PAT"
SHORT_PAT_ENV = "PAT"
MAKER_HOME_ENV = "TAPTAP_MAKER_HOME"
MAKER_DIR = ".taptap-maker"
TAP_TOKEN_URL = "https://maker.taptap.cn/api/v1/user/taptap-token"
ENTRANCE_URL = "wss://entrance-new-pd.spark.xd.com"
LOGIN_CLIENT_ID = "zgkpe37bjjs9gehbsz"
LOGIN_DEVICE_TYPE = 33554440
ALLOCATE_TIMEOUT_SECONDS = 45.0
HANDSHAKE_TIMEOUT_SECONDS = 10.0
MAX_MESSAGE_BYTES = 1024 * 1024
VERSION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,127}$", re.IGNORECASE)
POD_PATTERN = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------------------------------------------------------------------------
# 凭据：复用官方 taptap-maker login 的本地存储，不引入独立登录流程。


def maker_home() -> Path:
    override = os.environ.get(MAKER_HOME_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / MAKER_DIR


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_pat(manual: str | None = None) -> str:
    """按官方顺序解析 PAT：手动值 → MAKER_PAT → PAT → pat.json → 旧版 .maker-pat。"""
    token = manual or os.environ.get(MAKER_PAT_ENV) or os.environ.get(SHORT_PAT_ENV)
    if token and token.strip():
        return token.strip()
    cached = _read_json_object(maker_home() / "pat.json")
    if cached and isinstance(cached.get("token"), str) and cached["token"].strip():
        return cached["token"].strip()
    legacy = maker_home().parent / ".maker-pat"
    try:
        value = legacy.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value:
        return value
    raise WorkspaceError(
        "未找到 Maker PAT；请先运行 taptap-maker login（或设置 MAKER_PAT 环境变量）"
    )


def tap_auth_path() -> Path:
    return maker_home() / "tap-auth.json"


def save_tap_auth(auth: dict[str, str]) -> None:
    payload = {**auth, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        tap_auth_path().parent.mkdir(parents=True, exist_ok=True)
        tap_auth_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def request_tap_auth(pat_token: str, *, url: str = TAP_TOKEN_URL, timeout: float = 15.0) -> dict[str, str]:
    """用 PAT 换取测试服登录凭据 kid/mac_key（等价官方 requestTapAuthWithPat）。"""
    request = Request(url, headers={"Authorization": f"Bearer {pat_token}", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except OSError as error:
        raise WorkspaceError(f"TapTap token 请求失败：{error}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkspaceError("TapTap token 响应不是有效 JSON") from error
    if not isinstance(body, dict):
        raise WorkspaceError("TapTap token 响应格式无效")
    source = body.get("data") if isinstance(body.get("data"), dict) else body
    kid = source.get("kid")
    mac_key = source.get("mac_key")
    if not isinstance(kid, str) or not kid or not isinstance(mac_key, str) or not mac_key:
        raise WorkspaceError("TapTap token 响应缺少 kid/mac_key")
    auth = {"kid": kid, "mac_key": mac_key}
    save_tap_auth(auth)
    return auth


# ---------------------------------------------------------------------------
# 极简 protobuf（proto2 varint/length-delimited），仅覆盖测试服握手所需消息。


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint 不支持负数")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _varint_field(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def _bytes_field(field: int, data: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(data)) + data


def _string_field(field: int, value: str) -> bytes:
    return _bytes_field(field, value.encode("utf-8"))


def _parse_fields(data: bytes) -> dict[int, tuple[int, object]]:
    """解析 protobuf 消息；返回 field -> (wire_type, value)。未知字段跳过。"""
    result: dict[int, tuple[int, object]] = {}
    offset = 0
    while offset < len(data):
        tag_value = 0
        shift = 0
        while True:
            if offset >= len(data):
                raise WorkspaceError("protobuf tag 越界")
            byte = data[offset]
            offset += 1
            tag_value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        field, wire = tag_value >> 3, tag_value & 0x07
        if wire == 0:
            value = 0
            shift = 0
            while True:
                if offset >= len(data):
                    raise WorkspaceError("protobuf varint 越界")
                byte = data[offset]
                offset += 1
                value |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80:
                    break
        elif wire == 2:
            length = 0
            shift = 0
            while True:
                if offset >= len(data):
                    raise WorkspaceError("protobuf 长度越界")
                byte = data[offset]
                offset += 1
                length |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80:
                    break
            if offset + length > len(data):
                raise WorkspaceError("protobuf 内容越界")
            value = data[offset : offset + length]
            offset += length
        elif wire == 5:
            if offset + 4 > len(data):
                raise WorkspaceError("protobuf fixed32 越界")
            value = struct.unpack_from("<I", data, offset)[0]
            offset += 4
        elif wire == 1:
            if offset + 8 > len(data):
                raise WorkspaceError("protobuf fixed64 越界")
            value = struct.unpack_from("<Q", data, offset)[0]
            offset += 8
        else:
            raise WorkspaceError(f"不支持的 wire type：{wire}")
        result[field] = (wire, value)
    return result


def _field_int(fields: dict[int, tuple[int, object]], field: int, default: int = 0) -> int:
    entry = fields.get(field)
    if entry is None:
        return default
    value = entry[1]
    return value if isinstance(value, int) else default


def _field_bytes(fields: dict[int, tuple[int, object]], field: int) -> bytes:
    entry = fields.get(field)
    if entry is None:
        return b""
    value = entry[1]
    return value if isinstance(value, bytes) else b""


def encode_header(message_type: int, message_body: bytes, user_id: int = 0, login_id: int = 0) -> bytes:
    return (
        _varint_field(1, message_type)
        + _bytes_field(2, message_body)
        + _varint_field(3, user_id)
        + _varint_field(10, login_id)
    )


def encode_login(token: str) -> bytes:
    return (
        _bytes_field(1, b"default")
        + _bytes_field(2, b"")
        + _varint_field(4, LOGIN_DEVICE_TYPE)
        + _varint_field(6, 11)
        + _string_field(7, token)
        + _string_field(14, LOGIN_CLIENT_ID)
        + _string_field(17, "debug")
        + _varint_field(18, 0)
        + _varint_field(19, 1)
    )


def encode_lobby(request_id: int, message_body: bytes) -> bytes:
    return _varint_field(1, request_id) + _bytes_field(2, message_body)


def encode_create(map_name: str, mode_args: bytes) -> bytes:
    return (
        _string_field(1, map_name)
        + _varint_field(2, 1)
        + _bytes_field(3, mode_args)
        + _string_field(7, "test")
    )


def decode_header(data: bytes) -> tuple[int, bytes, int]:
    fields = _parse_fields(data)
    return (
        _field_int(fields, 1),
        _field_bytes(fields, 2),
        _field_int(fields, 3),
    )


def decode_login_result(data: bytes) -> int:
    return _field_int(_parse_fields(data), 1)


def decode_lobby(data: bytes) -> tuple[int, bytes]:
    fields = _parse_fields(data)
    return _field_int(fields, 1), _field_bytes(fields, 2)


def decode_created(data: bytes) -> tuple[int, str, int]:
    fields = _parse_fields(data)
    error_code = _field_int(fields, 1)
    connect = _field_bytes(fields, 2)
    connect_fields = _parse_fields(connect) if connect else {}
    pod = _field_bytes(connect_fields, 11).decode("utf-8", "replace")
    ws_port = _field_int(connect_fields, 10)
    return error_code, pod, ws_port


# ---------------------------------------------------------------------------
# 极简 WebSocket 客户端（RFC 6455：握手 + 二进制帧 + ping/pong），零第三方依赖。


class WebSocketError(RuntimeError):
    """WebSocket 握手或帧协议错误。"""


class WebSocketConnection:
    def __init__(self, url: str, *, timeout: float = HANDSHAKE_TIMEOUT_SECONDS) -> None:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss") or not parts.hostname:
            raise WebSocketError(f"WebSocket 地址无效：{url}")
        self._secure = parts.scheme == "wss"
        self._host = parts.hostname
        self._port = parts.port or (443 if self._secure else 80)
        self._path = parts.path or "/"
        if parts.query:
            self._path += "?" + parts.query
        self._deadline = time.monotonic() + timeout
        raw = socket.create_connection((self._host, self._port), timeout=timeout)
        if self._secure:
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=self._host)
        self._socket = raw
        self._socket.settimeout(timeout)
        self._reader = raw.makefile("rb")
        self._handshake()

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise WebSocketError("WebSocket 操作超时")
        return remaining

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {self._host}:{self._port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._socket.sendall(request.encode("ascii"))
        status_line = self._reader.readline()
        if not status_line or b" 101 " not in status_line:
            raise WebSocketError(f"WebSocket 握手失败：{status_line.decode('latin-1', 'replace').strip()}")
        headers: dict[str, str] = {}
        while True:
            line = self._reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(sha1((key + _WS_MAGIC).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise WebSocketError("WebSocket 握手校验失败")

    def send(self, payload: bytes) -> None:
        self._socket.settimeout(self._remaining())
        mask = os.urandom(4)
        header = bytearray([0x82])  # FIN + binary
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(bytes(header) + masked)

    def receive(self) -> bytes:
        message = bytearray()
        while True:
            self._socket.settimeout(self._remaining())
            first = self._read_exact(1)[0]
            second = self._read_exact(1)[0]
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if len(message) + length > MAX_MESSAGE_BYTES:
                raise WebSocketError("WebSocket 消息超过大小限制")
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(length)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise WebSocketError("WebSocket 连接已被服务端关闭")
            if opcode == 0x9:
                self._send_pong(payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if first & 0x80:
                return bytes(message)

    def _send_pong(self, payload: bytes) -> None:
        self._socket.settimeout(self._remaining())
        mask = os.urandom(4)
        header = bytearray([0x8A])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(bytes(header) + masked)

    def _read_exact(self, count: int) -> bytes:
        data = self._reader.read(count)
        if len(data) != count:
            raise WebSocketError("WebSocket 连接在读取途中断开")
        return data

    def close(self) -> None:
        try:
            self._socket.settimeout(2)
            self._socket.sendall(b"\x88\x80" + os.urandom(4))
        except OSError:
            pass
        try:
            self._socket.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 测试服申请与项目识别。


def request_test_server(
    project_id: str,
    version: str,
    auth: dict[str, str],
    *,
    entrance: str = ENTRANCE_URL,
    timeout: float = ALLOCATE_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """按官方 CreateMultiDebugGame 契约申请一个测试游戏，返回 directConnectParams 内容。"""
    deadline = time.monotonic() + timeout
    try:
        connection = WebSocketConnection(entrance)
    except (WebSocketError, OSError) as error:
        raise WorkspaceError(f"无法连接线上测试服入口，请检查网络后重试：{error}") from error
    try:
        token = f"{auth['mac_key']}${auth['kid']}"
        connection.send(encode_header(1, encode_login(token)))
        user_id = 0
        created_result: tuple[str, int] | None = None
        while created_result is None:
            if time.monotonic() > deadline:
                raise WorkspaceError("获取测试服连接超时，请稍后重试；未自动重复创建游戏")
            message_type, body, header_user_id = decode_header(connection.receive())
            if message_type == 2 and user_id == 0:
                result_code = decode_login_result(body)
                if result_code != 0:
                    raise WorkspaceError(
                        f"测试服登录失败（{result_code}），请重新运行 taptap-maker login"
                    )
                if header_user_id <= 0 or header_user_id > 2**53 - 1:
                    raise WorkspaceError("测试服返回的用户身份无效")
                user_id = header_user_id
                mode_args = json.dumps(
                    {"project_version": version}, separators=(",", ":")
                ).encode("utf-8")
                connection.send(
                    encode_header(12549, encode_lobby(1, encode_create(project_id, mode_args)))
                )
            elif message_type == 12549 and user_id:
                request_id, inner = decode_lobby(body)
                if request_id != 1:
                    continue
                error_code, pod, ws_port = decode_created(inner)
                if error_code != 0:
                    raise WorkspaceError(
                        f"测试服创建失败（{error_code}），请确认项目已成功提交构建，再重试预览"
                    )
                created_result = (pod, ws_port)
        pod, ws_port = created_result
        if (
            not POD_PATTERN.match(pod)
            or len(pod) > 253
            or not isinstance(ws_port, int)
            or not 1 <= ws_port <= 65535
        ):
            raise WorkspaceError("测试服返回的连接信息无效，未启动本地窗口")
        return {
            "userId": user_id,
            "connectInfo": {"pod_ip": pod, "server_port": 0, "ws_port": ws_port},
        }
    except WebSocketError as error:
        raise WorkspaceError(f"测试服连接在准备完成前断开，请稍后重试：{error}") from error
    finally:
        connection.close()


def direct_connect_param(server: dict[str, object]) -> str:
    """编码 Web Player 的 directConnectParams URL 参数（Base64 JSON）。"""
    return base64.b64encode(
        json.dumps(server, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


@dataclass(frozen=True)
class NetworkIdentity:
    project_id: str
    version: str
    author_id: str


def _project_settings(workspace_root: Path) -> dict[str, object]:
    value = _read_json_object(workspace_root / ".project" / "settings.json")
    return value or {}


def _project_manifest(workspace_root: Path) -> dict[str, object]:
    value = _read_json_object(workspace_root / ".project" / "project.json")
    return value or {}


def detect_multiplayer(workspace_root: Path) -> bool:
    """按官方分类规则识别联机/server 项目。"""
    runtime = _project_settings(workspace_root).get("@runtime")
    if isinstance(runtime, dict):
        multiplayer = runtime.get("multiplayer")
        if isinstance(multiplayer, dict):
            if "enabled" in multiplayer:
                if multiplayer["enabled"] is True:
                    return True
            elif isinstance(multiplayer.get("max_players"), int):
                if multiplayer["max_players"] > 0:
                    return True
            else:
                persistent = multiplayer.get("persistent_world")
                if isinstance(persistent, dict) and persistent.get("enabled") is True:
                    return True
        elif isinstance(runtime.get("max_players"), int) and runtime["max_players"] > 0:
            return True
    manifest = _project_manifest(workspace_root)
    if isinstance(manifest.get("entry@client"), str) or isinstance(manifest.get("entry@server"), str):
        return True
    return any(
        (workspace_root / "scripts" / name).is_file()
        for name in ("server_main.lua", "server.lua")
    )


def network_identity(workspace_root: Path, *, explicit_version: str | None = None) -> NetworkIdentity:
    """解析联机预览所需的真实项目身份与测试构建版本。"""
    manifest = _project_manifest(workspace_root)
    project_id = manifest.get("project_id")
    author = manifest.get("author")
    author_id = author.get("id") if isinstance(author, dict) else None
    if not isinstance(project_id, str) or not project_id.strip() or author_id == "local-preview":
        raise WorkspaceError(
            "联网预览缺少游戏配置，请先提交构建并生成一次测试二维码，再启动本地预览"
        )

    version: str | None = None
    if explicit_version is not None:
        version = explicit_version
    else:
        latest = _read_json_object(_dist_directory(workspace_root) / "latest.json")
        if latest is not None and isinstance(latest.get("version"), str):
            version = latest["version"]
        elif isinstance(manifest.get("version"), str):
            version = manifest["version"]
    if not version or not VERSION_PATTERN.match(version):
        raise WorkspaceError(
            "联网预览缺少有效的测试构建版本；请先提交构建，或用 --project-version 指定"
        )
    return NetworkIdentity(project_id.strip(), version, str(author_id or ""))


def _dist_directory(workspace_root: Path) -> Path:
    output = _project_settings(workspace_root).get("build", {})
    output_dir = output.get("output_dir") if isinstance(output, dict) else None
    project_dir = workspace_root / ".project"
    if isinstance(output_dir, str) and output_dir:
        candidate = Path(output_dir)
        if not candidate.is_absolute():
            candidate = project_dir / candidate
        try:
            return candidate.resolve()
        except OSError:
            return workspace_root / "dist"
    return workspace_root / "dist"
