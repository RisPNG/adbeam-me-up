import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.setup_portable import install_portable


class PortableSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vivo portable ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "app's new location ü $"
        self.root.mkdir()
        (self.root / "pyproject.toml").write_text('[project]\nname="example"\n', encoding="utf-8")
        self.platform = "win" if os.name == "nt" else "linux"
        self.runtime = self.root / "int" / self.platform / "MsPy-3_11_14"
        self.runtime_python = self.runtime / ("python.exe" if os.name == "nt" else "bin/python3.11")
        self.runtime_python.parent.mkdir(parents=True)
        self.runtime_python.touch()
        self.environment = self.runtime.parent / "venv"
        self.python = self.environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.calls = []

    def subprocess(self, command, **options):
        self.calls.append((command, options))
        if "venv" in command:
            self.python.parent.mkdir(parents=True)
            self.python.touch()
            (self.environment / "pyvenv.cfg").write_text(f"home = {self.runtime_python.parent}\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    def test_first_install_creates_environment_dependencies_and_shortcuts(self):
        with patch("tools.setup_portable.subprocess.run", self.subprocess):
            python = install_portable(self.root, launch=False)
        self.assertEqual(python, self.python)
        self.assertEqual(self.calls[0][0], [str(self.runtime_python), "-m", "venv", str(self.environment)])
        self.assertEqual(self.calls[1][0][-1], str(self.root))
        request = json.loads(self.calls[2][1]["input"])
        self.assertEqual(request["root"], str(self.root))
        self.assertTrue(request["desktop"])
        self.assertTrue(request["app_menu"])
        self.assertEqual((self.environment / ".requirements.sha256").read_text().strip(), hashlib.sha256((self.root / "pyproject.toml").read_bytes()).hexdigest())

    def test_repeated_launch_skips_dependency_install_but_repairs_shortcuts(self):
        with patch("tools.setup_portable.subprocess.run", self.subprocess):
            install_portable(self.root, launch=False)
            self.calls.clear()
            install_portable(self.root, launch=False)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("vivo_power.shortcuts", self.calls[0][0][2])

    def test_moved_folder_rebuilds_environment_before_launch(self):
        with patch("tools.setup_portable.subprocess.run", self.subprocess):
            install_portable(self.root, launch=False)
            (self.environment / "pyvenv.cfg").write_text("home = /old/folder/int/linux/MsPy-3_11_14/bin\n", encoding="utf-8")
            (self.environment / "old-file").touch()
            self.calls.clear()
            install_portable(self.root, launch=False)
        self.assertIn("venv", self.calls[0][0])
        self.assertFalse((self.environment / "old-file").exists())
        self.assertIn("pip", self.calls[1][0])

    def test_dependency_change_reinstalls_without_rebuilding_python(self):
        with patch("tools.setup_portable.subprocess.run", self.subprocess):
            install_portable(self.root, launch=False)
            (self.root / "pyproject.toml").write_text('[project]\nname="changed"\n', encoding="utf-8")
            self.calls.clear()
            install_portable(self.root, launch=False)
        self.assertIn("pip", self.calls[0][0])
        self.assertEqual(len(self.calls), 2)

    def test_failed_dependencies_never_write_success_marker_or_launch(self):
        def failure(command, **options):
            if "pip" in command:
                raise subprocess.CalledProcessError(1, command)
            return self.subprocess(command, **options)
        with patch("tools.setup_portable.subprocess.run", failure), patch("tools.setup_portable.subprocess.Popen") as launch:
            with self.assertRaises(subprocess.CalledProcessError):
                install_portable(self.root)
        self.assertFalse((self.environment / ".requirements.sha256").exists())
        launch.assert_not_called()

    def test_preferences_survive_setup_and_paths_are_structured_arguments(self):
        (self.root / "install.local.json").write_text(json.dumps({"desktop": False, "app_menu": True, "directory": str(self.root / "Custom shortcuts")}), encoding="utf-8")
        with patch("tools.setup_portable.subprocess.run", self.subprocess):
            install_portable(self.root, launch=False)
        request = json.loads(self.calls[-1][1]["input"])
        self.assertFalse(request["desktop"])
        self.assertTrue(request["app_menu"])
        self.assertEqual(request["directory"], str(self.root / "Custom shortcuts"))

    def test_gui_launch_keeps_diagnostics_and_survives_setup_terminal_closing(self):
        for hidden in (False, True):
            with self.subTest(hidden=hidden), patch("tools.setup_portable.subprocess.run", self.subprocess), patch("tools.setup_portable.subprocess.Popen") as launch:
                launch.return_value.wait.side_effect = subprocess.TimeoutExpired("gui", 1)
                install_portable(self.root, hidden=hidden)
            command = launch.call_args.args[0]
            self.assertEqual(command[-2:], ["-m", "vivo_power"])
            self.assertTrue(Path(command[0]).is_relative_to(self.environment))
            options = launch.call_args.kwargs
            self.assertEqual(options["cwd"], self.root)
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertEqual(options["stderr"], subprocess.STDOUT)
            self.assertEqual(options["start_new_session"], os.name != "nt")
            self.assertEqual(Path(options["stdout"].name), self.root / "bin" / self.platform / "logs" / "application.log")
            launch.return_value.wait.assert_called_once_with(timeout=1)

    def test_early_gui_failure_reports_the_application_log(self):
        with patch("tools.setup_portable.subprocess.run", self.subprocess), patch("tools.setup_portable.subprocess.Popen") as launch:
            launch.return_value.wait.return_value = 1
            with self.assertRaises(OSError) as failure:
                install_portable(self.root)
        self.assertIn(str(self.root / "bin" / self.platform / "logs" / "application.log"), str(failure.exception))

    def test_gui_that_closes_normally_during_startup_is_successful(self):
        with patch("tools.setup_portable.subprocess.run", self.subprocess), patch("tools.setup_portable.subprocess.Popen") as launch:
            launch.return_value.wait.return_value = 0
            self.assertEqual(install_portable(self.root), self.python)

    @unittest.skipIf(os.name != "posix", "Closing a POSIX controlling terminal delivers SIGHUP")
    def test_real_gui_process_survives_controlling_terminal_close(self):
        import pty

        self.python.parent.mkdir(parents=True)
        self.python.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n", encoding="utf-8")
        self.python.chmod(0o755)
        (self.environment / "pyvenv.cfg").write_text(f"home = {self.runtime_python.parent}\n", encoding="utf-8")
        (self.environment / ".requirements.sha256").write_text(hashlib.sha256((self.root / "pyproject.toml").read_bytes()).hexdigest(), encoding="utf-8")
        package = self.root / "vivo_power"
        package.mkdir()
        (package / "__init__.py").touch()
        (package / "__main__.py").write_text("""import os
from pathlib import Path
import sys
import time
Path('gui.pid').write_text(str(os.getpid()))
print('GUI stdout retained', flush=True)
print('GUI stderr retained', file=sys.stderr, flush=True)
time.sleep(2)
Path('gui-survived').write_text('complete')
""", encoding="utf-8")
        caller = """import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import patch
sys.path.insert(0, sys.argv[2])
from tools.setup_portable import install_portable
children = []
actual_popen = subprocess.Popen
def launch(*args, **options):
    child = actual_popen(*args, **options)
    children.append(child)
    return child
terminal_closed = False
def terminal_close(signum, frame):
    global terminal_closed
    terminal_closed = True
signal.signal(signal.SIGHUP, terminal_close)
with patch('tools.setup_portable.subprocess.run', return_value=subprocess.CompletedProcess([], 0)), patch('tools.setup_portable.subprocess.Popen', launch):
    install_portable(sys.argv[1])
Path('setup-finished').touch()
deadline = time.monotonic() + 10
while not terminal_closed and time.monotonic() < deadline:
    time.sleep(0.01)
try:
    result = children[0].wait(timeout=5)
finally:
    if children[0].poll() is None:
        children[0].terminate()
        children[0].wait()
Path('outcome.json').write_text(json.dumps({'terminal_closed': terminal_closed, 'gui_exit': result}))
"""
        child, terminal = pty.fork()
        if child == 0:
            os.chdir(self.root)
            os.execv(sys.executable, [sys.executable, "-c", caller, str(self.root), str(Path(__file__).resolve().parents[1])])
        reaped = False
        try:
            deadline = time.monotonic() + 10
            while not (self.root / "setup-finished").is_file() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / "setup-finished").is_file(), "Setup did not launch the isolated fake GUI")
            os.close(terminal)
            terminal = None
            while time.monotonic() < deadline:
                waited, _ = os.waitpid(child, os.WNOHANG)
                if waited:
                    reaped = True
                    break
                time.sleep(0.01)
            self.assertTrue(reaped, "The setup process did not finish after its terminal closed")
            self.assertEqual(json.loads((self.root / "outcome.json").read_text()), {"terminal_closed": True, "gui_exit": 0})
            self.assertTrue((self.root / "gui-survived").is_file())
            log = (self.root / "bin" / self.platform / "logs" / "application.log").read_text()
            self.assertIn("GUI stdout retained", log)
            self.assertIn("GUI stderr retained", log)
        finally:
            if terminal is not None:
                os.close(terminal)
            if not reaped:
                pid_file = self.root / "gui.pid"
                if pid_file.is_file():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    os.kill(child, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                os.waitpid(child, 0)


if __name__ == "__main__":
    unittest.main()
