"""Offline graph, corruption, pinning and HTTP contracts; no CDN needed."""

from __future__ import annotations

import json
import gzip
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen
import zlib

from tapmaker_local_web.cache import download
from tapmaker_local_web.config import WorkspaceError, direct_project
from tapmaker_local_web.offline import (
    _get,
    _OfficialRedirects,
    OfflineCache,
    ORIGIN,
    PREFIX,
    PLAYER_PATHS,
    asset_path,
    cache_path,
    json_bytes,
    sync_offline,
)
from tapmaker_local_web.server import (
    LocalWebProject,
    LocalWebServer,
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

        def open_asset(request, **kwargs):
            response = io.BytesIO(self.get(request.full_url.removeprefix(ORIGIN)))
            response.headers = {}
            return response

        self.download = patch(
            "tapmaker_local_web.offline.download",
            side_effect=lambda url, dest, expected, **kw: download(
                url, dest, expected, opener=open_asset
            ),
        )
        self.download.start()

    def get(self, path, **kwargs):
        self.calls.append(path)
        return self.remote[path]

    def tearDown(self):
        self.fetch.stop()
        self.download.stop()
        self.temp.cleanup()

    def sync(self, **kwargs):
        return sync_offline(self.cache, **kwargs)

    def test_default_is_bounded_and_does_not_download_unused_official_assets(self):
        bundle = OfflineCache(self.sync())
        self.assertEqual(len(bundle.runtime_paths), 3)
        path = asset_path("official-res", self.manifests["official-res"]["files"][1])
        self.assertNotIn(path, self.calls)
        with self.assertRaisesRegex(WorkspaceError, "not prepared"):
            bundle.raw(path)

    def test_literal_selection_follows_reference_closure(self):
        bundle = OfflineCache(self.sync(texts='load("Characters/hero.xml")'))
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
            bundle = OfflineCache(self.sync(**kwargs))
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

    def test_same_size_corruption_and_missing_files_fail_offline_status(self):
        paths = (
            PLAYER_PATHS[0],
            asset_path("engine", self.manifests["engine"]["files"][1]),
        )
        for name in paths:
            for delete in (False, True):
                with self.subTest(path=name, delete=delete):
                    self.sync()
                    path = cache_path(self.cache, name)
                    if delete:
                        path.unlink()
                    else:
                        path.write_bytes(b"x" * path.stat().st_size)
                    with self.assertRaisesRegex(
                        WorkspaceError, "cache (corrupt|incomplete)"
                    ):
                        OfflineCache(self.cache)

    def test_failed_sync_invalidates_readiness_and_retry_repairs(self):
        bundle = OfflineCache(self.sync())
        f = self.manifests["engine"]["files"][0]
        path = asset_path("engine", f)
        cache_path(self.cache, path).unlink()
        original = self.remote[path]
        self.remote[path] = b"x" * f["size"]
        with self.assertRaisesRegex(WorkspaceError, "checksum mismatch"):
            self.sync()
        self.assertFalse((self.cache / "cache.json").exists())
        self.assertFalse(list(self.cache.rglob("*.part")))
        with self.assertRaisesRegex(WorkspaceError, "changed or missing"):
            bundle.raw(PLAYER_PATHS[0])
        self.remote[path] = original
        self.assertEqual(OfflineCache(self.sync()).raw(path), original)

    def test_current_cache_replaces_player_and_serves_without_network(self):
        self.sync()
        self.remote[PLAYER_PATHS[0]] = b"new player"
        self.sync()
        with patch(
            "urllib.request.OpenerDirector.open",
            side_effect=AssertionError("network forbidden"),
        ):
            self.assertEqual(
                OfflineCache(self.cache).raw(PLAYER_PATHS[0]), b"new player"
            )
        self.assertEqual({p.name for p in self.cache.iterdir()}, {"src", "cache.json"})

    def test_missing_checksum_metadata_is_rejected(self):
        self.sync()
        p = self.cache / "cache.json"
        data = json.loads(p.read_bytes())
        del data["files"][PLAYER_PATHS[0]]["sha256"]
        p.write_bytes(json_bytes(data))
        with self.assertRaisesRegex(WorkspaceError, "corrupt"):
            OfflineCache(self.cache)

    def test_version_mixing_rejected(self):
        self.sync()
        p = self.cache / "cache.json"
        data = json.loads(p.read_bytes())
        data["versions"]["engine"]["version"] = "2.0"
        p.write_bytes(json_bytes(data))
        with self.assertRaisesRegex(WorkspaceError, "version mismatch"):
            OfflineCache(self.cache)

    def test_paths_cannot_escape_cache(self):
        for path in ("/src/../escape", "/src//escape", "/etc/passwd"):
            with self.assertRaises(WorkspaceError):
                cache_path(self.cache, path)
        with self.assertRaises(WorkspaceError):
            asset_path("engine", {"uuid": "../escape", "hash": "123", "ext": ".js"})

    def test_repeated_reads_do_not_rehash_and_mutation_fails_closed(self):
        bundle = OfflineCache(self.sync())
        path = PLAYER_PATHS[0]
        with patch(
            "tapmaker_local_web.offline.file_checks",
            side_effect=AssertionError("unexpected rehash"),
        ):
            first = bundle.render(path, "http://127.0.0.1:1")
            self.assertIs(first, bundle.render(path, "http://127.0.0.1:1"))
            bundle.raw(path)
            cache_path(self.cache, path).write_bytes(b"x" * len(self.remote[path]))
            with self.assertRaisesRegex(WorkspaceError, "changed"):
                bundle.render(path, "http://127.0.0.1:1")

    def test_unknown_manifest_origin_is_rejected_before_asset_download(self):
        manifest = self.manifests["official-res"]
        manifest["sources"] = {"engine-res": {"base_url": "https://unrelated.example/"}}
        self.remote["/src/official-res/1.0/manifest-official-res.json"] = json_bytes(
            manifest
        )
        with self.assertRaisesRegex(WorkspaceError, "Unsupported source origin"):
            self.sync()
        self.assertFalse(any("/assets/" in path for path in self.calls))

    def test_only_known_documentation_is_omitted_from_preload_manifest(self):
        manifest = self.manifests["engine-res"]
        for key, path in (("readme", "Fonts/read_me.html"), ("page", "UI/page.html")):
            data = b"<html>test</html>"
            item = {
                "uuid": key,
                "ext": ".html",
                "hash": f"{zlib.crc32(data):08x}",
                "size": len(data),
                "fs_path": path,
                "groups": ["default"],
            }
            manifest["files"].append(item)
            self.remote[asset_path("engine-res", item)] = data
        manifest_path = "/src/engine-res/1.0/manifest-engine-res.json"
        self.remote[manifest_path] = json_bytes(manifest)
        bundle = OfflineCache(self.sync())
        served = json.loads(bundle.render(manifest_path, "http://127.0.0.1:1"))
        paths = {f["fs_path"] for f in served["files"]}
        self.assertNotIn("Fonts/read_me.html", paths)
        self.assertIn("UI/page.html", paths)
        self.assertEqual(served["assets_pak"], 0)
        self.assertEqual(served["paks"], [])

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
        bundle = OfflineCache(self.sync())
        self.assertEqual(bundle.raw(path), data)
        (cache_path(self.cache, path)).write_bytes(b"x" * len(data))
        with self.assertRaisesRegex(WorkspaceError, "corrupt"):
            OfflineCache(self.cache)

    def test_missing_wasm_abi_descriptor_is_rejected(self):
        self.sync()
        p = self.cache / "cache.json"
        value = json.loads(p.read_bytes())
        del value["files"]["/src/engine-res/wasm-1.0.json"]
        p.write_bytes(json_bytes(value))
        with self.assertRaisesRegex(WorkspaceError, "not prepared"):
            OfflineCache(self.cache)

    def test_http_serves_local_player_manifests_and_fails_closed(self):
        bundle = OfflineCache(self.sync())
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
                    self.assertEqual(json.load(r)["assets_pak"], 0)
                with patch.object(
                    bundle, "render", side_effect=AssertionError("binary must stream")
                ):
                    with urlopen(base + "/UrhoXRuntime.wasm") as r:
                        self.assertEqual("application/wasm", r.headers["Content-Type"])
                        self.assertEqual(
                            r.read(),
                            bundle.raw(bundle.runtime_paths["UrhoXRuntime.wasm"]),
                        )
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + PREFIX + "/src/official-res/assets/not-cached")
                self.assertEqual(error.exception.code, 503)
                self.assertIn(b"not prepared", error.exception.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class DownloadTest(unittest.TestCase):
    def test_gzip_is_decoded_before_size_and_checksum_validation(self):
        response = io.BytesIO(gzip.compress(b"asset content"))
        response.url = ORIGIN + "/src/engine/assets/example"
        response.headers = {"Content-Encoding": "gzip"}
        with patch("tapmaker_local_web.offline.build_opener") as factory:
            factory.return_value.open.return_value = response
            self.assertEqual(
                _get("/src/engine/assets/example", limit=13), b"asset content"
            )

    def test_shared_download_streams_gzip_checks_reuse_and_cleans_failed_temp(self):
        content = b"asset content" * 100000
        expected = {"size": len(content), "hash": f"{zlib.crc32(content):08x}"}

        def response(data):
            result = io.BytesIO(gzip.compress(data))
            result.headers = {"Content-Encoding": "gzip"}
            return result

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "asset"
            with patch(
                "pathlib.Path.read_bytes", side_effect=AssertionError("whole-file read")
            ):
                download(
                    "https://example.test/asset",
                    target,
                    expected,
                    opener=lambda *a, **k: response(content),
                )
                download(
                    "https://example.test/asset",
                    target,
                    expected,
                    opener=lambda *a, **k: self.fail("verified file should be reused"),
                )
            target.write_bytes(b"x" * len(content))
            with self.assertRaisesRegex(WorkspaceError, "exceeds"):
                download(
                    "https://example.test/asset",
                    target,
                    expected,
                    opener=lambda *a, **k: response(content + b"extra"),
                )
            self.assertFalse(target.with_name("asset.part").exists())
            download(
                "https://example.test/asset",
                target,
                expected,
                opener=lambda *a, **k: response(content),
            )
            self.assertEqual(target.read_bytes(), content)

    def test_redirect_does_not_downgrade_tls_or_contact_another_origin(self):
        handler = _OfficialRedirects()
        for url in (
            "http://tapcode-sce.spark.xd.com/src/a",
            "https://other.example/src/a",
        ):
            with self.assertRaisesRegex(WorkspaceError, "outside"):
                handler.redirect_request(None, None, 302, "", {}, url)


if __name__ == "__main__":
    unittest.main()
