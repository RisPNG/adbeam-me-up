# Validation record

Updated: 1 October 2026. The original bundle's prior full run is documented separately in `vivo-background-power/README.md`.

Final automated result: **139 tests passed** in 9.372 seconds using the installed MsPy environment. Both helper JAR copies were rebuilt after the process-ownership changes, and their embedded source hashes match the Java sources. On 30 September, the wheel then current was compared with its application modules and prebuilt helper, installed in an isolated temporary directory, and launched successfully with Qt offscreen on Linux; shortcut repair stayed disabled for that installed wheel. The portable setup was exercised with the actual MsPy runtime as described below.

## Physical device

Checked on the connected vivo X200 Pro (V2413 / PD2405F_EX), Android 16 API 36, OriginOS 6, English `en-US`, main profile 0. On 1 October, the read-only helper discovered **629 apps: 249 user apps and 380 system apps**, with labels, UIDs, system flags, version codes, and installation/update dates.

The actual Qt window, preset panel, queue preview, background worker, saved history, and report exports were exercised using the real ADB engine with Qt's offscreen platform. These were physical phone operations, with isolated host state and preferences:

| Check | Result |
| --- | --- |
| Verify Files and WhatsApp | Completed 20/20 operations; both background AppOps, UID-level permission exemption, native Allow after reopening, and final Doze membership verified for both apps. No policy writes. |
| Apply Files | Native Smart changed to Allow and survived reopening. The Doze add request was accepted, and membership remained present at final readback. All five selected settings finished verified or already configured. |
| Pause, close, restart, resume | Paused safely after 4/10 operations, released the phone, recreated the window from saved state, rescanned and reviewed the queue, then completed 10/10 operations. |
| Device-wide timeout | Read-only checks distinguished matching and mismatching requested actions. Actual GUI runs with no apps selected cleared and set the override, verified the requested action, and refreshed the panel's measured values. |
| Host crash recovery | Killed the test host while its native helper was active. Recovery confirmed the exact session token, stopped the owned helper, removed its directory, cleared saved resources, and marked the phone available. |
| Final release | Original timeout override restored; both apps' Android policies and Doze membership verified again. No helper processes, temporary phone files, or matching generated cache files remained. Phone returned to Home. |

The original effective and stored timeout were both `9223372036854775807`. Clearing the local override produced stored `null` and effective `7776000000`; setting restored both values to `9223372036854775807`. That original state was confirmed before release.

The crash check exposed that this phone rewrites the native helper's process arguments to `uiautomator`, hiding the session directory. The helper now inherits an exact `ADBEAM_SESSION` ownership token, which was observed in the live process environment before the host was killed. Inventory uses a session-specific process name and PID file. Recovery leaves ambiguous older active processes pending rather than declaring the phone available without proof.

A subsequent selected-device `adb reconnect` returned success but did not rediscover the still-connected USB phone on this host. Restarting the host ADB server restored the same hardware identity, and the final GUI and release checks then passed. Automatic rediscovery after that SDK command is not an established outcome; the application does not use it for normal device discovery.

### Earlier check: 30 September

The earlier inventory contained 631 apps. The following small acceptance check preceded the broader checks above.

For `com.documentsui.shortcut` (Files), the updated native helper's read-only workflow observed Allow, left and reopened the policy page, confirmed Allow, closed the screens, and reported `phone_free: true`. Its native navigation test took approximately 4.8 seconds.

The new shared ADB engine and run controller checked both background AppOps, UID-level auto-revoke, Doze membership, and native policy for that app in verification mode. Both background modes were Allow, UID auto-revoke was Ignore, native Allow remained selected after reopening, and Doze membership was present at readback. Its application paths for background, auto-revoke, and native settings reported Already configured. The global timeout was only read, and no Doze add request was made during this acceptance check.

All temporary phone files and matching generated cache entries were removed, no helper or Settings navigation was left running, and the phone was released to the user. That earlier check did not attempt a full app run, new-app installation, forced physical disconnect, or global timeout mutation.

The first controller smoke check exposed a host result-label mismatch for an unchanged timeout; this was corrected locally. The physical results above came from the actual setting readbacks, not from that initial run-summary label.

## Automated checks

The test suite covers independent setting choices, verification without policy writes, conflicting UID modes, shared UID auto-revoke, system exclusion, fixed selections, five newly installed apps, updates/reinstalls, stale historical evidence, timeout preservation and global-only set/clear, checkpoint recovery, pause/restart/resume/stop, rejected whitelist requests with exit code zero, accepted requests absent later, accurate report import/export, and native cleanup ownership.

Acceptance regressions cover requested timeout mismatches remaining visible at final verification; pending pause/stop state surviving checkpoints and restart; shared recovery before new work in the GUI, controller, and CLI; durable cleanup records when the CLI's initial inventory fails; failed scans and unconfirmed runs invalidating stale readiness; fresh idle-phone confirmation after a run; and preset-owned timeout display updates from measured results. Ownership tests cover inventory PID reporting, rewritten native arguments, exact environment tokens, reused PIDs, and ambiguous legacy helpers.

The 23 automated Qt offscreen tests exercise the actual window, inventory model, dialogs, and background workers with simulated ADB responses. They include exact queue previews with hidden selections, an end-to-end reviewed run, read-only scanning, timeout controls without app selections, responsive close/pause, durable inventory cleanup, and imported history. Separate physical Qt workflows are recorded above. The rendered interface was inspected. A wheel was built, installed into an isolated temporary directory, and its GUI launched and closed using Qt offscreen; the packaged Android helper was found in that installation.

The Java source is compiled and executed against a simulated UIAutomator environment. Tests preserve Allow/Smart/Restrict in verification mode, verify applied changes after reopening, reject incorrect app labels, distinguish absent/disabled native controls, and terminate navigation when a heartbeat expires during a blocked UI call. The bundled and packaged JARs include hashes matching their Java sources.

## Preset modules and rename

The window renders as **ADBeam me up**, with **Preset** above the ADB field and **Persist V2413** selected. Its policy controls and queue review are owned by that preset's panel. Tests use a second, test-only module to verify that switching replaces the panel, engine, controller, result columns, and history; clears device/inventory context and idle-phone confirmation; and is prevented during active work. The shipped registry contains only Persist V2413.

Run and report tests check preset-specific evidence, rejection of another preset's resume before phone operations, malformed report metadata, and compatibility with older reports that lack preset metadata. Phone cleanup still covers resources from all presets for the same phone/profile. The application retains its existing Qt storage identity and instance lock, preserving saved settings and checkpoints through the display-name change. Shortcut tests check migration of this installation's old names and preservation of unrelated installations. The current preset's physical GUI workflows are recorded above.

The existing Linux portable installation was refreshed successfully without downloading another Python runtime. Its Desktop and Applications shortcuts now use the new name, and its matching old shortcuts were removed. The renamed GUI also launched on the actual desktop and held the existing application lock. Its idle launch did not scan or change a phone.

## Portable installation

The pinned MsPy 3.11.14 Linux release archive was downloaded and verified against the published SHA-256 digest. Its actual x86_64 interpreter created a virtual environment and installed this application with PySide6 / Qt 6.11.2. A fresh setup completed, a repeated setup skipped dependency installation, and moving the complete folder to another path rebuilt the environment, reused the project-local pip cache, and repaired both desktop and applications-menu entries. These paths included spaces, Unicode, an apostrophe, and a dollar sign.

All 16 GUI tests passed using the relocated portable environment and Qt offscreen. Its actual window and Installation → Repair shortcuts dialog launched, rendered, and closed successfully. Shortcut tests additionally use the native GIO desktop-entry parser to execute paths containing quotes, backticks, dollar signs, percent signs, and backslashes. Setup and shortcut tests isolate their home, application-data, and shortcut directories; no host desktop or menu shortcuts were left behind, and no additional phone operations were performed.

The release metadata, Windows archive layout, and x64 interpreter architecture were inspected. Windows bootstrap, PowerShell COM shortcut creation, and relocation have not been executed on Windows; their implementation and structured arguments have only been reviewed and tested with mocked subprocess responses. Linux bootstrap tests cover verified staged installation, download/checksum/interpreter failure cleanup, unchanged valid runtimes, inherited Python environment variables, unsupported options, and platform restrictions. Portable environment tests cover first installation, dependency changes, failed installs, relocation, preferences, hidden launch logs, and shortcut repair.

## Setup terminal lifetime

The Linux setup launcher was exercised on the actual Wayland desktop using the user's installed MsPy runtime and PySide6 / Qt 6.11.2. Setup completed in a controlling pseudo-terminal, that terminal was closed, and the GUI remained running in its own process session. No phone work was initiated. A separate automated PTY regression checks that an isolated GUI process survives the terminal hangup, finishes normally, and retains both stdout and stderr.

Every GUI launch now saves diagnostics to the local application log and performs a one-second initial process check. An early nonzero exit fails setup with the log path; this initial check is not a complete application readiness test.

## Outstanding acceptance

- Windows launch and its complete supported workflow have not been run on a Windows machine.
- Desktop launch was checked, and actual phone workflows used Qt's offscreen platform. A full visible desktop GUI run across all phone apps remains outstanding.
- Installing five actual apps, physical cable disconnect/reconnect, and heartbeat expiration during a real disconnect have not been tested on the phone. Their state and lifecycle decisions are covered locally; an active-helper host crash and recovery were tested physically.
- The new application was not run across all 629 current apps. Compatibility beyond the stated model, firmware, language, and main profile remains unvalidated.

These limits must remain visible when assessing readiness. Successful local tests and the representative physical workflows do not establish the outstanding outcomes above.
