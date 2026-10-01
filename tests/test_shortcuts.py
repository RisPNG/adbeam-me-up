import base64
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QStandardPaths

from vivo_power.shortcuts import WINDOWS_SHORTCUTS, ensure_shortcuts


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.root = self.base / 'Vivo app $HOME `whoami` "quoted" 50% \\ folder'
        self.root.mkdir()
        (self.root / "setup_linux.sh").write_text("#!/bin/bash\nprintf '%s\\n' \"$PWD\" \"$@\" > launch-result.txt\n")
        (self.root / "setup_win.vbs").write_text("WScript.Quit\n")
        self.desktop = self.base / "Localized Desktop"
        self.menu = self.base / "applications"
        self.paths = patch("vivo_power.shortcuts.QStandardPaths.writableLocation", side_effect=lambda kind:
            str(self.desktop if kind == QStandardPaths.StandardLocation.DesktopLocation else self.menu))
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.directory.cleanup()

    def test_linux_entries_target_bootstrap_and_do_not_create_autostart(self):
        files = ensure_shortcuts(self.root)
        self.assertEqual(set(files), {str(self.desktop / "adbeam-me-up.desktop"), str(self.menu / "adbeam-me-up.desktop")})
        entry = Path(files[0]).read_text()
        self.assertIn("Name=ADBeam me up\n", entry)
        self.assertIn("Exec=bash ", entry)
        self.assertIn(" --hidden\n", entry)
        self.assertIn("Terminal=false", entry)
        self.assertIn("%%", entry)
        self.assertIn("\\\\$HOME", entry)
        self.assertIn("\\\\`whoami\\\\`", entry)
        self.assertIn('\\\\"quoted\\\\"', entry)
        self.assertIn("\\\\\\\\", entry)
        self.assertEqual(Path(files[0]).stat().st_mode & 0o777, 0o755)
        self.assertFalse((self.base / "autostart").exists())

    def test_repair_is_idempotent_and_updates_relocated_folder(self):
        files = ensure_shortcuts(self.root)
        before = {file: Path(file).stat().st_mtime_ns for file in files}
        ensure_shortcuts(self.root)
        self.assertEqual(before, {file: Path(file).stat().st_mtime_ns for file in files})
        relocated = self.base / "Moved app"
        self.root.rename(relocated)
        ensure_shortcuts(relocated)
        content = Path(files[0]).read_text()
        self.assertIn(str(relocated / "setup_linux.sh"), content)
        self.assertNotIn("50%%", content)

    def test_preferences_remove_disabled_entries_and_repair_custom_folder(self):
        ensure_shortcuts(self.root)
        custom = self.base / "Chosen folder"
        files = ensure_shortcuts(self.root, app_menu=False, directory=custom)
        self.assertEqual(files, [str(custom / "adbeam-me-up.desktop")])
        self.assertFalse((self.desktop / "adbeam-me-up.desktop").exists())
        self.assertFalse((self.menu / "adbeam-me-up.desktop").exists())
        (self.root / "install.local.json").write_text(json.dumps({"desktop": True, "app_menu": False, "directory": str(custom)}))
        files = ensure_shortcuts(self.root, desktop=False, app_menu=False)
        self.assertEqual(files, [])
        self.assertFalse((custom / "adbeam-me-up.desktop").exists())

    def test_rename_replaces_owned_legacy_shortcuts_in_all_saved_locations(self):
        files = ensure_shortcuts(self.root)
        old_content = Path(files[0]).read_text().replace("Name=ADBeam me up", "Name=Vivo Background Power")
        previous = self.base / "Previously chosen shortcuts"
        selected = self.base / "New shortcuts"
        directories = [self.desktop, self.menu, self.root, previous, selected]
        for folder in directories:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "vivo-background-power.desktop").write_text(old_content)
        (self.root / "install.local.json").write_text(json.dumps({"desktop": True, "app_menu": True, "directory": str(previous)}))
        files = ensure_shortcuts(self.root, directory=selected)
        self.assertEqual(set(files), {str(selected / "adbeam-me-up.desktop"), str(self.menu / "adbeam-me-up.desktop")})
        for folder in directories:
            self.assertFalse((folder / "vivo-background-power.desktop").exists())

    def test_rename_preserves_legacy_shortcuts_for_another_installation(self):
        self.desktop.mkdir()
        old = self.desktop / "vivo-background-power.desktop"
        content = '[Desktop Entry]\nName=Vivo Background Power\nExec=bash "/another/installation/setup_linux.sh" --hidden\n'
        old.write_text(content)
        ensure_shortcuts(self.root)
        self.assertEqual(old.read_text(), content)
        self.assertTrue((self.desktop / "adbeam-me-up.desktop").exists())

    def test_incomplete_or_wheel_tree_does_not_create_shortcuts(self):
        (self.root / "setup_win.vbs").unlink()
        with self.assertRaisesRegex(ValueError, "portable application folder"):
            ensure_shortcuts(self.root)
        self.assertFalse(self.desktop.exists())
        self.assertFalse(self.menu.exists())

    @unittest.skipUnless(shutil.which("gio"), "GIO desktop entry launcher is unavailable")
    def test_native_gio_parser_launches_special_character_path_exactly(self):
        files = ensure_shortcuts(self.root)
        subprocess.run([shutil.which("gio"), "launch", files[0]], check=True, capture_output=True, text=True, timeout=5)
        result = self.root / "launch-result.txt"
        for _ in range(100):
            if result.exists():
                break
            time.sleep(0.01)
        self.assertEqual(result.read_text().splitlines(), [str(self.root), "--hidden"])

    def test_windows_uses_native_com_and_structured_stdin_without_path_code(self):
        expected = [str(self.root / "ADBeam me up.lnk")]
        native = subprocess.CompletedProcess([], 0, json.dumps(expected), "")
        with patch("vivo_power.shortcuts.sys.platform", "win32"), patch("vivo_power.shortcuts.subprocess.run", return_value=native) as execute:
            result = ensure_shortcuts(self.root, desktop=False, app_menu=False)
        self.assertEqual(result, expected)
        command = execute.call_args.args[0]
        script = base64.b64decode(command[-1]).decode("utf-16-le")
        self.assertEqual(script, WINDOWS_SHORTCUTS)
        self.assertTrue(script.startswith("$ErrorActionPreference"))
        self.assertIn("WScript.Shell", script)
        self.assertIn("SpecialFolders.Item('Programs')", script)
        self.assertIn("setup_win.vbs", script)
        self.assertIn("$name = $options.app_name + '.lnk'", script)
        self.assertIn("$legacy.TargetPath -eq $target -and $legacy.Arguments -eq $arguments", script)
        self.assertNotIn(str(self.root), script)
        options = json.loads(execute.call_args.kwargs["input"])
        self.assertEqual(options["root"], str(self.root))
        self.assertEqual(options["app_name"], "ADBeam me up")
        self.assertFalse(options["desktop"])
        self.assertFalse(options["app_menu"])
        self.assertEqual(options["directory"], None)

    def test_windows_native_failure_reports_error(self):
        native = subprocess.CompletedProcess([], 1, "", "Permission denied")
        with patch("vivo_power.shortcuts.sys.platform", "win32"), patch("vivo_power.shortcuts.subprocess.run", return_value=native):
            with self.assertRaisesRegex(OSError, "Permission denied"):
                ensure_shortcuts(self.root)


if __name__ == "__main__":
    unittest.main()
