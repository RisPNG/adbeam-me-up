#!/usr/bin/env python3

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build the Android inventory and native Settings helper")
    parser.add_argument("--sdk", type=Path, default=Path(os.environ.get("ANDROID_SDK_ROOT", os.environ.get("ANDROID_HOME", str(Path.home() / "Android" / "Sdk")))))
    parser.add_argument("--platform", default="android-37.0")
    parser.add_argument("--build-tools", default="36.0.0")
    parser.add_argument("--output", type=Path, default=root / "presets" / "vivo-background-power" / "vivo-background-power.jar")
    args = parser.parse_args()
    platform = args.sdk / "platforms" / args.platform
    libraries = [platform / "android.jar", platform / "uiautomator.jar", platform / "optional" / "android.test.base.jar"]
    d8 = args.sdk / "build-tools" / args.build_tools / "lib" / "d8.jar"
    for dependency in libraries + [d8]:
        if not dependency.is_file():
            parser.error(f"Missing Android SDK dependency: {dependency}")
    tools = {}
    for name in ("java", "javac"):
        tools[name] = shutil.which(name)
        if tools[name] is None:
            parser.error(f"A JDK with {name} on PATH is required")
    sources = [root / "presets" / "vivo-background-power" / name for name in ("VivoBackgroundPower.java", "PackageInventory.java")]
    with tempfile.TemporaryDirectory(prefix="vivo-native-build-") as temporary:
        build = Path(temporary)
        classes = build / "classes"
        classes.mkdir()
        subprocess.run([tools["javac"], "--release", "8", "-encoding", "UTF-8", "-g:none", "-classpath", os.pathsep.join(map(str, libraries)), "-d", str(classes), *map(str, sources)], check=True)
        subprocess.run([tools["java"], "-cp", str(d8), "com.android.tools.r8.D8", "--release", "--min-api", "26", "--lib", str(libraries[0]), "--classpath", str(libraries[1]), "--classpath", str(libraries[2]), "--output", str(build), *map(str, sorted(classes.rglob("*.class")))], check=True)
        checksums = "".join(f"{hashlib.sha256(source.read_bytes()).hexdigest()}  {source.name}\n" for source in sources)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, content in (("classes.dex", (build / "classes.dex").read_bytes()), ("META-INF/sources.sha256", checksums.encode())):
                entry = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o644 << 16
                archive.writestr(entry, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    packaged = root / "vivo_power" / "resources" / "vivo-background-power.jar"
    if args.output.resolve() != packaged.resolve():
        packaged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.output, packaged)
    print(f"Built {args.output}")
    print(f"Packaged {packaged}")


if __name__ == "__main__":
    main()
