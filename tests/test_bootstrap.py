import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile


PROJECT = Path(__file__).resolve().parents[1]
LINUX_DIGEST = "abd329f7b29c7c62b780942cec1b8e79a49e371f848098bdd4ab796391d2a851"
WINDOWS_DIGEST = "575162a0ecccb7c8a419d758174733c31a0f5ac0ae534e17c1e31e1fe0567733"


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Linux shell acceptance tests")
class LinuxBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "portable app ! $ café"
        self.root.mkdir()
        self.script = self.root / "setup_linux.sh"
        shutil.copy2(PROJECT / "setup_linux.sh", self.script)
        (self.root / "tools").mkdir()
        (self.root / "tools" / "setup_portable.py").write_text("# Test setup stage\n", encoding="utf-8")
        self.arguments = self.root / "captured-arguments"
        self.commands = self.root / "test-commands"
        self.commands.mkdir()
        self.environment = dict(os.environ, BOOTSTRAP_TEST_ARGS=str(self.arguments),
                                PATH=str(self.commands) + os.pathsep + os.environ["PATH"])
        self.runtime = self.root / "int" / "linux" / "MsPy-3_11_14"
        self.python = self.runtime / "bin" / "python3.11"
        self.python_source = '''#!/bin/bash
if [[ -n "${PYTHONHOME:-}${PYTHONPATH:-}${VIRTUAL_ENV:-}" ]]; then exit 9; fi
if [[ "${1:-}" == "-c" ]]; then exit 0; fi
printf '%s\\0' "$@" > "$BOOTSTRAP_TEST_ARGS"
exit "${BOOTSTRAP_TEST_EXIT:-0}"
'''
        self.command("uname", "#!/bin/bash\nprintf 'x86_64\\n'\n")

    def command(self, name, source):
        path = self.commands / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)
        return path

    def existing_runtime(self):
        self.python.parent.mkdir(parents=True)
        self.python.write_text(self.python_source, encoding="utf-8")
        self.python.chmod(0o755)

    def run_setup(self, *arguments):
        return subprocess.run(["bash", str(self.script), *arguments], env=self.environment,
                              capture_output=True, text=True, timeout=20)

    def fixture_archive(self):
        archive = self.root / "fixture.zip"
        with zipfile.ZipFile(archive, "w") as output:
            entry = zipfile.ZipInfo("MsPy-3_11_14/bin/python3.11")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o755) << 16
            output.writestr(entry, self.python_source)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.script.write_text(self.script.read_text(encoding="utf-8").replace(LINUX_DIGEST, digest), encoding="utf-8")
        self.environment["BOOTSTRAP_TEST_ARCHIVE"] = str(archive)
        self.command("curl", '''#!/bin/bash
while (( $# )); do
    if [[ "$1" == "--output" ]]; then output="$2"; shift 2; else shift; fi
done
cp -- "$BOOTSTRAP_TEST_ARCHIVE" "$output"
''')
        return archive

    def test_existing_runtime_preserves_special_path_and_argument_boundaries(self):
        self.existing_runtime()
        self.command("curl", "#!/bin/bash\nexit 99\n")
        result = self.run_setup("--no-launch", "--hidden")
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = self.arguments.read_bytes().split(b"\0")[:-1]
        self.assertEqual(captured, [b"-u", str(self.root / "tools" / "setup_portable.py").encode(), b"--no-launch", b"--hidden"])
        self.assertTrue((self.root / "bin" / "linux" / "logs" / "setup.log").exists())
        self.assertFalse(any((self.root / "int" / "linux").glob(".mspy-bootstrap.*")))


    def test_existing_interpreter_isolated_from_inherited_python_environment(self):
        self.existing_runtime()
        self.environment.update(PYTHONHOME="/wrong/python", PYTHONPATH="/other/application", VIRTUAL_ENV="/old/environment")
        result = self.run_setup("--no-launch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.arguments.exists())

    def test_setup_stage_failure_propagates(self):
        self.existing_runtime()
        self.environment["BOOTSTRAP_TEST_EXIT"] = "7"
        result = self.run_setup("--no-launch")
        self.assertEqual(result.returncode, 7)

    def test_download_failure_cleans_staging_and_never_launches(self):
        self.command("curl", "#!/bin/bash\nprintf 'download failed\\n' >&2\nexit 22\n")
        result = self.run_setup("--no-launch")
        self.assertEqual(result.returncode, 22)
        self.assertFalse(self.arguments.exists())
        self.assertFalse(self.runtime.exists())
        self.assertFalse(any((self.root / "int" / "linux").glob(".mspy-bootstrap.*")))

    def test_wrong_digest_cleans_staging_without_replacing_runtime(self):
        self.command("curl", '''#!/bin/bash
while (( $# )); do
    if [[ "$1" == "--output" ]]; then output="$2"; shift 2; else shift; fi
done
printf 'not the published archive' > "$output"
''')
        result = self.run_setup("--no-launch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("SHA-256", result.stderr)
        self.assertFalse(self.runtime.exists())
        self.assertFalse(self.arguments.exists())
        self.assertFalse(any((self.root / "int" / "linux").glob(".mspy-bootstrap.*")))

    def test_verified_archive_replaces_only_owned_incomplete_runtime(self):
        if not shutil.which("unzip"):
            self.skipTest("unzip is required for archive extraction acceptance")
        self.python.parent.mkdir(parents=True)
        (self.runtime / "incomplete-marker").write_text("incomplete", encoding="utf-8")
        settings = self.root / "user-settings.json"
        settings.write_text('{"keep":true}', encoding="utf-8")
        self.fixture_archive()
        result = self.run_setup("--no-launch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.python.is_file())
        self.assertFalse((self.runtime / "incomplete-marker").exists())
        self.assertEqual(settings.read_text(encoding="utf-8"), '{"keep":true}')
        self.assertTrue(self.arguments.exists())
        self.assertFalse(any((self.root / "int" / "linux").glob(".mspy-bootstrap.*")))

    def test_unsupported_architecture_and_options_do_not_launch(self):
        self.existing_runtime()
        self.command("uname", "#!/bin/bash\nprintf 'aarch64\\n'\n")
        result = self.run_setup("--no-launch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("x86_64", result.stderr)
        self.assertFalse(self.arguments.exists())
        result = self.run_setup("--unsupported")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.arguments.exists())


class BootstrapSourceTest(unittest.TestCase):
    def test_windows_launcher_keeps_project_path_out_of_expanded_command(self):
        windows = (PROJECT / "setup_win.vbs").read_text(encoding="utf-8")
        command = next(line for line in windows.splitlines() if line.startswith("command = "))
        self.assertNotRegex(command, r"root|ScriptFullName|BuildPath|%")
        self.assertIn('"bin\\win\\install.ps1"', command)
        self.assertIn("shell.CurrentDirectory = root", windows)
        self.assertLess(windows.index("shell.CurrentDirectory = root"), windows.index("shell.Run(command"))
        self.assertIn("shell.Run(command, windowStyle, True)", windows)
        self.assertIn("WScript.Quit result", windows)
        self.assertIn('"ADBeam me up setup"', windows)

    def test_assets_are_the_exact_requested_release_with_published_digests(self):
        linux = (PROJECT / "setup_linux.sh").read_text(encoding="utf-8")
        windows = (PROJECT / "bin" / "win" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("releases/download/3.11.14/MsPy-3_11_14-linux.zip", linux)
        self.assertIn(LINUX_DIGEST, linux)
        self.assertIn("releases/download/3.11.14/MsPy-3_11_14-win.zip", windows)
        self.assertIn(WINDOWS_DIGEST, windows)

    def test_linux_shell_parses(self):
        if not shutil.which("bash"):
            self.skipTest("bash unavailable")
        result = subprocess.run(["bash", "-n", str(PROJECT / "setup_linux.sh")], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
