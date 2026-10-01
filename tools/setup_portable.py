import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vivo_power.branding import APP_NAME


def install_portable(root, launch=True, hidden=False):
    root = Path(root).resolve()
    platform = "win" if os.name == "nt" else "linux"
    platform_dir = root / "int" / platform
    runtime = platform_dir / "MsPy-3_11_14"
    interpreter = runtime / ("python.exe" if os.name == "nt" else "bin/python3.11")
    environment = platform_dir / "venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    configuration = environment / "pyvenv.cfg"
    previous_home = None
    if configuration.is_file():
        for line in configuration.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "home":
                previous_home = Path(value.strip()).resolve()
                break
    rebuilt = not python.is_file() or previous_home != interpreter.parent.resolve()
    if rebuilt:
        print("Creating the local Python environment…", flush=True)
        if environment.exists():
            shutil.rmtree(environment)
        subprocess.run([str(interpreter), "-m", "venv", str(environment)], check=True)
    dependency_hash = hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest()
    marker = environment / ".requirements.sha256"
    if rebuilt or not marker.is_file() or marker.read_text(encoding="utf-8").strip() != dependency_hash:
        print("Installing the application and its Qt dependencies…", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--cache-dir", str(platform_dir / "pip-cache"), "-e", str(root)], cwd=root, check=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=environment, delete=False) as output:
            output.write(dependency_hash + "\n")
            temporary = Path(output.name)
        os.replace(temporary, marker)
    else:
        print("The local Python environment is up to date.", flush=True)
    preferences = root / "install.local.json"
    choices = json.loads(preferences.read_text(encoding="utf-8")) if preferences.is_file() else {}
    print("Updating launch shortcuts…", flush=True)
    request = {"root": str(root), "desktop": choices.get("desktop", True), "app_menu": choices.get("app_menu", True), "directory": choices.get("directory")}
    code = "import json, pathlib, sys; from vivo_power.shortcuts import ensure_shortcuts; options=json.loads(sys.stdin.read()); options['root']=pathlib.Path(options['root']); options['directory']=pathlib.Path(options['directory']) if options['directory'] else None; print('\\n'.join(ensure_shortcuts(**options)))"
    subprocess.run([str(python), "-c", code], input=json.dumps(request), text=True, cwd=root, check=True)
    if launch:
        gui_python = environment / "Scripts/pythonw.exe" if os.name == "nt" else python
        command = [str(gui_python), "-m", "vivo_power"]
        logs = root / "bin" / platform / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / "application.log"
        print(f"Starting {APP_NAME}…", flush=True)
        with log.open("a", encoding="utf-8") as output:
            process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=os.name != "nt")
        try:
            result = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if not hidden:
                print("The application is running. You can close this setup window.", flush=True)
        else:
            if result:
                raise OSError(f"The GUI exited during startup (code {result}). Details: {log}")
    return python


def main():
    parser = argparse.ArgumentParser(description=f"Prepare {APP_NAME}'s portable Python environment and launch shortcuts")
    parser.add_argument("--hidden", action="store_true", help="Use the background launcher without a setup terminal")
    parser.add_argument("--no-launch", action="store_true", help="Install and repair shortcuts without opening the GUI")
    args = parser.parse_args()
    try:
        install_portable(Path(__file__).resolve().parents[1], launch=not args.no_launch, hidden=args.hidden)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Setup failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
