from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tapmaker_local_web import Workspace, cli, session
from tapmaker_local_web.config import direct_project
from tapmaker_local_web.control import ControlError, ControlPlane
from tapmaker_local_web.server import (
    LocalWebProject,
    LocalWebServer,
    parse_viewport_size,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"image-payload"


def _png_data_url(data: bytes = PNG_BYTES) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _post_json(url: str, payload: object, token: str | None = None, host: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-TapMaker-Control"] = token
    if host is not None:
        headers["Host"] = host
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        body = error.read()
        try:
            return error.code, json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return error.code, {"raw": body.decode("utf-8", "replace")}


class ControlPlaneContractTest(unittest.TestCase):
    def test_authorize_requires_exact_token(self) -> None:
        control = ControlPlane("token-a")
        self.assertTrue(control.authorize("token-a"))
        self.assertFalse(control.authorize("token-b"))
        self.assertFalse(control.authorize(None))
        self.assertFalse(control.authorize(""))

    def test_record_logs_validates_and_counts_levels(self) -> None:
        control = ControlPlane()
        accepted = control.record_logs(
            [
                {"level": "info", "message": "hello"},
                {"level": "warn", "message": "careful"},
                {"level": "error", "message": "boom"},
                {"message": "defaults"},
            ]
        )
        self.assertEqual(accepted, 4)
        logs = control.logs(10)
        self.assertEqual(len(logs["lines"]), 4)
        self.assertEqual(logs["total"], 4)
        self.assertEqual(logs["counts"], {"info": 2, "warn": 1, "error": 1})
        self.assertTrue(all(line["ts"] for line in logs["lines"]))

        with self.assertRaises(ControlError):
            control.record_logs("not-a-list")
        with self.assertRaises(ControlError):
            control.record_logs([{"level": "debug", "message": "no"}])
        with self.assertRaises(ControlError):
            control.record_logs([{"level": "info"}])

    def test_record_logs_reports_dropped_lines(self) -> None:
        control = ControlPlane(log_limit=3)
        control.record_logs([{"level": "info", "message": f"m{i}"} for i in range(5)])
        logs = control.logs(10)
        self.assertEqual(logs["total"], 5)
        self.assertEqual(logs["dropped"], 2)
        self.assertEqual([line["message"] for line in logs["lines"]], ["m2", "m3", "m4"])

    def test_mark_keeps_first_timestamp_and_validates_names(self) -> None:
        control = ControlPlane()
        control.mark("entry_served")
        first = control.milestones()["entry_served"]
        control.mark("entry_served")
        self.assertEqual(control.milestones()["entry_served"], first)
        with self.assertRaises(ControlError):
            control.mark("not valid!")
        with self.assertRaises(ControlError):
            control.mark(123)

    def test_store_screenshot_validates_payload(self) -> None:
        control = ControlPlane()
        with self.assertRaises(ControlError):
            control.store_screenshot("", _png_data_url())
        with self.assertRaises(ControlError):
            control.store_screenshot("id", "data:image/jpeg;base64," + "AAAA")
        with self.assertRaises(ControlError):
            control.store_screenshot("id", "data:image/png;base64," + "!!!not-base64!!!")
        with self.assertRaises(ControlError):
            control.store_screenshot("id", "data:image/png;base64," + base64.b64encode(b"junk").decode())

        shot = control.store_screenshot("shot-1", _png_data_url())
        self.assertEqual(shot.data, PNG_BYTES)
        status = control.screenshot_status()
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["id"], "shot-1")
        self.assertEqual(control.screenshot_bytes(), PNG_BYTES)

    def test_broadcast_increments_sequence_and_returns_payload(self) -> None:
        control = ControlPlane()
        self.assertEqual(control.command_seq(), 0)
        first = control.broadcast("screenshot")
        second = control.broadcast("screenshot")
        self.assertEqual(control.command_seq(), 2)
        self.assertEqual(first["seq"], 1)
        self.assertEqual(second["seq"], 2)
        self.assertEqual(control.last_command()["id"], second["id"])


class PreviewServerContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "tapmaker.workspace.toml").write_text(
            'schema = 1\n[projects.demo]\npath = "apps/demo"\n', encoding="utf-8"
        )
        project = self.root / "apps/demo"
        (project / "scripts").mkdir(parents=True)
        (project / "assets").mkdir()
        (project / "scripts/main.lua").write_text("return true\n", encoding="utf-8")
        (project / "tapmaker.toml").write_text(
            """schema = 1
name = "demo"
entry = "main.lua"

[[mounts]]
source = "scripts"
target = "scripts"

[[mounts]]
source = "assets"
target = "assets"
""",
            encoding="utf-8",
        )
        self.state = LocalWebProject(Workspace(self.root).project("demo"))
        self.server = LocalWebServer(("127.0.0.1", 0), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = self.server.control.token

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    # -- 鉴权 ---------------------------------------------------------------

    def test_control_endpoints_require_token_and_loopback_host(self) -> None:
        status, body = _post_json(f"{self.base}/__tapmaker/control/refresh", {})
        self.assertEqual(status, 403)

        status, body = _post_json(f"{self.base}/__tapmaker/control/refresh", {}, token="wrong")
        self.assertEqual(status, 403)

        status, body = _post_json(
            f"{self.base}/__tapmaker/control/refresh", {}, token=self.token, host="evil.example"
        )
        self.assertEqual(status, 403)
        self.assertIn("Host", body["error"])

        status, body = _post_json(
            f"{self.base}/__tapmaker/control/refresh", {}, token=self.token
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["reloaded"])

    def test_unknown_control_path_returns_404(self) -> None:
        status, _ = _post_json(f"{self.base}/__tapmaker/control/nope", {}, token=self.token)
        self.assertEqual(status, 404)

    def test_invalid_json_body_returns_400(self) -> None:
        request = Request(
            f"{self.base}/__tapmaker/report/milestone",
            data=b"not-json",
            headers={
                "Content-Type": "application/json",
                "X-TapMaker-Control": self.token,
            },
            method="POST",
        )
        try:
            urlopen(request, timeout=5)
            self.fail("expected HTTP 400")
        except HTTPError as error:
            self.assertEqual(error.code, 400)

    # -- 刷新与关停 -----------------------------------------------------------

    def test_refresh_bumps_revision(self) -> None:
        with urlopen(f"{self.base}/__tapmaker/revision") as response:
            before = json.load(response)["revision"]
        _post_json(f"{self.base}/__tapmaker/control/refresh", {}, token=self.token)
        with urlopen(f"{self.base}/__tapmaker/revision") as response:
            after = json.load(response)["revision"]
        self.assertGreater(after, before)

    def test_shutdown_stops_the_server(self) -> None:
        status, body = _post_json(
            f"{self.base}/__tapmaker/control/shutdown", {}, token=self.token
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["stopping"])
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    # -- 日志与里程碑 -----------------------------------------------------------

    def test_logs_round_trip_through_report_and_fetch(self) -> None:
        status, body = _post_json(
            f"{self.base}/__tapmaker/report/logs",
            {"lines": [{"level": "error", "message": "engine boom"}, {"message": "plain"}]},
            token=self.token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 2)

        with urlopen(f"{self.base}/__tapmaker/logs?lines=50") as response:
            logs = json.load(response)
        self.assertEqual(logs["total"], 2)
        self.assertEqual(logs["lines"][-1]["message"], "plain")
        self.assertEqual(logs["counts"]["error"], 1)

        status, _ = _post_json(
            f"{self.base}/__tapmaker/report/milestone", {"name": "bad name!"}, token=self.token
        )
        self.assertEqual(status, 400)

    def test_health_and_check_summarize_server_state(self) -> None:
        with urlopen(f"{self.base}/__tapmaker/health") as response:
            health = json.load(response)
        self.assertTrue(health["ok"])
        self.assertEqual(health["project"], self.state.project.name)

        with urlopen(f"{self.base}/__tapmaker/check") as response:
            check = json.load(response)
        self.assertFalse(check["started"])
        self.assertIn("no_connected_page", check["reasons"])
        self.assertIn("entry_not_served", check["reasons"])
        self.assertEqual(check["viewport"], {"width": 844, "height": 390})
        self.assertIn("/console", check["console_url"])

    def test_serving_entry_asset_marks_entry_served(self) -> None:
        manifest = self.state.manifest()
        entry = next(
            item for item in manifest["files"] if item["fs_path"] == self.state.deployment.entry
        )
        asset_name = f"{entry['uuid']}-{entry['hash']}{entry['ext']}"
        with urlopen(f"{self.base}/assets/{asset_name}") as response:
            self.assertEqual(response.status, 200)
        milestones = self.server.control.milestones()
        self.assertIn("first_asset_served", milestones)
        self.assertIn("entry_served", milestones)

    # -- 截图 -----------------------------------------------------------------

    def test_screenshot_requires_connected_page_then_stores_png(self) -> None:
        status, body = _post_json(
            f"{self.base}/__tapmaker/control/screenshot", {}, token=self.token
        )
        self.assertEqual(status, 409)

        events = urlopen(f"{self.base}/__tapmaker/events", timeout=3)
        try:
            self.assertEqual(events.readline(), b"event: revision\n")
            events.readline()
            events.readline()
            deadline = time.monotonic() + 3
            while self.server.control.connected_pages == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(self.server.control.connected_pages, 1)

            status, body = _post_json(
                f"{self.base}/__tapmaker/control/screenshot", {}, token=self.token
            )
            self.assertEqual(status, 200)
            requested_id = body["id"]

            command_line = events.readline()
            self.assertEqual(command_line, b"event: command\n")
            payload = json.loads(events.readline().removeprefix(b"data: "))
            self.assertEqual(payload["name"], "screenshot")
            self.assertEqual(payload["id"], requested_id)

            status, body = _post_json(
                f"{self.base}/__tapmaker/report/screenshot",
                {"id": requested_id, "data_url": _png_data_url()},
                token=self.token,
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["size"], len(PNG_BYTES))

            with urlopen(f"{self.base}/__tapmaker/screenshot") as response:
                info = json.load(response)
            self.assertEqual(info["status"], "ready")
            self.assertEqual(info["id"], requested_id)
            with urlopen(f"{self.base}/__tapmaker/screenshot.png") as response:
                self.assertEqual(response.read(), PNG_BYTES)
                self.assertEqual(response.headers["Content-Type"], "image/png")
        finally:
            events.close()

    # -- 页面壳与控制台 -----------------------------------------------------------

    def test_index_page_embeds_control_hooks_and_token(self) -> None:
        with urlopen(f"{self.base}/") as response:
            page = response.read()
        self.assertIn(f'content="{self.token}"'.encode(), page)
        self.assertIn(b"tapmaker-control-token", page)
        self.assertIn(b"window.__tapmakerScreenshot", page)
        self.assertIn(b"preserveDrawingBuffer", page)
        self.assertIn(b"/__tapmaker/report/logs", page)
        self.assertIn(b"addEventListener('command'", page)
        self.assertIn(b'href="/console"', page)

    def test_console_page_serves_management_ui(self) -> None:
        with urlopen(f"{self.base}/console") as response:
            page = response.read()
        self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(f'content="{self.token}"'.encode(), page)
        self.assertIn(b"/__tapmaker/check", page)
        self.assertIn(b"/__tapmaker/control/refresh", page)
        self.assertIn(b"/__tapmaker/control/screenshot", page)
        self.assertIn(b"/__tapmaker/logs", page)

    def test_custom_viewport_size_updates_canvas_ratio(self) -> None:
        server = LocalWebServer(("127.0.0.1", 0), self.state, size=(1260, 540))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/") as response:
                page = response.read()
            self.assertIn(b"--tapmaker-viewport-width: 1260", page)
            self.assertIn(b"--tapmaker-viewport-height: 540", page)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_parse_viewport_size_validation(self) -> None:
        self.assertEqual(parse_viewport_size("1260x540"), (1260, 540))
        with self.assertRaises(Exception):
            parse_viewport_size("1260")
        with self.assertRaises(Exception):
            parse_viewport_size("99x540")
        with self.assertRaises(Exception):
            parse_viewport_size("1260x5000")
        with self.assertRaises(Exception):
            parse_viewport_size("axb")


class PreviewCliDetachedTest(unittest.TestCase):
    """覆盖 preview start --detached 与管理动词的完整生命周期。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "scripts/main.lua").write_text("return true\n", encoding="utf-8")
        self.state_root = self.root / "state"
        self.env = mock.patch.dict(
            "os.environ", {"TAPMAKER_LOCAL_WEB_STATE_ROOT": str(self.state_root)}
        )
        self.env.start()

    def tearDown(self) -> None:
        record = session.read_record(self._project_name())
        if record is not None and session.pid_alive(record.pid):
            os_kill(record.pid)
        self.env.stop()
        self.temporary.cleanup()

    def _project_name(self) -> str:
        return direct_project(self.root, "scripts/main.lua").name

    def _run(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            code = cli.main(["preview", *argv])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_detached_lifecycle_status_refresh_logs_check_stop(self) -> None:
        code, stdout, stderr = self._run(
            "start",
            "--code",
            str(self.root),
            "--entry",
            "scripts/main.lua",
            "--detached",
            "--no-open",
            "--port",
            "0",
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("后台", stdout)

        record = session.read_record(self._project_name())
        self.assertIsNotNone(record)
        self.assertTrue(session.pid_alive(record.pid))
        health = session.health(record)
        self.assertIsNotNone(health)
        self.assertTrue(health["ok"])

        code, stdout, status_stderr = self._run(
            "status", "--code", str(self.root), "--entry", "scripts/main.lua"
        )
        self.assertEqual(code, 0, status_stderr)
        self.assertIn('"running": true', stdout)
        self.assertNotIn(record.token, stdout)

        with urlopen(record.base_url() + "/__tapmaker/revision") as response:
            before = json.load(response)["revision"]
        code, _, _ = self._run(
            "refresh", "--code", str(self.root), "--entry", "scripts/main.lua"
        )
        self.assertEqual(code, 0)
        with urlopen(record.base_url() + "/__tapmaker/revision") as response:
            self.assertGreater(json.load(response)["revision"], before)

        status_code, _ = session.control_post(
            record,
            "/__tapmaker/report/logs",
            {"lines": [{"level": "info", "message": "cli-log-line"}]},
        )
        self.assertEqual(status_code, 200)
        code, stdout, _ = self._run(
            "logs", "--code", str(self.root), "--entry", "scripts/main.lua"
        )
        self.assertEqual(code, 0)
        self.assertIn("cli-log-line", stdout)

        code, stdout, _ = self._run(
            "check", "--code", str(self.root), "--entry", "scripts/main.lua"
        )
        self.assertEqual(code, 1)
        self.assertIn("no_connected_page", stdout)

        code, stdout, stderr = self._run(
            "screenshot", "--code", str(self.root), "--entry", "scripts/main.lua"
        )
        self.assertEqual(code, 2)
        self.assertIn("没有已连接的预览页面", stderr)

        code, stdout, _ = self._run(
            "stop", "--code", str(self.root), "--entry", "scripts/main.lua"
        )
        self.assertEqual(code, 0)
        self.assertFalse(session.pid_alive(record.pid))
        self.assertIsNone(session.read_record(self._project_name()))

        code, _, stderr = self._run(
            "stop", "--code", str(self.root), "--entry", "scripts/main.lua"
        )
        self.assertEqual(code, 2)
        self.assertIn("没有运行中的预览会话", stderr)


def os_kill(pid: int) -> None:
    import os
    import signal

    os.kill(pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
