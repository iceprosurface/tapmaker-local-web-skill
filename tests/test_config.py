from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tapmaker_local_web import LocalWebProject, LocalWebServer, Workspace, WorkspaceError, direct_project


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
                self.assertEqual(state.deployment.entry, "scripts/main.lua")
                self.assertIn("scripts/main.lua", paths)
                self.assertNotIn(".env", paths)
                self.assertIn("entry=scripts/main.lua", server.url)
                self.assertNotIn(str(root), server.url)
            finally:
                server.server_close()

    def test_direct_project_rejects_entry_outside_code_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(WorkspaceError):
                direct_project(root, "../main.lua")

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
