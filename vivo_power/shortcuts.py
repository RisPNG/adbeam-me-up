import base64
import json
import os
from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import QIODevice, QSaveFile, QStandardPaths

from .branding import APP_ID, APP_NAME


WINDOWS_SHORTCUTS = r"""$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$options = [Console]::In.ReadToEnd() | ConvertFrom-Json
$shell = New-Object -ComObject WScript.Shell
$root = [System.IO.Path]::GetFullPath($options.root)
$name = $options.app_name + '.lnk'
$desktop = $shell.SpecialFolders.Item('Desktop')
$programs = $shell.SpecialFolders.Item('Programs')
$locations = [ordered]@{}
if ($options.previous_directory -and $options.previous_directory -ne $options.directory) {
    $locations[(Join-Path $options.previous_directory $name)] = $false
}
if ($options.directory) {
    $locations[(Join-Path $desktop $name)] = $false
    $desktop = $options.directory
}
$locations[(Join-Path $desktop $name)] = [bool]$options.desktop
$locations[(Join-Path $programs $name)] = [bool]$options.app_menu
$locations[(Join-Path $root $name)] = $true
$target = Join-Path $env:SystemRoot 'System32\wscript.exe'
$arguments = '"' + (Join-Path $root 'setup_win.vbs') + '" --hidden'
$legacyDirectories = @($shell.SpecialFolders.Item('Desktop'), $programs, $root, $options.directory, $options.previous_directory)
foreach ($folder in $legacyDirectories | Select-Object -Unique) {
    if (-not $folder) { continue }
    $legacyPath = Join-Path $folder 'Vivo Background Power.lnk'
    if (Test-Path -LiteralPath $legacyPath) {
        $legacy = $shell.CreateShortcut($legacyPath)
        if ($legacy.TargetPath -eq $target -and $legacy.Arguments -eq $arguments) {
            Remove-Item -LiteralPath $legacyPath
        }
    }
}
$created = @()
foreach ($path in $locations.Keys) {
    if (-not $locations[$path]) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
        continue
    }
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($path)) | Out-Null
    $link = $shell.CreateShortcut($path)
    if ($link.TargetPath -ne $target -or $link.Arguments -ne $arguments -or $link.WorkingDirectory -ne $root) {
        $link.TargetPath = $target
        $link.Arguments = $arguments
        $link.WorkingDirectory = $root
        $link.Description = $options.app_name
        $link.IconLocation = (Join-Path $env:SystemRoot 'System32\shell32.dll') + ',167'
        $link.WindowStyle = 1
        $link.Save()
    }
    $created += $path
}
ConvertTo-Json -InputObject @($created) -Compress
"""


def ensure_shortcuts(root: Path, desktop=True, app_menu=True, directory: Path | None = None) -> list[str]:
    root = Path(root).resolve()
    if not (root / "setup_linux.sh").is_file() or not (root / "setup_win.vbs").is_file():
        raise ValueError("Shortcut repair needs the portable application folder with both setup launchers.")
    directory = Path(directory).expanduser().resolve() if directory else None
    preferences_file = root / "install.local.json"
    preferences = json.loads(preferences_file.read_text(encoding="utf-8")) if preferences_file.is_file() else {}
    previous_directory = preferences.get("directory")
    if sys.platform == "win32":
        executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        encoded = base64.b64encode(WINDOWS_SHORTCUTS.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            [str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            input=json.dumps({"root": str(root), "app_name": APP_NAME, "desktop": desktop, "app_menu": app_menu,
                              "directory": str(directory) if directory else None,
                              "previous_directory": previous_directory}, ensure_ascii=False),
            capture_output=True, encoding="utf-8", timeout=30,
        )
        if result.returncode:
            raise OSError(result.stderr.strip() or "Windows could not repair the application shortcuts.")
        return json.loads(result.stdout.lstrip("\ufeff"))

    desktop_directory = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation))
    menu_directory = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.ApplicationsLocation))
    name = f"{APP_ID}.desktop"
    targets = {}
    if previous_directory and Path(previous_directory) != directory:
        targets[Path(previous_directory) / name] = False
    if directory:
        targets[desktop_directory / name] = False
    targets[(directory or desktop_directory) / name] = desktop
    targets[menu_directory / name] = app_menu
    argument = str(root / "setup_linux.sh")
    argument = argument.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    argument = argument.replace("\\", "\\\\").replace("%", "%%").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    working_directory = str(root).replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    content = (f"[Desktop Entry]\nType=Application\nName={APP_NAME}\n"
               "Comment=Apply and verify Android device presets\n"
               f'Exec=bash "{argument}" --hidden\nPath={working_directory}\n'
               "Terminal=false\nIcon=preferences-system\nCategories=Utility;\n")
    legacy_directories = {desktop_directory, menu_directory, root}
    if directory:
        legacy_directories.add(directory)
    if previous_directory:
        legacy_directories.add(Path(previous_directory))
    for folder in legacy_directories:
        legacy = folder / "vivo-background-power.desktop"
        if legacy.is_file() and f'Exec=bash "{argument}" --hidden' in legacy.read_text(encoding="utf-8").splitlines():
            legacy.unlink()
    created = []
    for path, enabled in targets.items():
        if not enabled:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            output = QSaveFile(str(path))
            if not output.open(QIODevice.OpenModeFlag.WriteOnly):
                raise OSError(output.errorString())
            payload = content.encode("utf-8")
            if output.write(payload) != len(payload) or not output.commit():
                raise OSError(output.errorString())
        path.chmod(0o755 if path.parent == (directory or desktop_directory) else 0o644)
        created.append(str(path))
    return created
