from __future__ import annotations

import base64
from hashlib import sha1
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import unquote

from tapmaker_local_web.multiplayer import (
    WebSocketConnection,
    _bytes_field,
    _field_bytes,
    _field_int,
    _parse_fields,
    _string_field,
    _varint_field,
    decode_created,
    decode_header,
    decode_lobby,
    direct_connect_param,
    detect_multiplayer,
    encode_create,
    encode_header,
    encode_login,
    encode_lobby,
    load_pat,
    network_identity,
    request_tap_auth,
    request_test_server,
)
from tapmaker_local_web.config import WorkspaceError
from tapmaker_local_web.server import LocalWebProject, LocalWebServer

_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _read_exact_stream(reader, count: int) -> bytes:
    data = reader.read(count)
    if len(data) != count:
        raise AssertionError("frame underflow")
    return data


def _read_client_frame(reader) -> bytes:
    first, second = _read_exact_stream(reader, 1)[0], _read_exact_stream(reader, 1)[0]
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(_read_exact_stream(reader, 2), "big")
    elif length == 127:
        length = int.from_bytes(_read_exact_stream(reader, 8), "big")
    mask = _read_exact_stream(reader, 4) if second & 0x80 else None
    payload = _read_exact_stream(reader, length)
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    assert first & 0x0F == 0x2, "expected binary frame"
    return payload


def _server_frame(payload: bytes) -> bytes:
    header = bytearray([0x82])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += length.to_bytes(2, "big")
    else:
        header.append(127)
        header += length.to_bytes(8, "big")
    return bytes(header) + payload


class FakeEntrance:
    """本地伪测试服入口：按官方握手协议回放 login/create 流程。"""

    def __init__(
        self,
        *,
        login_result_code: int = 0,
        create_error_code: int = 0,
        user_id: int = 424242,
        pod: str = "pod-a.abc.zone",
        ws_port: int = 47001,
    ) -> None:
        self.login_result_code = login_result_code
        self.create_error_code = create_error_code
        self.user_id = user_id
        self.pod = pod
        self.ws_port = ws_port
        self.login_frame: bytes | None = None
        self.create_frame: bytes | None = None
        self.listener = socket.socket()
        self.listener.settimeout(10)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/entrance"
    def wait(self) -> None:
        self.thread.join(timeout=10)

    def _run(self) -> None:
        try:
            connection, _ = self.listener.accept()
        except OSError:
            return
        reader = connection.makefile("rb")
        try:
            handshake = b""
            while b"\r\n\r\n" not in handshake:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                handshake += chunk
            match = re.search(rb"Sec-WebSocket-Key: (.+?)\r\n", handshake)
            assert match is not None
            key = match.group(1).decode("ascii")
            accept = base64.b64encode(sha1((key + _WS_MAGIC).encode("ascii")).digest()).decode("ascii")
            connection.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode("ascii")
            )
            self.login_frame = _read_client_frame(reader)
            if self.login_result_code != 0:
                body = _varint_field(1, self.login_result_code)
                connection.sendall(_server_frame(encode_header(2, body, user_id=0)))
                return
            body = _varint_field(1, 0)
            connection.sendall(_server_frame(encode_header(2, body, user_id=self.user_id)))
            self.create_frame = _read_client_frame(reader)
            connect = _varint_field(10, self.ws_port) + _string_field(11, self.pod)
            created = _varint_field(1, self.create_error_code) + _bytes_field(2, connect)
            connection.sendall(
                _server_frame(encode_header(12549, encode_lobby(1, created), user_id=self.user_id))
            )
        finally:
            try:
                connection.close()
            except OSError:
                pass


class ProtoCodecTest(unittest.TestCase):
    def test_header_roundtrip(self) -> None:
        encoded = encode_header(12549, b"inner", user_id=7, login_id=0)
        message_type, body, user_id = decode_header(encoded)
        self.assertEqual((message_type, body, user_id), (12549, b"inner", 7))

    def test_login_message_fields(self) -> None:
        encoded = encode_login("mac$kid")
        fields = _parse_fields(encoded)
        self.assertEqual(_field_bytes(fields, 1), b"default")
        self.assertEqual(_field_bytes(fields, 2), b"")
        self.assertEqual(_field_int(fields, 4), 33554440)
        self.assertEqual(_field_int(fields, 6), 11)
        self.assertEqual(_field_bytes(fields, 7).decode(), "mac$kid")
        self.assertEqual(_field_bytes(fields, 14).decode(), "zgkpe37bjjs9gehbsz")
        self.assertEqual(_field_bytes(fields, 17).decode(), "debug")
        self.assertEqual(_field_int(fields, 18), 0)
        self.assertEqual(_field_int(fields, 19), 1)

    def test_create_message_fields(self) -> None:
        encoded = encode_create("proj-1", b'{"project_version":"1.0.3"}')
        fields = _parse_fields(encoded)
        self.assertEqual(_field_bytes(fields, 1).decode(), "proj-1")
        self.assertEqual(_field_int(fields, 2), 1)
        self.assertEqual(json.loads(_field_bytes(fields, 3)), {"project_version": "1.0.3"})
        self.assertEqual(_field_bytes(fields, 7).decode(), "test")

    def test_created_message_decoding(self) -> None:
        connect = _varint_field(10, 47001) + _string_field(11, "pod.x")
        created = _varint_field(1, 0) + _bytes_field(2, connect)
        error_code, pod, ws_port = decode_created(created)
        self.assertEqual((error_code, pod, ws_port), (0, "pod.x", 47001))


class RequestTestServerTest(unittest.TestCase):
    def test_roundtrip_against_fake_entrance(self) -> None:
        fake = FakeEntrance()
        result = request_test_server(
            "proj-1",
            "1.0.3",
            {"mac_key": "mac-value", "kid": "kid-value"},
            entrance=fake.url,
            timeout=10,
        )
        fake.wait()
        self.assertEqual(
            result,
            {
                "userId": 424242,
                "connectInfo": {"pod_ip": "pod-a.abc.zone", "server_port": 0, "ws_port": 47001},
            },
        )
        assert fake.login_frame is not None
        _, login_body, _ = decode_header(fake.login_frame)
        login_fields = _parse_fields(login_body)
        self.assertEqual(
            _field_bytes(login_fields, 7).decode(), "mac-value$kid-value"
        )
        assert fake.create_frame is not None
        _, create_body, _ = decode_header(fake.create_frame)
        request_id, inner = decode_lobby(create_body)
        self.assertEqual(request_id, 1)
        create_fields = _parse_fields(inner)
        self.assertEqual(_field_bytes(create_fields, 1).decode(), "proj-1")
        self.assertEqual(json.loads(_field_bytes(create_fields, 3)), {"project_version": "1.0.3"})

    def test_login_failure_message(self) -> None:
        fake = FakeEntrance(login_result_code=7)
        with self.assertRaises(WorkspaceError) as caught:
            request_test_server(
                "proj-1", "1.0.3", {"mac_key": "m", "kid": "k"}, entrance=fake.url, timeout=10
            )
        fake.wait()
        self.assertIn("测试服登录失败（7）", str(caught.exception))
        self.assertIn("taptap-maker login", str(caught.exception))

    def test_create_failure_message(self) -> None:
        fake = FakeEntrance(create_error_code=3)
        with self.assertRaises(WorkspaceError) as caught:
            request_test_server(
                "proj-1", "1.0.3", {"mac_key": "m", "kid": "k"}, entrance=fake.url, timeout=10
            )
        fake.wait()
        self.assertIn("测试服创建失败（3）", str(caught.exception))
        self.assertIn("提交构建", str(caught.exception))

    def test_invalid_connection_info_rejected(self) -> None:
        fake = FakeEntrance(pod="bad pod name")
        with self.assertRaises(WorkspaceError) as caught:
            request_test_server(
                "proj-1", "1.0.3", {"mac_key": "m", "kid": "k"}, entrance=fake.url, timeout=10
            )
        fake.wait()
        self.assertIn("连接信息无效", str(caught.exception))


class TapAuthTest(unittest.TestCase):
    def _server(self, payload: str) -> tuple[ThreadingHTTPServer, str]:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload.encode("utf-8"))

            def log_message(self, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_port}/user/taptap-token"

    def test_parses_nested_response_and_saves_cache(self) -> None:
        server, url = self._server(json.dumps({"data": {"kid": "K1", "mac_key": "M1"}}))
        temporary = tempfile.TemporaryDirectory()
        try:
            with mock.patch.dict(
                "os.environ", {"TAPTAP_MAKER_HOME": str(Path(temporary.name) / "maker")}
            ):
                auth = request_tap_auth("pat-token", url=url)
                self.assertEqual(auth, {"kid": "K1", "mac_key": "M1"})
                cached = json.loads(
                    (Path(temporary.name) / "maker" / "tap-auth.json").read_text(encoding="utf-8")
                )
                self.assertEqual(cached["kid"], "K1")
                self.assertEqual(cached["mac_key"], "M1")
                self.assertIn("saved_at", cached)
        finally:
            server.shutdown()
            server.server_close()
            temporary.cleanup()

    def test_rejects_response_without_credentials(self) -> None:
        server, url = self._server(json.dumps({"data": {"kid": "K1"}}))
        try:
            with self.assertRaises(WorkspaceError) as caught:
                request_tap_auth("pat-token", url=url)
            self.assertIn("kid/mac_key", str(caught.exception))
        finally:
            server.shutdown()
            server.server_close()

    def test_load_pat_resolution_order(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        home = Path(temporary.name) / "maker"
        home.mkdir(parents=True)
        try:
            with mock.patch.dict(
                "os.environ", {"TAPTAP_MAKER_HOME": str(home), "MAKER_PAT": "", "PAT": ""}
            ):
                with self.assertRaises(WorkspaceError) as caught:
                    load_pat()
                self.assertIn("taptap-maker login", str(caught.exception))

                (home / "pat.json").write_text(
                    json.dumps({"token": "file-pat"}), encoding="utf-8"
                )
                self.assertEqual(load_pat(), "file-pat")

                with mock.patch.dict("os.environ", {"MAKER_PAT": "env-pat"}):
                    self.assertEqual(load_pat(), "env-pat")

                self.assertEqual(load_pat("manual-pat"), "manual-pat")
        finally:
            temporary.cleanup()


class ProjectDetectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_project(self, *, settings: dict | None, manifest: dict | None) -> None:
        project = self.root / ".project"
        project.mkdir(parents=True, exist_ok=True)
        if settings is not None:
            (project / "settings.json").write_text(
                json.dumps(settings, ensure_ascii=False), encoding="utf-8"
            )
        if manifest is not None:
            (project / "project.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )

    def test_detects_multiplayer_settings_variants(self) -> None:
        self._write_project(settings={"@runtime": {"multiplayer": {"enabled": True}}}, manifest=None)
        self.assertTrue(detect_multiplayer(self.root))

        self._write_project(
            settings={"@runtime": {"multiplayer": {"max_players": 4}}}, manifest=None
        )
        self.assertTrue(detect_multiplayer(self.root))

        self._write_project(
            settings={"@runtime": {"multiplayer": {"persistent_world": {"enabled": True}}}},
            manifest=None,
        )
        self.assertTrue(detect_multiplayer(self.root))

        self._write_project(
            settings={"@runtime": {"multiplayer": {"enabled": False}, "max_players": 2}},
            manifest=None,
        )
        self.assertFalse(detect_multiplayer(self.root))

        self._write_project(settings={"@runtime": {"multiplayer": {"enabled": False}}}, manifest=None)
        self.assertFalse(detect_multiplayer(self.root))

    def test_detects_server_entry_markers(self) -> None:
        self._write_project(settings={}, manifest={"entry@server": "server_main.lua"})
        self.assertTrue(detect_multiplayer(self.root))

        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "server_main.lua").write_text("return true\n")
        self._write_project(settings={}, manifest={})
        self.assertTrue(detect_multiplayer(self.root))

        self._write_project(settings={}, manifest={})
        (self.root / "scripts" / "server_main.lua").unlink()
        self.assertFalse(detect_multiplayer(self.root))

    def test_network_identity_resolves_version_sources(self) -> None:
        self._write_project(
            settings={"build": {"output_dir": "../dist"}},
            manifest={"project_id": "proj-9", "author": {"id": "42"}, "version": "1.0.0"},
        )
        identity = network_identity(self.root)
        self.assertEqual((identity.project_id, identity.version, identity.author_id), ("proj-9", "1.0.0", "42"))

        (self.root / "dist").mkdir()
        (self.root / "dist" / "latest.json").write_text(
            json.dumps({"version": "2.4.1-test"}), encoding="utf-8"
        )
        identity = network_identity(self.root)
        self.assertEqual(identity.version, "2.4.1-test")

        identity = network_identity(self.root, explicit_version="9.9.9")
        self.assertEqual(identity.version, "9.9.9")

    def test_network_identity_rejects_local_preview_identity(self) -> None:
        self._write_project(
            settings={},
            manifest={"project_id": "x", "author": {"id": "local-preview"}, "version": "1.0.0"},
        )
        with self.assertRaises(WorkspaceError) as caught:
            network_identity(self.root)
        self.assertIn("提交构建", str(caught.exception))

    def test_network_identity_requires_project_id(self) -> None:
        self._write_project(settings={}, manifest={"version": "1.0.0"})
        with self.assertRaises(WorkspaceError) as caught:
            network_identity(self.root)
        self.assertIn("缺少游戏配置", str(caught.exception))

    def test_network_identity_rejects_invalid_version(self) -> None:
        self._write_project(
            settings={}, manifest={"project_id": "p", "version": "has space"}
        )
        with self.assertRaises(WorkspaceError) as caught:
            network_identity(self.root)
        self.assertIn("测试构建版本", str(caught.exception))


class DirectConnectUrlTest(unittest.TestCase):
    def test_direct_connect_param_encoding(self) -> None:
        server = {
            "userId": 424242,
            "connectInfo": {"pod_ip": "pod.x", "server_port": 0, "ws_port": 47001},
        }
        encoded = direct_connect_param(server)
        self.assertEqual(json.loads(base64.b64decode(encoded)), server)
        self.assertNotIn(b" ", encoded.encode())

    def test_server_url_includes_direct_connect_param(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        try:
            root = Path(temporary.name)
            (root / "scripts").mkdir(parents=True)
            (root / "scripts/main.lua").write_text("return true\n", encoding="utf-8")
            from tapmaker_local_web.config import direct_project

            state = LocalWebProject(direct_project(root, "scripts/main.lua"))
            direct_connect = {
                "userId": 7,
                "connectInfo": {"pod_ip": "pod-a", "server_port": 0, "ws_port": 9999},
            }
            server = LocalWebServer(("127.0.0.1", 0), state, direct_connect=direct_connect)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertIn("directConnectParams=", server.url)
                encoded = unquote(server.url.split("directConnectParams=")[1])
                self.assertEqual(json.loads(base64.b64decode(encoded)), direct_connect)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            temporary.cleanup()


class SessionRecordTest(unittest.TestCase):
    def test_record_roundtrip_with_test_server(self) -> None:
        from tapmaker_local_web.session import SessionRecord

        record = SessionRecord(
            pid=1, port=2, token="t", url="u", project="p", entry="e", started_at="s",
            test_server={"pod_ip": "a", "ws_port": 1, "user_id": 2, "version": "1.0.0"},
        )
        restored = SessionRecord.from_dict(json.loads(json.dumps(record.to_dict())))
        self.assertEqual(restored.test_server, record.test_server)

        plain = SessionRecord.from_dict(
            {"pid": 1, "port": 2, "token": "t", "url": "u", "project": "p", "entry": "e", "started_at": ""}
        )
        self.assertIsNone(plain.test_server)


if __name__ == "__main__":
    unittest.main()
