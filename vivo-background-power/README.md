# Vivo background power script

This terminal script targets the vivo X200 Pro running Android 16 / OriginOS 6 with the English Settings interface. For every selected main-profile package, it sets and reads back `RUN_IN_BACKGROUND allow`, `RUN_ANY_IN_BACKGROUND allow`, and UID-level `AUTO_REVOKE_PERMISSIONS_IF_UNUSED ignore`. It clears conflicting background UID overrides when needed and requests `cmd deviceidle whitelist +PACKAGE`. It then uses the phone's built-in UIAutomator to select **Background power control → Allow background power usage** for each supported app. It uploads a temporary JAR; it does not install an APK or require root.

The earlier background/native run covered 629 main-profile packages: both Android background AppOps were verified as Allow for all 629, and the native setting was verified as Allow after reopening for 249 apps. The other 380 packages exposed no native control. Of the 249 verified native settings, 238 changed from Smart or Restrict to Allow and 11 were already Allow. The full run took about 78 minutes with system packages included. The auto-revoke and timeout command paths were verified separately on this phone; whitelist testing showed that accepted requests can later be removed by vivo. The added combined command/reporting flow has been checked locally, without repeating the full phone run. Navigation checks wait for the resulting screen, and radio checks wait for all selection states to settle.

## Use the preset in the GUI

Select **Persist V2413** from the Preset dropdown. Its panel contains independent per-app policies, device-wide controls, and run modes. **Preview and run** shows the exact frozen queue before applying the selected choices. **Verify current settings** measures them without policy changes; native verification still requires an unlocked, idle phone.

**Retry unverified work** applies only settings that lack successful evidence for the same installation. Select **Recheck unavailable native controls** to retry those controls explicitly. After verification finds a changed setting, select the affected apps and policies and use retry or all-selected mode to apply corrections.

The device-wide timeout offers **Leave unchanged**, **Set effectively unreachable**, and **Remove local override**. Set and remove actions work with no apps selected. Leaving it unchanged preserves an existing override. See the timeout section below for its scope and limitations.

## Run after reconnecting

The terminal entry and GUI share the same ADB engine, saved checkpoints, and final checks. Install this project as described in the [project README](../README.md). The current application requires Python 3.11+, PySide6, and ADB. These dependencies replace the original standalone script's Python 3.9-only requirement.

Unlock the phone, authorize USB debugging, and leave it idle throughout the run. The script moves through Settings automatically. Run:

```bash
mise exec -- python vivo-background-power/allow-background-power.py
```

The application includes the matching JAR in `vivo_power/resources`; source-tree runs can also find the original bundle copy. Keep the shared `vivo_power` package with this entry script when moving the project. Native sessions use Qt's per-phone process lock to prevent concurrent navigation. The script finds ADB on PATH or in `~/Android/Sdk/platform-tools`; use `--adb /path/to/adb` if needed, or `--serial SERIAL` for a particular phone.

To run only one app later:

```bash
mise exec -- python vivo-background-power/allow-background-power.py \
  --package com.documentsui.shortcut
```

By default, all per-package changes and native UI attempts cover every package in the main profile, including system apps. Unavailable native controls are recorded as skipped. To exclude system apps from all per-package steps, run:

```bash
mise exec -- python vivo-background-power/allow-background-power.py \
  --exclude-system
```

The script cannot change a native setting that vivo does not expose, and it does not unlock or automate private/cloned profiles. The auto-revoke setting uses Android's UID-level switch, so packages sharing a UID also share that exemption, including when using `--package`.

## Optional unused-app timeout override

To make the unused-app timeout effectively unreachable for existing and future apps, add:

```bash
mise exec -- python vivo-background-power/allow-background-power.py \
  --unreachable-unused-timeout
```

This applies the persistent local override `permissions/auto_revoke_unused_threshold_millis2=9223372036854775807` and verifies both its effective value and stored override. It affects unused-app permission revocation and hibernation device-wide, across profiles, regardless of `--package` or `--exclude-system`. It does not flip future apps' individual Settings toggles. Omitting the flag leaves the existing global timeout unchanged, including an override applied previously.

To remove only this global override later:

```bash
adb shell device_config clear_override permissions auto_revoke_unused_threshold_millis2
```

Per-app auto-revoke exemptions remain after clearing the global override. Neither operation restores permissions already revoked.

## Doze whitelist results

Whitelist requests are recorded individually, including unknown-package rejections that can return exit code zero. A rejection does not stop the other packages or native UI processing. At the end, the script reads the full whitelist and records each selected package's membership in its `user` or `system` entries. A `system-excidle` entry alone does not count as a full Doze whitelist entry.

Command acceptance and final membership are separate report fields. Vivo may remove entries after accepting them; the script does not repeatedly reapply removed entries or disable Doze. A retained entry is a snapshot, not proof of reboot durability. The summary never assumes that every package became fully Unrestricted.

Exit code `0` means the run completed with all whitelist requests accepted and selected names present at final readback. Exit code `2` means the completed run has whitelist request or membership issues; argument errors also use `2`. Command/UI failures use `1`, and Ctrl+C uses `130`. Review the report for the actual per-package results.

Every native success requires the correct nonempty app label and **Allow** still selected after leaving and reopening the page. Each app starts with a fresh battery-settings task so a previous app's screen cannot receive the next package's intent. Missing or disabled controls are recorded as skipped. Unexpected UI states stop the session. A version-3 JSON report records the immutable selection, installation identities, selected settings, per-operation results and verification times, initial and final observations, native resources, and device-wide timeout; choose its destination with `--report /path/to/report.json`. Ctrl+C stops the session and keeps completed changes.

Temporary phone files are removed only after the owned helper is confirmed stopped. A heartbeat lease and bounded per-app watchdog limit navigation after host loss. If disconnected mid-run, saved resources remain pending for reconnect recovery. The GUI can pause, restart, resume unfinished work, and process new or changed installations. Ctrl+C keeps the CLI checkpoint for later GUI recovery; no install watcher runs. The optional global timeout override continues to affect future apps without rerunning, while their per-app switches and whitelist/native choices require another run. Unrelated permissions and settings are preserved.

`--verify-only` measures selected settings without applying them; native checks still require the phone idle. `--no-native` runs the Android policies without navigating Settings. `--state PATH` selects a checkpoint store, and `--report PATH` selects the exported report. The original flags and system-app inclusion defaults remain available.

`VivoBackgroundPower.java` and `PackageInventory.java` are the sources of the included helper JAR. See [VALIDATION.md](../VALIDATION.md) for the new one-app physical checks and outstanding Windows, lifecycle, and full-run acceptance. The earlier 629-app record above describes the original bundle.

See the [ADBeam me up user guide](../README.md) for GUI installation, presets, app selection, settings, pause/resume, and reports.
