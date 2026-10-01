# Build the native helpers

The prebuilt `vivo-background-power.jar` contains the UIAutomator Settings helper and the read-only package inventory entry point. It runs through ADB without installing an APK. Keep both Java sources and the matching JAR together.

Rebuilding requires Python 3.9+, a JDK 17+, Android SDK platform `android-37.0`, and Android build tools `36.0.0`. Run from the repository root:

```bash
mise exec java@17.0.2 -- python3 tools/build_native.py --sdk /path/to/Android/Sdk
```

On Windows, use the same script with the SDK's Windows path and a JDK on PATH. `ANDROID_SDK_ROOT` or `ANDROID_HOME` can replace `--sdk`. `--platform` and `--build-tools` select other installed SDK versions; the defaults keep the build toolchain explicit. The script compiles Java 8 bytecode, converts it to DEX with D8, fixes archive timestamps, embeds source checksums, and updates both the bundle JAR and `vivo_power/resources/vivo-background-power.jar`.

The GUI launches one native app operation per invocation using `package`, `pidfile`, `heartbeat`, `mode` (`apply` or `verify`), and `expected_label` parameters. The installed `uiautomator` command inherits `ADBEAM_SESSION=<session-directory>` so the host can prove process ownership even when Android rewrites its arguments. The host touches the remote heartbeat before launch and every three seconds. The helper stops its own PID after a 15-second heartbeat gap or a 120-second app deadline. A forced termination reports `phone_free: false`; the host must confirm the owned process has stopped before making the phone available. Recovery checks the exact session directory in process arguments or the exact environment token; ambiguous older active helpers remain pending. The legacy `packages` file parameter remains supported for the CLI, with the 120-second deadline applied separately to each app. An optional `control` file accepts `run`, `pause`, or `stop` between apps.

Verification opens, leaves, and reopens the native page to observe Allow, Smart, or Restrict without selecting a radio. Results are emitted after the native pages close. The package inventory runs `phonepolicy.PackageInventory --user 0 --pidfile <session-directory>/pid` with `app_process --nice-name=<session-directory>`, records and emits its PID before collecting PackageManager metadata, emits `VIVO_APP` JSON records, and uses a 30-second deadline. It does not navigate Settings.

Compilation and matching source checksums verify the bundle build. Physical checks exercised the current inventory, apply and read-only native navigation, pause/restart/resume, and recovery after killing the host while its helper was active. The live helper retained its exact session token despite rewritten arguments, and recovery confirmed its termination and removed its files. Heartbeat expiration during a real disconnect and broader app/device acceptance remain outstanding; see [VALIDATION.md](../VALIDATION.md). The earlier bundle's full phone run does not establish these new paths.
