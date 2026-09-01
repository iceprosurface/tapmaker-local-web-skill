from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tapmaker_local_web import LocalWebProject, LocalWebServer, Workspace, WorkspaceError, direct_project
from tapmaker_local_web.cli import parser


class WorkspaceConfigContractTest(unittest.TestCase):
    def test_direct_project_uses_code_root_and_entry_with_internal_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts/main.lua").write_text("return true\n", encoding="utf-8")
            (root / ".env").write_text("PRIVATE_VALUE=not-served\n", encoding="utf-8")

            project = direct_project(root, "scripts/main.lua")
            same_project = direct_project(root, "scripts/main.lua")
            state = LocalWebProject(project)
            server = LocalWebServer(("127.0.0.1", 0), state)
            try:
                paths = {item["fs_path"] for item in state.manifest()["files"]}
                self.assertRegex(state.project_id, r"^local-[0-9a-f]{16}$")
                self.assertEqual(state.project_id, same_project.deployment().maker_project_id)
                self.assertEqual(state.deployment.entry, "main.lua")
                self.assertIn("main.lua", paths)
                self.assertNotIn("scripts/main.lua", paths)
                self.assertNotIn(".env", paths)
                self.assertIn("entry=main.lua", server.url)
                self.assertNotIn(str(root), server.url)
            finally:
                server.server_close()

    def test_direct_project_rejects_entry_outside_code_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(WorkspaceError):
                direct_project(root, "../main.lua")

    def test_direct_project_combines_multiple_maker_resource_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets/images").mkdir(parents=True)
            (root / "scripts").mkdir()
            (root / "config").mkdir()
            (root / "assets/images/hero.png").write_bytes(b"hero")
            (root / "scripts/main.lua").write_text("return true\n", encoding="utf-8")
            (root / "config/game.json").write_text("{}\n", encoding="utf-8")

            project = direct_project(
                [root / "assets", root / "scripts", root / "config"],
                "scripts/main.lua",
            )
            state = LocalWebProject(project)
            paths = {item["fs_path"] for item in state.manifest()["files"]}
            reordered = direct_project(
                [root / "config", root / "scripts", root / "assets"],
                "main.lua",
            )

            self.assertIn("images/hero.png", paths)
            self.assertIn("main.lua", paths)
            self.assertIn("game.json", paths)
            self.assertNotIn("assets/images/hero.png", paths)
            self.assertEqual(project.deployment().entry, "main.lua")
            self.assertEqual(project.name, reordered.name)

    def test_direct_project_rejects_collisions_between_maker_resource_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            (root / "scripts").mkdir()
            (root / "scripts/main.lua").write_text("return true\n", encoding="utf-8")
            (root / "assets/shared.json").write_text("{}\n", encoding="utf-8")
            (root / "scripts/shared.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(WorkspaceError, "资源路径冲突"):
                LocalWebProject(direct_project(root, "scripts/main.lua"))

    def test_direct_project_normalizes_a_custom_scripts_root_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project").mkdir()
            (root / "assets").mkdir()
            (root / "custom").mkdir()
            (root / "custom/boot.lua").write_text("return true\n", encoding="utf-8")
            (root / ".project/settings.json").write_text(
                json.dumps({"build": {"asset_dirs": ["../assets", "../custom"]}}),
                encoding="utf-8",
            )

            project = direct_project(root, "custom/boot.lua")
            root_relative_project = direct_project(root, "boot.lua")

            self.assertEqual(project.deployment().entry, "boot.lua")
            self.assertEqual(project.name, root_relative_project.name)

    def test_direct_project_supports_a_third_local_resource_root_from_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project").mkdir()
            (root / "assets").mkdir()
            (root / "scripts").mkdir()
            (root / "data").mkdir()
            (root / "scripts/main.lua").write_text("return true\n", encoding="utf-8")
            (root / "assets/hero.png").write_bytes(b"hero")
            (root / "data/levels.json").write_text("{}\n", encoding="utf-8")
            (root / ".project/settings.json").write_text(
                json.dumps(
                    {
                        "build": {
                            "asset_dirs": ["../assets", "../scripts", "../data"],
                            "generate_fs_path": True,
                        }
                    }
                ),
                encoding="utf-8",
            )

            state = LocalWebProject(direct_project(root, "scripts/main.lua"))
            files = {item["fs_path"]: item for item in state.manifest()["files"]}
            paths = set(files)
            settings_item = files["settings.json"]
            settings_asset = state.asset(
                f"{settings_item['uuid']}-{settings_item['hash']}{settings_item['ext']}"
            )
            self.assertIsNotNone(settings_asset)
            generated_settings = json.loads(settings_asset.read())

            self.assertIn("hero.png", paths)
            self.assertIn("main.lua", paths)
            self.assertIn("levels.json", paths)
            self.assertEqual(
                generated_settings["build"]["asset_dirs"],
                ["../assets", "../data", "../scripts"],
            )

    def test_direct_project_rejects_entry_outside_selected_resource_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            (root / "scripts").mkdir()
            (root / "private").mkdir()
            (root / "private/main.lua").write_text("return true\n", encoding="utf-8")

            with self.assertRaises(WorkspaceError):
                direct_project([root / "assets", root / "scripts"], "private/main.lua")

    def test_cli_accepts_repeated_code_directories(self) -> None:
        args = parser().parse_args(
            [
                "web",
                "--code",
                "/project/assets",
                "--code",
                "/project/scripts",
                "--entry",
                "scripts/main.lua",
            ]
        )

        self.assertEqual(args.code, [Path("/project/assets"), Path("/project/scripts")])

    def test_rejects_project_paths_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tapmaker.workspace.toml").write_text(
                'schema = 1\n[projects.demo]\npath = "../outside"\n',
                encoding="utf-8",
            )

            with self.assertRaises(WorkspaceError):
                Workspace(root).project("demo")

    def test_reads_generic_multi_target_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "apps/demo"
            (project / "scripts").mkdir(parents=True)
            (project / "scripts/main.lua").write_text("return true\n", encoding="utf-8")
            (project / "version.toml").write_text("schema = 1\nversion = 7\n", encoding="utf-8")
            (root / "tapmaker.workspace.toml").write_text(
                'schema = 1\n[projects.demo]\npath = "apps/demo"\n', encoding="utf-8"
            )
            (project / "tapmaker.toml").write_text(
                """schema = 1
name = "demo"
entry = "main.lua"

[[mounts]]
source = "scripts"
target = "scripts"

[deploy]
default = "test"
version_file = "version.toml"
build_info = "scripts/runtime/GeneratedBuildInfo.lua"

[deploy.targets.test]
entry = "main.lua"
channel = "test"
""",
                encoding="utf-8",
            )

            loaded = Workspace(root).project("demo")

            self.assertEqual(loaded.current_version(), 7)
            self.assertEqual(loaded.deployment().channel, "test")
            self.assertEqual(loaded.build_info_target, Path("scripts/runtime/GeneratedBuildInfo.lua"))


if __name__ == "__main__":
    unittest.main()
