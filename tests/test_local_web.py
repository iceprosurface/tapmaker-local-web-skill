from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
import zlib

from tapmaker_local_web import Workspace
from tapmaker_local_web.server import (
    LocalWebProject,
    LocalWebServer,
    current_web_runtime,
    sync_web_runtime,
)


class LocalWebContractTest(unittest.TestCase):
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
        (project / "scripts/main.lua.meta").write_text(
            '{"uuid":"main-script-id"}\n', encoding="utf-8"
        )
        (project / "assets/hero.png").write_bytes(b"png-data")
        (project / "tapmaker.toml").write_text(
            """schema = 1
name = "demo"
entry = "main.lua"
test = ["true"]
check = ["true"]

[[mounts]]
source = "scripts"
target = "scripts"

[[mounts]]
source = "assets"
target = "assets"
""",
            encoding="utf-8",
        )
        self.project = Workspace(self.root).project("demo")
        self.state = LocalWebProject(self.project)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_manifest_preserves_meta_uuid_and_maps_mounted_paths(self) -> None:
        files = {item["fs_path"]: item for item in self.state.manifest()["files"]}
        original_content = b"return true\n"

        self.assertEqual(files["main.lua"]["uuid"], "main-script-id")
        self.assertEqual(
            files["__tapmaker_project_entry.lua"]["hash"],
            f"{zlib.crc32(original_content):08x}",
        )
        self.assertEqual(files["hero.png"]["size"], len(b"png-data"))
        self.assertNotIn("main.lua.meta", files)
        self.assertEqual(self.state.manifest()["entry"], "main.lua")

    def test_default_platform_mock_wraps_entry_with_local_account_and_cloud(self) -> None:
        files = {item["fs_path"]: item for item in self.state.manifest()["files"]}
        wrapper = files["main.lua"]
        asset_name = f"{wrapper['uuid']}-{wrapper['hash']}{wrapper['ext']}"
        source = self.state.asset(asset_name)

        self.assertIsNotNone(source)
        content = source.read().decode("utf-8")
        self.assertIn("function lobby.GetMyUserId()", content)
        self.assertIn("function clientCloud:Get(key, events)", content)
        self.assertIn("function clientCloud:Set(key, value, events)", content)
        self.assertIn('require("__tapmaker_project_entry")', content)

    def test_platform_mock_can_be_disabled_for_runtime_diagnostics(self) -> None:
        state = LocalWebProject(self.project, platform_mock=False)

        self.assertEqual(state.manifest()["entry"], "main.lua")
        paths = {item["fs_path"] for item in state.manifest()["files"]}
        self.assertNotIn("__tapmaker_project_entry.lua", paths)

    def test_refresh_changes_revision_and_asset_url_after_source_change(self) -> None:
        before = {item["fs_path"]: item for item in self.state.manifest()["files"]}
        revision = self.state.revision
        version = self.state.version
        client = self.state.client

        (self.root / "apps/demo/scripts/main.lua").write_text("return false\n", encoding="utf-8")

        self.assertTrue(self.state.refresh())
        after = {item["fs_path"]: item for item in self.state.manifest()["files"]}
        self.assertGreater(self.state.revision, revision)
        self.assertEqual(self.state.version, version)
        self.assertNotEqual(self.state.client, client)
        self.assertNotEqual(
            before["__tapmaker_project_entry.lua"]["hash"],
            after["__tapmaker_project_entry.lua"]["hash"],
        )

    def test_new_server_state_reuses_version_with_a_fresh_manifest_client(self) -> None:
        before = self.state.latest()
        state = LocalWebProject(self.project)

        self.assertEqual(state.latest()["version"], before["version"])
        self.assertNotEqual(state.latest()["client"], before["client"])

    def test_concurrent_revision_requests_share_one_filesystem_scan(self) -> None:
        scans = 0
        original = self.state._collect_candidates

        def counted_collect():
            nonlocal scans
            scans += 1
            return original()

        self.state._collect_candidates = counted_collect
        self.state._last_refresh_at = 0
        server = LocalWebServer(("127.0.0.1", 0), self.state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        barrier = threading.Barrier(8)
        failures = []

        def request_revision() -> None:
            try:
                barrier.wait()
                with urlopen(f"{base}/__tapmaker/revision") as response:
                    self.assertEqual(response.status, 200)
            except BaseException as error:
                failures.append(error)

        requests = [threading.Thread(target=request_revision) for _ in range(8)]
        try:
            for request in requests:
                request.start()
            for request in requests:
                request.join(timeout=2)
            self.assertFalse(failures)
            self.assertEqual(scans, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_refresh_ignores_operating_system_metadata_in_layered_mounts(self) -> None:
        (self.root / "apps/demo/scripts/.DS_Store").write_bytes(b"scripts metadata")
        (self.root / "apps/demo/assets/.DS_Store").write_bytes(b"assets metadata")

        self.assertFalse(self.state.refresh())
        paths = {item["fs_path"] for item in self.state.manifest()["files"]}
        self.assertNotIn(".DS_Store", paths)

    def test_http_server_exposes_manifest_assets_and_cross_origin_headers(self) -> None:
        server = LocalWebServer(("127.0.0.1", 0), self.state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(
                f"{base}/{self.state.version}/manifest-{self.state.client}.json"
            ) as response:
                manifest = json.load(response)
                self.assertEqual(response.headers["Cross-Origin-Embedder-Policy"], "require-corp")
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
            main = next(
                item
                for item in manifest["files"]
                if item["fs_path"] == "__tapmaker_project_entry.lua"
            )
            asset_name = f"{main['uuid']}-{main['hash']}{main['ext']}"
            with urlopen(f"{base}/assets/{asset_name}") as response:
                self.assertEqual(response.read(), b"return true\n")
            with self.assertRaises(HTTPError) as missing:
                urlopen(f"{base}/assets/../tapmaker.toml")
            self.assertEqual(missing.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_player_errors_expose_a_close_action(self) -> None:
        server = LocalWebServer(("127.0.0.1", 0), self.state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/") as response:
                page = response.read().decode("utf-8")

            self.assertIn("installDismissibleErrorDialogs", page)
            self.assertIn("callbackId === -1 && !cancelText", page)
            self.assertIn("关闭", page)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_runtime_sync_caches_verified_engine_files_and_serves_them_locally(self) -> None:
        source = self.root / "runtime-source"
        assets = source / "assets"
        version_dir = source / "1.2.3"
        assets.mkdir(parents=True)
        version_dir.mkdir()
        contents = {
            "UrhoXRuntime.js": b"runtime js",
            "UrhoXRuntime.wasm": b"runtime wasm",
            "UrhoXRuntime.data": b"runtime data",
        }
        files = []
        for name, content in contents.items():
            checksum = f"{zlib.crc32(content) & 0xFFFFFFFF:08x}"
            extension = Path(name).suffix
            resource_uuid = f"uuid-{extension.removeprefix('.')}"
            (assets / f"{resource_uuid}-{checksum}{extension}").write_bytes(content)
            files.append(
                {
                    "uuid": resource_uuid,
                    "ext": extension,
                    "hash": checksum,
                    "size": len(content),
                    "fs_path": name,
                }
            )
        (source / "latest.json").write_text(
            json.dumps({"version": "1.2.3", "client": "client-id"}), encoding="utf-8"
        )
        (version_dir / "manifest-client-id.json").write_text(
            json.dumps({"files": files}), encoding="utf-8"
        )

        cache = self.root / "runtime-cache"
        runtime = sync_web_runtime(cache, engine_base_url=source.as_uri())

        self.assertEqual(current_web_runtime(cache), runtime)
        self.assertEqual((runtime / "UrhoXRuntime.wasm").read_bytes(), b"runtime wasm")
        server = LocalWebServer(("127.0.0.1", 0), self.state, runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertIn("local_engine=true", server.url)
            self.assertIn("screen_orientation=landscape", server.url)
            with urlopen(f"http://127.0.0.1:{server.server_port}/UrhoXRuntime.wasm") as response:
                self.assertEqual(response.headers["Content-Type"], "application/wasm")
                self.assertEqual(response.read(), b"runtime wasm")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
