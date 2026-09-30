"""Offline graph, corruption, pinning and HTTP contracts; no CDN needed."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen
import zlib

from tapmaker_local_web.config import WorkspaceError, direct_project
from tapmaker_local_web.offline import (
    OfflineBundle,
    ORIGIN,
    PREFIX,
    PLAYER_PATHS,
    asset_path,
    digest,
    json_bytes,
    sync_offline,
)
from tapmaker_local_web.server import (
    LocalWebProject,
    LocalWebServer,
    current_web_runtime,
    sync_web_runtime,
)


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.remote = {}
        self.manifests = {}
        for name in (
            "engine",
            "engine-res",
            "engine-startup",
            "urhox-libs",
            "official-res",
        ):
            files = []
            paths = {
                "engine": ["UrhoXRuntime.js", "UrhoXRuntime.wasm", "UrhoXRuntime.data"],
                "engine-res": ["urhox-libs/UI.lua"],
                "engine-startup": ["engine-startup/main.lua"],
                "urhox-libs": ["UI.lua"],
                "official-res": [
                    "Shaders/example.glsl",
                    "Characters/hero.xml",
                    "Textures/hero.png",
                ],
            }[name]
            for i, path in enumerate(paths):
                data = (name + path).encode()
                item = {
                    "uuid": f"{name}-{i}",
                    "hash": f"{zlib.crc32(data):08x}",
                    "size": len(data),
                    "ext": Path(path).suffix,
                    "fs_path": path,
                }
                if name == "official-res":
                    item["groups"] = ["official-shaders" if i == 0 else "characters"]
                    if i == 0:
                        item["groups"].append("official-shadercache")
                    if i == 1:
                        item["refs"] = ["official-res-2"]
                files.append(item)
                self.remote[asset_path(name, item)] = data
            self.manifests[name] = {
                "files": files,
                "sources": {},
                "preload_groups": [],
                "paks": ["do-not-fetch"],
            }
            version = {"version": "1.0", "client": name}
            self.remote[f"/src/{name}/stable.json"] = json_bytes(version)
            self.remote[f"/src/{name}/latest.json"] = json_bytes(version)
            self.remote[f"/src/{name}/wasm-1.0.json"] = json_bytes(version)
            self.remote[f"/src/{name}/1.0/manifest-{name}.json"] = json_bytes(
                self.manifests[name]
            )
        for path in PLAYER_PATHS:
            self.remote[path] = (f'const cdn="{ORIGIN}";').encode()
        self.calls = []
        self.fetch = patch("tapmaker_local_web.offline._get", side_effect=self.get)
        self.fetch.start()

    def get(self, path, **kwargs):
        self.calls.append(path)
        return self.remote[path]

    def tearDown(self):
        self.fetch.stop()
        self.temp.cleanup()

    def sync(self, **kwargs):
        return sync_offline(self.cache, **kwargs)

    def test_default_is_bounded_and_does_not_download_unused_official_assets(self):
        bundle = OfflineBundle(self.cache, self.sync())
        self.assertEqual(len(bundle.runtime_paths), 3)
        path = asset_path("official-res", self.manifests["official-res"]["files"][1])
        self.assertNotIn(path, self.calls)
        with self.assertRaisesRegex(WorkspaceError, "not prepared"):
            bundle.raw(path)

    def test_literal_selection_follows_reference_closure(self):
        bundle = OfflineBundle(
            self.cache, self.sync(texts='load("Characters/hero.xml")')
        )
        for f in self.manifests["official-res"]["files"]:
            self.assertEqual(
                bundle.raw(asset_path("official-res", f)),
                self.remote[asset_path("official-res", f)],
            )

    def test_explicit_dynamic_resource_and_group_selection(self):
        for kwargs in (
            {"resources": ["Characters/hero.xml"]},
            {"groups": ["characters"]},
        ):
            bundle = OfflineBundle(self.cache, self.sync(**kwargs))
            self.assertIn(
                asset_path("official-res", self.manifests["official-res"]["files"][2]),
                bundle.files,
            )

    def test_unknown_selectors_fail_before_asset_download(self):
        for kwargs in ({"resources": ["missing"]}, {"groups": ["missing"]}):
            with self.assertRaisesRegex(WorkspaceError, "Unknown official"):
                self.sync(**kwargs)
        self.assertFalse(any("/assets/" in p for p in self.calls))

    def test_size_budget_rejects_before_asset_download(self):
        with self.assertRaisesRegex(WorkspaceError, "exceeds"):
            self.sync(max_bytes=1)
        self.assertFalse(any("/assets/" in p for p in self.calls))

    def test_same_size_corruption_and_missing_object_fail_status(self):
        for delete in (False, True):
            bundle = OfflineBundle(self.cache, self.sync())
            path = self.cache / "objects" / bundle.files[PLAYER_PATHS[0]]["sha256"]
            if delete:
                path.unlink()
            else:
                path.write_bytes(b"x" * path.stat().st_size)
            with self.assertRaisesRegex(WorkspaceError, "cache (corrupt|missing)"):
                OfflineBundle(self.cache)

    def test_corrupt_download_never_publishes_snapshot(self):
        self.sync()
        previous = (self.cache / "current").read_text()
        f = self.manifests["engine"]["files"][0]
        (self.cache / "downloads.json").unlink()
        self.remote[asset_path("engine", f)] = b"x" * f["size"]
        with self.assertRaisesRegex(WorkspaceError, "checksum mismatch"):
            self.sync()
        self.assertEqual(previous, (self.cache / "current").read_text())

    def test_pinned_snapshot_survives_player_update_without_network(self):
        first = self.sync()
        self.remote[PLAYER_PATHS[0]] = b"new player"
        second = self.sync()
        self.assertNotEqual(first, second)
        with patch(
            "tapmaker_local_web.offline._get",
            side_effect=AssertionError("network forbidden"),
        ):
            old = OfflineBundle(self.cache, first)
            self.assertNotEqual(
                old.raw(PLAYER_PATHS[0]), OfflineBundle(self.cache).raw(PLAYER_PATHS[0])
            )

    def test_modified_metadata_is_rejected(self):
        key = self.sync()
        p = self.cache / "snapshots" / f"{key}.json"
        p.write_bytes(p.read_bytes().replace(b"1.0", b"2.0"))
        with self.assertRaisesRegex(WorkspaceError, "metadata checksum"):
            OfflineBundle(self.cache)

    def test_version_mixing_rejected_even_with_new_snapshot_id(self):
        key = self.sync()
        data = json.loads((self.cache / "snapshots" / f"{key}.json").read_bytes())
        data["versions"]["engine"]["version"] = "2.0"
        body = json_bytes(data)
        new = digest(body)
        (self.cache / "snapshots" / f"{new}.json").write_bytes(body)
        with self.assertRaisesRegex(WorkspaceError, "version mismatch"):
            OfflineBundle(self.cache, new)

    def test_paths_cannot_escape_cache(self):
        with self.assertRaises(WorkspaceError):
            OfflineBundle(self.cache, "../../elsewhere")
        with self.assertRaises(WorkspaceError):
            asset_path("engine", {"uuid": "../escape", "hash": "123", "ext": ".js"})

    def test_declared_platform_variants_are_cached_and_verified(self):
        f = self.manifests["engine-res"]["files"][0]
        data = b"platform variant"
        f["hash@windows"] = f"{zlib.crc32(data):08x}"
        f["size@windows"] = len(data)
        variant = {**f, "hash": f["hash@windows"], "size": len(data)}
        path = asset_path("engine-res", variant)
        self.remote[path] = data
        self.remote["/src/engine-res/1.0/manifest-engine-res.json"] = json_bytes(
            self.manifests["engine-res"]
        )
        bundle = OfflineBundle(self.cache, self.sync())
        self.assertEqual(bundle.raw(path), data)
        (self.cache / "objects" / bundle.files[path]["sha256"]).write_bytes(
            b"x" * len(data)
        )
        with self.assertRaisesRegex(WorkspaceError, "corrupt"):
            OfflineBundle(self.cache)

    def test_missing_wasm_abi_descriptor_is_rejected(self):
        key = self.sync()
        p = self.cache / "snapshots" / f"{key}.json"
        value = json.loads(p.read_bytes())
        del value["files"]["/src/engine-res/wasm-1.0.json"]
        body = json_bytes(value)
        key = digest(body)
        (self.cache / "snapshots" / f"{key}.json").write_bytes(body)
        with self.assertRaisesRegex(WorkspaceError, "not prepared"):
            OfflineBundle(self.cache, key)

    def test_http_serves_local_player_manifests_and_fails_closed(self):
        bundle = OfflineBundle(self.cache, self.sync())
        project = self.root / "project"
        project.mkdir()
        (project / "main.lua").write_text("return true")
        state = LocalWebProject(direct_project(project, "main.lua"))
        server = LocalWebServer(("127.0.0.1", 0), state, offline=bundle)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch(
                "tapmaker_local_web.offline._get",
                side_effect=AssertionError("network forbidden"),
            ):
                with urlopen(server.url) as r:
                    html = r.read()
                    self.assertIn(b"local_engine=true", server.url.encode())
                    self.assertNotIn(('src="' + ORIGIN).encode(), html)
                    self.assertIn(
                        "connect-src 'self'", r.headers["Content-Security-Policy"]
                    )
                with urlopen(base + PREFIX + PLAYER_PATHS[0]) as r:
                    self.assertIn((base + PREFIX).encode(), r.read())
                with urlopen(
                    base + PREFIX + "/src/engine/1.0/manifest-engine.json"
                ) as r:
                    self.assertNotIn("paks", json.load(r))
                with urlopen(base + "/UrhoXRuntime.wasm") as r:
                    self.assertEqual("application/wasm", r.headers["Content-Type"])
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + PREFIX + "/src/official-res/assets/not-cached")
                self.assertEqual(error.exception.code, 503)
                self.assertIn(b"not prepared", error.exception.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class RuntimeIntegrityTest(unittest.TestCase):
    def test_sync_repairs_same_size_corruption_and_status_rejects_incomplete_cache(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            assets = source / "assets"
            assets.mkdir(parents=True)
            (source / "1").mkdir()
            files = []
            for name in ("UrhoXRuntime.js", "UrhoXRuntime.wasm", "UrhoXRuntime.data"):
                data = name.encode()
                crc = f"{zlib.crc32(data):08x}"
                f = {
                    "uuid": name,
                    "hash": crc,
                    "size": len(data),
                    "ext": Path(name).suffix,
                    "fs_path": name,
                }
                files.append(f)
                (assets / f"{name}-{crc}{f['ext']}").write_bytes(data)
            (source / "latest.json").write_text('{"version":"1","client":"a"}')
            (source / "1/manifest-a.json").write_bytes(json_bytes({"files": files}))
            cache = root / "cache"
            runtime = sync_web_runtime(cache, engine_base_url=source.as_uri())
            target = runtime / "UrhoXRuntime.wasm"
            target.write_bytes(b"x" * target.stat().st_size)
            self.assertIsNone(current_web_runtime(cache))
            sync_web_runtime(cache, engine_base_url=source.as_uri())
            self.assertEqual(target.read_bytes(), b"UrhoXRuntime.wasm")
            self.assertEqual(current_web_runtime(cache), runtime)
            target.unlink()
            self.assertIsNone(current_web_runtime(cache))
            (runtime / "runtime.json").write_text("{}")
            self.assertIsNone(current_web_runtime(cache))


if __name__ == "__main__":
    unittest.main()
