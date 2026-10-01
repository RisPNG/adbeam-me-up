# ADBeam me up

A Python and Qt desktop application for running Android Debug Bridge (ADB) presets against selected apps. Choose a preset, connect a device, review the work it will perform, and inspect the measured results. Saved checkpoints let you pause and resume unfinished runs, while reports keep a record of what was verified and when.

Each preset supplies its own device requirements, options, run controls, and result columns. The shared application handles device connection, app inventory, selection, progress, and history. Supported settings and device compatibility depend on the selected preset.

## Install and launch

Download and extract this repository, or clone it:

```bash
git clone https://github.com/RisPNG/adbeam-me-up.git
cd adbeam-me-up
```

Keep the whole project folder in a writable location. The setup launchers download and verify [MsPy's portable Python 3.11.14](https://github.com/RisPNG/MsPy/releases/tag/3.11.14), install the GUI dependencies locally, create shortcuts, and open the application. The first installation needs internet access. You do not need an installed Python, mise, JDK, or Android SDK platform; the included Android helper is prebuilt. The included preset does not require root or installing an APK on the phone.

### Linux

On **x86_64 Linux**, run this from the project folder:

```bash
bash setup_linux.sh
```

Setup needs Bash, `unzip`, `curl` or `wget`, and `sha256sum` or `shasum`. The GUI needs a working Wayland or X11 desktop and Qt's platform libraries; an X11 installation may need the distribution's `libxcb-cursor` package. Current Qt Linux wheels require glibc 2.34 or newer. USB access may require Android udev rules on distributions that restrict access.

### Windows

On **x64 Windows**, double-click **`setup_win.vbs`** in the project folder. It uses Windows' built-in PowerShell to prepare the runtime and launch the GUI. Your device may also require its manufacturer's USB driver.

### Shortcuts and local files

Setup creates **ADBeam me up** Desktop and Applications menu / Start menu shortcuts automatically; Windows also gets a launcher shortcut inside the project folder. Use **Installation → Repair shortcuts…** in the GUI to choose another desktop shortcut folder or disable the external shortcuts. Enabled shortcuts are checked and repaired on launch.

To move the installation, close the application, move the **whole project folder**, then run its setup launcher once from the new location. This rebuilds the local virtual environment when needed and updates the shortcuts. An old external shortcut uses an absolute path and cannot find a moved folder by itself. Saved runs and reports remain in Qt's application-data directory for your account.

Local Python, the virtual environment, and the pip cache live under `int/linux/` or `int/win/`; shortcut preferences live in `install.local.json`. GUI output and errors are saved to `bin/linux/logs/application.log` or `bin/win/logs/application.log` on every launch. The GUI runs independently of the setup terminal. Hidden launches also save setup output to `setup.log`; Windows saves setup output there for all launches. To install or repair without opening the GUI, run `bash setup_linux.sh --no-launch` on Linux or `wscript.exe setup_win.vbs --no-launch` on Windows.

Android Platform Tools (`adb`) are still required. Put ADB on PATH, set `ANDROID_SDK_ROOT` / `ANDROID_HOME`, or use the GUI's **Browse** button to select `adb` / `adb.exe`. Enable USB debugging, connect the phone, accept its authorization prompt, and choose **Find devices → Scan apps**.

## Use

1. Choose a **Preset** above the ADB field and read its displayed scope and device requirements.
2. Select ADB, choose **Find devices**, select the connected device, and choose **Scan apps**.
3. Search or filter the inventory and check the apps to include. System apps are included by default; use the exclusion option when appropriate.
4. Configure the selected preset's options and run mode. Available policies, device-wide actions, and verification choices belong to the preset.
5. Review the exact queue before starting. The preview includes checked apps hidden by search or filters. Apps installed during a run require another scan and never extend its queue.
6. Review **Results and history**. Open **Advanced details** for installation identity, timestamps, commands, and errors, then export JSON or a readable summary if needed.

Presets that navigate the device's Settings interface require the phone to remain unlocked and idle, including during native verification. Only one GUI instance runs at a time; do not run other phone automation concurrently.

For example, run a preset for your chosen apps, install more apps later, then scan again and review a queue for the new installations. The included preset offers **New apps only** and **New or changed installations** modes for this workflow; without a previous saved scan, every observed app is new.

### Runs and verification

**Pause** finishes the current bounded operation and stops Settings navigation before declaring the phone available. **Stop** preserves applied changes. A restart or disconnect preserves completed checkpoints; interrupted work remains unverified.

To continue, reconnect and scan, select the saved run in history, and choose the preset's resume option. Resume checks the preset, phone, and profile and reviews removed, updated, or reinstalled apps. Scanning recovers pending temporary resources before allowing continuation. Confirm the phone is unlocked and idle again before native work.

Verification measures selected settings without applying policy changes. Results record individual settings and their verification times; historical success describes what was observed at that time. Unavailable controls remain visible separately from successful settings. Retry options and device-wide actions depend on the preset; stopping a run does not undo completed changes.

Import schema-1/2 terminal reports or schema-3 GUI reports as historical evidence. Missing checks and unknown verification dates remain unknown. Imported reports cannot resume a run.

## Included presets

The application currently ships with one preset:

| Preset | Purpose | Validated scope | Guide |
| --- | --- | --- | --- |
| **Persist V2413** | Background execution, unused-app policies, vivo background power controls, and Doze requests | vivo X200 Pro, Android 16 / OriginOS 6, English Settings, main profile (0) | [Settings, limitations, and terminal usage](presets/vivo-background-power/README.md) |

The preset guide explains shared-UID effects, device-wide overrides, native control availability, and the distinction between command acceptance and verified readback. Other configurations show their validation limits in the application.

## State and reports

GUI checkpoints live under Qt's writable application-data directory for your account. Use `--state-dir PATH` to choose another location. The preset's terminal entry uses the operating system's user state directory. State and reports contain phone identifiers, installed package names, and command results. They are local files; export and share them deliberately.

Runs and verification evidence are isolated by preset ID. Reports without a preset ID belong to Persist V2413 for compatibility with earlier versions. The included preset's [terminal entry](presets/vivo-background-power/README.md) uses the same engine and checkpoint/report semantics as the GUI.

## Development and validation

An installed mise toolchain is an alternative to the portable setup:

```bash
mise trust
mise install
mise exec -- python -m venv .venv
mise exec -- .venv/bin/python -m pip install -e .
mise exec -- .venv/bin/python -m vivo_power
```

Run the automated checks or rebuild the Android helper:

```bash
QT_QPA_PLATFORM=offscreen mise exec -- .venv/bin/python -m unittest discover -s tests -v
mise exec -- python tools/build_native.py
```

The native test suite compiles and executes the actual Java source against an isolated UIAutomator simulator, so a JDK is required for those tests. Native rebuilds additionally need Android SDK platform and build-tools files; see [native-build.md](presets/vivo-background-power/native-build.md). A wheel includes the matching prebuilt helper.

Preset bundles live under the top-level [`presets/`](presets/) directory. Each bundle keeps its terminal entry, native sources and helper JAR, usage guide, and build notes together. The included bundle is [`presets/vivo-background-power/`](presets/vivo-background-power/); the matching helper is also packaged in `vivo_power/resources/` for installed applications.

Runtime preset descriptors and GUI panels live in [`vivo_power/presets/`](vivo_power/presets/) and are registered in [`registry.py`](vivo_power/presets/registry.py). Each `Preset` descriptor supplies its engine, run controller, options panel, device observations, setting labels, and result columns. A preset's descriptor and sibling GUI module keep its workflow together, while the shared window handles connection, inventory, progress, history, and installation. To add a preset, implement its descriptor and panel, register it alongside the existing preset, and keep any bundle assets and documentation under `presets/`.

See [VALIDATION.md](VALIDATION.md) for the exact automated, Linux GUI, and physical-device checks completed, and outstanding Windows and broader phone acceptance checks. The original bundle's full run is a baseline, not evidence of a full GUI run.
