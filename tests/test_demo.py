from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = REPOSITORY_ROOT / "examples/demo-project"


class DemoContractTest(unittest.TestCase):
    @contextmanager
    def running_demo(self, code_directories: list[Path]):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        command = [
            sys.executable,
            "-m",
            "tapmaker_local_web",
            "web",
        ]
        for directory in code_directories:
            command.extend(("--code", str(directory)))
        command.extend(
            (
                "--entry",
                "scripts/main.lua",
                "--runtime",
                "remote",
                "--no-open",
                "--port",
                str(port),
            )
        )
        process = subprocess.Popen(
            command,
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 5
        try:
            while True:
                if process.poll() is not None:
                    output = process.stdout.read() if process.stdout is not None else ""
                    self.fail(f"demo server exited early:\n{output}")
                try:
                    with urlopen(f"{base}/latest.json", timeout=0.2):
                        break
                except URLError:
                    if time.monotonic() >= deadline:
                        self.fail("demo server did not become ready")
                    time.sleep(0.05)
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()

    def manifest(self, base: str) -> dict[str, object]:
        with urlopen(f"{base}/latest.json") as response:
            latest = json.load(response)
        with urlopen(f"{base}/local/manifest-{latest['client']}.json") as response:
            return json.load(response)

    def asset_json(self, base: str, item: dict[str, object]) -> object:
        asset_name = f"{item['uuid']}-{item['hash']}{item['ext']}"
        with urlopen(f"{base}/assets/{asset_name}") as response:
            return json.load(response)

    def test_demo_simulates_standard_maker_assets_and_scripts_roots(self) -> None:
        with self.running_demo([DEMO_ROOT]) as base:
            manifest = self.manifest(base)
            files = {item["fs_path"]: item for item in manifest["files"]}

            self.assertEqual(manifest["entry"], "main.lua")
            self.assertIn("demo.json", files)
            self.assertIn("main.lua", files)
            self.assertNotIn("assets/demo.json", files)
            self.assertNotIn("scripts/main.lua", files)
            self.assertNotIn("local-preview.json", files)
            self.assertEqual(
                self.asset_json(base, files["demo.json"]),
                {"name": "tapmaker-local-web-demo", "public_sample": True},
            )
            settings = self.asset_json(base, files["settings.json"])
            self.assertEqual(
                settings["build"]["asset_dirs"],
                ["../assets", "../scripts"],
            )

    def test_demo_can_simulate_a_third_local_resource_root(self) -> None:
        with self.running_demo(
            [DEMO_ROOT / "assets", DEMO_ROOT / "scripts", DEMO_ROOT / "config"]
        ) as base:
            manifest = self.manifest(base)
            files = {item["fs_path"]: item for item in manifest["files"]}

            self.assertEqual(manifest["entry"], "main.lua")
            self.assertIn("demo.json", files)
            self.assertIn("main.lua", files)
            self.assertIn("local-preview.json", files)
            self.assertNotIn("assets/demo.json", files)
            config = files["local-preview.json"]
            self.assertEqual(
                self.asset_json(base, config),
                {"mode": "three-resource-roots", "local_preview_only": True},
            )


if __name__ == "__main__":
    unittest.main()
