from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import threading
import tempfile
import time
import uuid


class AdbError(RuntimeError):
    pass


class AdbEngine:
    def __init__(self, adb_path=None, serial=None, resource_callback=None):
        sdk = Path(os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or Path.home() / "Android/Sdk")
        executable = "adb.exe" if os.name == "nt" else "adb"
        self.adb_path = str(adb_path or shutil.which("adb") or sdk / "platform-tools" / executable)
        self.serial = serial
        self.resource_callback = resource_callback
        self.pending_cleanup = []
        bundled = Path(__file__).resolve().parent / "resources" / "vivo-background-power.jar"
        self.jar_path = bundled if bundled.is_file() else Path(__file__).resolve().parent.parent / "presets" / "vivo-background-power" / "vivo-background-power.jar"
        self._device_info = None
        self._inventory = {}
        self._commands = []
        self._command_lock = threading.Lock()
        self._native_lock = threading.Lock()

    def _adb(self, arguments, timeout=20, check=True, record=True, selected=True):
        command = [self.adb_path]
        if selected:
            if not self.serial:
                raise AdbError("Select an authorized device first")
            command += ["-s", self.serial]
        if arguments and arguments[0] == "shell":
            command += ["shell", shlex.join(str(item) for item in arguments[1:])]
        else:
            command += [str(item) for item in arguments]
        started = time.monotonic()
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as error:
            if record:
                with self._command_lock:
                    self._commands.append({"command": command, "error": str(error), "elapsed_seconds": round(time.monotonic() - started, 3)})
            raise AdbError(str(error)) from error
        if record:
            with self._command_lock:
                self._commands.append({"command": command, "returncode": result.returncode,
                                       "stdout": result.stdout, "stderr": result.stderr,
                                       "elapsed_seconds": round(time.monotonic() - started, 3)})
        if check and result.returncode:
            raise AdbError((result.stdout + result.stderr).strip() or f"ADB command failed ({result.returncode})")
        return result

    def devices(self):
        output = self._adb(["devices", "-l"], selected=False, record=False).stdout
        devices = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 2 or line.startswith("List of devices") or line.startswith("*"):
                continue
            devices.append({"serial": parts[0], "state": parts[1], "description": " ".join(parts[2:])})
        return devices

    def device(self):
        devices = self.devices()
        authorized = [item["serial"] for item in devices if item["state"] == "device"]
        if not self.serial and len(authorized) == 1:
            self.serial = authorized[0]
        connected = next((item for item in devices if item["serial"] == self.serial), None)
        if not connected or connected["state"] != "device":
            state = connected["state"] if connected else "disconnected"
            return {"serial": self.serial, "identity": None, "model": "", "name": self.serial or "",
                    "android": "", "origin_os": "", "locale": "", "profile": 0, "ready": False,
                    "reason": "Authorize USB debugging on the phone" if state == "unauthorized" else "Connect and select one authorized phone",
                    "native_supported": False, "native_reason": "An authorized connection is required", "state": state}
        output = self._adb(["shell", "getprop"], record=False).stdout
        props = dict(re.findall(r"(?m)^\[([^]]+)\]: \[([^]]*)\]", output))
        model = props.get("ro.product.model", "")
        name = props.get("ro.product.marketname") or props.get("ro.vivo.market.name") or model
        hardware_serial = props.get("ro.serialno") or props.get("ro.boot.serialno")
        identity = hashlib.sha256(json.dumps([hardware_serial or self.serial, model], separators=(",", ":")).encode()).hexdigest()
        locale = props.get("persist.sys.locale") or props.get("ro.product.locale", "")
        sdk = int(props.get("ro.build.version.sdk", "0") or "0")
        origin = props.get("ro.vivo.os.build.display.id") or props.get("ro.vivo.os.version", "")
        power = self._adb(["shell", "pm", "path", "com.iqoo.powersaving"], check=False, record=False)
        power_available = power.returncode == 0 and power.stdout.startswith("package:")
        power_version = ""
        if power_available:
            details = self._adb(["shell", "dumpsys", "package", "com.iqoo.powersaving"], check=False, record=False).stdout
            match = re.search(r"versionCode=(\d+)", details)
            power_version = match[1] if match else ""
        tested_model = "x200 pro" in (name + " " + model).lower() or (model == "V2413" and props.get("ro.product.model.bbk") == "PD2405F_EX")
        if tested_model and name == model:
            name = "vivo X200 Pro"
        active_user = self._adb(["shell", "am", "get-current-user"], record=False).stdout.strip()
        main_profile = active_user == "0"
        native_supported = sdk == 36 and origin.strip() == "OriginOS 6" and tested_model and locale.lower().startswith("en") and power_available and main_profile
        native_reason = ("Tested model and English Settings; native controls are checked separately for every app"
                         if native_supported else "Native automation is validated for vivo X200 Pro, Android 16 / OriginOS 6, English Settings with vivo power settings")
        info = {"serial": self.serial, "identity": identity, "identity_source": "hardware" if hardware_serial else "transport",
                "hardware_serial": hardware_serial, "model": model, "name": name, "android": props.get("ro.build.version.release", ""),
                "origin_os": origin, "locale": locale, "profile": 0, "active_profile": int(active_user) if active_user.isdigit() else None,
                "ready": main_profile, "reason": "Main profile only" if main_profile else "Switch the phone to its main profile (user 0) before continuing",
                "native_supported": native_supported, "native_reason": native_reason, "state": "device", "sdk": sdk,
                "build": props.get("ro.build.fingerprint", ""), "power_version": power_version,
                "configuration": ":".join((props.get("ro.build.fingerprint", ""), power_version, locale))}
        self._device_info = info
        return info

    def inventory(self):
        device = self.device()
        if not device["ready"]:
            raise AdbError(device["reason"])
        resource = {"serial": self.serial, "identity": device["identity"],
                    "remote_dir": "/data/local/tmp/vivo-background-" + uuid.uuid4().hex,
                    "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "kind": "inventory", "launched": False, "pid": None}
        resource["pidfile"] = resource["remote_dir"] + "/pid"
        records = []
        self.pending_cleanup.append(resource)
        if self.resource_callback:
            self.resource_callback(dict(resource))
        try:
            self._adb(["shell", "mkdir", "-p", resource["remote_dir"]], record=False)
            remote_jar = resource["remote_dir"] + "/vivo-background-power.jar"
            self._adb(["push", self.jar_path, remote_jar], timeout=30, record=False)
            resource["launched"] = True
            if self.resource_callback:
                self.resource_callback(dict(resource))
            command = ("CLASSPATH=" + shlex.quote(remote_jar) + " app_process /system/bin --nice-name="
                       + shlex.quote(resource["remote_dir"]) + " phonepolicy.PackageInventory --user 0 --pidfile "
                       + shlex.quote(resource["pidfile"]))
            result = self._adb(["shell", "sh", "-c", command], timeout=45, record=False)
            resource["launch_finished"] = True
            complete = False
            for line in result.stdout.splitlines():
                if line.startswith("VIVO_PID "):
                    resource["pid"] = int(line.split()[1])
                elif line.startswith("VIVO_APP "):
                    app = json.loads(line[9:])
                    if not isinstance(app, dict) or not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*", app.get("package", "")):
                        raise AdbError("Invalid app metadata from the phone")
                    records.append(app)
                elif line.startswith("VIVO_INVENTORY_COMPLETE "):
                    complete = int(line.split()[1]) == len(records)
            if self.resource_callback:
                self.resource_callback(dict(resource))
            if not complete:
                raise AdbError("The phone did not finish its main-profile inventory")
        finally:
            remaining = self.recover_cleanup([resource])
            if not remaining:
                self.pending_cleanup = [item for item in self.pending_cleanup if item["remote_dir"] != resource["remote_dir"]]
                if self.resource_callback:
                    self.resource_callback(None)
        packages_by_uid = {}
        for item in records:
            packages_by_uid.setdefault(item["uid"], []).append(item["package"])
        for item in records:
            item["shared_uid_packages"] = sorted(package for package in packages_by_uid[item["uid"]] if package != item["package"])
        self._inventory = {item["package"]: item for item in records}
        return sorted(records, key=lambda item: item["package"])

    def perform(self, package, setting, verify_only=False):
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*", package):
            raise ValueError("Invalid package name")
        if setting not in {"RUN_IN_BACKGROUND", "RUN_ANY_IN_BACKGROUND", "AUTO_REVOKE_PERMISSIONS_IF_UNUSED", "native", "doze"}:
            raise ValueError("Unknown setting")
        self._commands = []
        result = {"status": "pending", "checked_at": None, "before": None, "observed": None, "error": None, "commands": []}
        try:
            if setting == "native":
                result.update(self._native_policy(package, verify_only))
            elif setting == "doze":
                result.update({"request": "not_requested", "membership": None})
                if not verify_only:
                    added = self._adb(["shell", "cmd", "deviceidle", "whitelist", "+" + package], check=False)
                    response = (added.stdout + added.stderr).strip()
                    result["response"] = response
                    result["request_returncode"] = added.returncode
                    result["request_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    if added.returncode:
                        result.update(status="failed", request="error", error=response or f"Command returned {added.returncode}")
                    elif "Unknown package: " + package in response:
                        result.update(status="rejected", request="rejected_unknown_package")
                    elif "Added: " + package in added.stdout:
                        result.update(status="verified", request="accepted")
                    else:
                        result.update(status="not_checked", request="unconfirmed")
                whitelist = self._adb(["shell", "cmd", "deviceidle", "whitelist"])
                if whitelist.stderr.strip():
                    raise AdbError(whitelist.stderr.strip())
                names = {parts[1] for line in whitelist.stdout.splitlines()
                         if len(parts := line.split(",")) == 3 and parts[0] in {"user", "system"}}
                result["membership"] = package in names
                result["observed"] = package in names
                if verify_only or result["status"] == "verified":
                    result["status"] = "verified" if package in names else "not_present"
                result["checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            else:
                uid_level = setting == "AUTO_REVOKE_PERMISSIONS_IF_UNUSED"
                expected = "ignore" if uid_level else "allow"
                before = self._adb(["shell", "cmd", "appops", "get", "--user", "0", package, setting]).stdout
                result["before"] = before.strip()
                observed = before
                uid_mode = re.search(r"(?m)^Uid mode:\s*" + setting + r": (\w+)", observed)
                package_mode = re.search(r"(?m)^(?:" + setting + r"|Default mode): (\w+)", observed)
                configured = ((uid_mode is not None and uid_mode[1] == expected) if uid_level else
                              (package_mode is not None and package_mode[1] == expected and (uid_mode is None or uid_mode[1] == expected)))
                already = configured
                if not verify_only and not configured:
                    changed = self._adb(["shell", "cmd", "appops", "set", "--user", "0"]
                                        + (["--uid"] if uid_level else []) + [package, setting, expected])
                    if changed.stderr.strip() or changed.stdout.strip():
                        raise AdbError((changed.stderr + changed.stdout).strip())
                    observed = self._adb(["shell", "cmd", "appops", "get", "--user", "0", package, setting]).stdout
                    uid_mode = re.search(r"(?m)^Uid mode:\s*" + setting + r": (\w+)", observed)
                    if not uid_level and uid_mode is not None and uid_mode[1] != expected:
                        repaired = self._adb(["shell", "cmd", "appops", "set", "--user", "0", "--uid", package, setting, expected])
                        if repaired.stderr.strip() or repaired.stdout.strip():
                            raise AdbError((repaired.stderr + repaired.stdout).strip())
                        observed = self._adb(["shell", "cmd", "appops", "get", "--user", "0", package, setting]).stdout
                        uid_mode = re.search(r"(?m)^Uid mode:\s*" + setting + r": (\w+)", observed)
                    package_mode = re.search(r"(?m)^(?:" + setting + r"|Default mode): (\w+)", observed)
                    configured = ((uid_mode is not None and uid_mode[1] == expected) if uid_level else
                                  (package_mode is not None and package_mode[1] == expected and (uid_mode is None or uid_mode[1] == expected)))
                    if configured:
                        flushed = self._adb(["shell", "cmd", "appops", "write-settings"])
                        if flushed.stderr.strip():
                            raise AdbError(flushed.stderr.strip())
                result["observed"] = {"package_mode": package_mode[1] if package_mode else None,
                                      "uid_mode": uid_mode[1] if uid_mode else None, "readback": observed.strip()}
                result["status"] = ("already_configured" if already and not verify_only else "verified") if configured else ("not_present" if verify_only else "failed")
                if not configured and not verify_only:
                    result["error"] = f"AppOps readback was not {expected} for {package} / {setting}"
                result["checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        except (AdbError, json.JSONDecodeError) as error:
            result.update(status="failed", error=str(error))
        result["commands"] = list(self._commands)
        return result

    def timeout(self, action="unchanged", verify_only=False):
        if action not in {"unchanged", "set", "clear"}:
            raise ValueError("Unknown timeout action")
        self._commands = []
        key = "auto_revoke_unused_threshold_millis2"
        result = {"action": action, "status": "pending", "checked_at": None, "before": None,
                  "effective": None, "override": None, "effective_value": None, "override_value": None, "error": None, "commands": []}
        try:
            before_effective = self._adb(["shell", "device_config", "get", "permissions", key]).stdout.strip()
            before_override = self._adb(["shell", "device_config", "get", "device_config_overrides", "permissions:" + key]).stdout.strip()
            result["before"] = {"effective": before_effective, "override": before_override}
            if not verify_only and action != "unchanged":
                if action == "set":
                    self._adb(["shell", "device_config", "override", "permissions", key, "9223372036854775807"])
                else:
                    self._adb(["shell", "device_config", "clear_override", "permissions", key])
                effective = self._adb(["shell", "device_config", "get", "permissions", key]).stdout.strip()
                override = self._adb(["shell", "device_config", "get", "device_config_overrides", "permissions:" + key]).stdout.strip()
            else:
                effective, override = before_effective, before_override
            configured = (effective == override == "9223372036854775807") if action == "set" else override in {"", "null"}
            result.update(effective=effective, override=override, effective_value=effective, override_value=override,
                          status="unchanged" if action == "unchanged" else "verified" if configured else "not_present" if verify_only else "failed",
                          checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
            if action != "unchanged" and not configured and not verify_only:
                result["error"] = "The effective and stored timeout override did not match the requested action"
        except (AdbError, json.JSONDecodeError) as error:
            result.update(status="failed", error=str(error))
        result["commands"] = list(self._commands)
        return result

    def _native_policy(self, package, verify_only):
        device = self._device_info or self.device()
        if not device.get("ready"):
            raise AdbError(device.get("reason", "Device not ready"))
        if not device["native_supported"]:
            return {"status": "unavailable", "error": device["native_reason"], "before": None, "observed": None, "checked_at": None}
        if not self._native_lock.acquire(blocking=False):
            raise AdbError("Another native operation is already running on this engine")
        try:
            from PySide6.QtCore import QLockFile
            phone_lock = QLockFile(str(Path(tempfile.gettempdir()) / ("vivo-background-native-" + device["identity"] + ".lock")))
            phone_lock.setStaleLockTime(0)
            if not phone_lock.tryLock(0):
                raise AdbError("A native session is already running for this phone in another process")
        except ImportError as error:
            self._native_lock.release()
            raise AdbError("Install this project's PySide6 runtime to use native Settings automation") from error
        except BaseException:
            self._native_lock.release()
            raise
        resource = {"serial": self.serial, "identity": device["identity"],
                    "remote_dir": "/data/local/tmp/vivo-background-" + uuid.uuid4().hex,
                    "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "kind": "native", "lease_seconds": 15, "launched": False, "pid": None}
        resource.update(pidfile=resource["remote_dir"] + "/pid", heartbeat=resource["remote_dir"] + "/heartbeat")
        stop_lease = threading.Event()
        lease_thread = None
        self.pending_cleanup.append(resource)
        try:
            if self.resource_callback:
                self.resource_callback(dict(resource))
            self._adb(["shell", "mkdir", "-p", resource["remote_dir"]])
            remote_jar = resource["remote_dir"] + "/vivo-background-power.jar"
            self._adb(["push", self.jar_path, remote_jar], timeout=30)
            self._adb(["shell", "touch", resource["heartbeat"]], record=False)
            resource["launched"] = True
            if self.resource_callback:
                self.resource_callback(dict(resource))
            lease_thread = threading.Thread(target=self._maintain_native_lease, args=(resource, stop_lease), daemon=True)
            lease_thread.start()
            arguments = ["shell", "env", "ADBEAM_SESSION=" + resource["remote_dir"],
                         "uiautomator", "runtest", "/system/framework/android.test.base.jar", remote_jar,
                         "-c", "phonepolicy.VivoBackgroundPower", "-e", "package", package,
                         "-e", "pidfile", resource["pidfile"], "-e", "heartbeat", resource["heartbeat"],
                         "-e", "mode", "verify" if verify_only else "apply"]
            label = self._inventory.get(package, {}).get("label")
            if label:
                arguments += ["-e", "expected_label", label]
            output = self._adb(arguments, timeout=150, check=False)
            resource["launch_finished"] = True
            outcome = None
            ended = None
            for line in output.stdout.splitlines():
                if line.startswith("VIVO_PID "):
                    resource["pid"] = int(line.split()[1])
                elif line.startswith("VIVO_RESULT "):
                    outcome = json.loads(line[12:])
                elif line.startswith("VIVO_END "):
                    ended = json.loads(line[9:])
            resource["completed_ack"] = bool(ended and ended.get("phone_free"))
            if self.resource_callback:
                self.resource_callback(dict(resource))
            if not ended or not ended.get("phone_free") or not outcome or outcome.get("package") != package:
                raise AdbError("The native session ended without a verified app result and closed-screen acknowledgement: "
                               + (output.stdout + output.stderr)[-4000:])
            status = outcome.get("status")
            if status == "not_configured":
                status = "not_present"
            if status not in {"verified", "already_configured", "unavailable", "failed", "not_present"}:
                raise AdbError("The native helper reported an unknown outcome")
            if status in {"verified", "already_configured"} and outcome.get("after") != "allow":
                raise AdbError("The native result did not observe Allow after reopening")
            return {"status": status, "before": outcome.get("before"), "observed": outcome.get("after"),
                    "checked_at": outcome.get("verified_at") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "error": outcome.get("reason") if status in {"failed", "unavailable"} else None,
                    "phone_free": True}
        finally:
            stop_lease.set()
            if lease_thread:
                lease_thread.join(timeout=7)
            try:
                remaining = self.recover_cleanup([resource])
                if not remaining:
                    self.pending_cleanup = [item for item in self.pending_cleanup if item["remote_dir"] != resource["remote_dir"]]
                    if self.resource_callback:
                        self.resource_callback(None)
            finally:
                phone_lock.unlock()
                self._native_lock.release()

    def _maintain_native_lease(self, resource, stop):
        while not stop.wait(3):
            try:
                self._adb(["shell", "touch", resource["heartbeat"]], timeout=5, check=True, record=False)
            except AdbError:
                return

    def _owns_session_process(self, resource, pid):
        commandline = self._adb(["shell", "cat", f"/proc/{pid}/cmdline"], timeout=5, check=False, record=False)
        if commandline.returncode == 0:
            if resource["remote_dir"] in commandline.stdout:
                return True
            if resource.get("kind") == "inventory" and "phonepolicy.PackageInventory" in commandline.stdout.replace("\0", " ").split():
                raise AdbError("An active inventory helper does not identify its session; cleanup remains pending")
            if resource.get("kind") != "native" or commandline.stdout.split("\0", 1)[0] != "uiautomator":
                return False
            environment = self._adb(["shell", "cat", f"/proc/{pid}/environ"], timeout=5, check=False, record=False)
            if environment.returncode == 0:
                entries = environment.stdout.split("\0")
                if "ADBEAM_SESSION=" + resource["remote_dir"] in entries:
                    return True
                if any(entry.startswith("ADBEAM_SESSION=") for entry in entries):
                    return False
                raise AdbError("An active native helper has no readable session ownership token; cleanup remains pending")
        exists = self._adb(["shell", "test", "-d", f"/proc/{pid}"], timeout=5, check=False, record=False)
        if exists.returncode == 0:
            raise AdbError("The saved helper PID exists but its ownership cannot be read")
        if exists.returncode != 1:
            raise AdbError("The saved helper PID could not be checked")
        return False

    def recover_cleanup(self, resources):
        unresolved = []
        for original in resources:
            resource = dict(original)
            directory = resource.get("remote_dir", "")
            if resource.get("serial") != self.serial or not re.fullmatch(r"/data/local/tmp/vivo-background-[0-9a-f]{32}", directory):
                resource["cleanup_error"] = "Cleanup resource does not belong to this selected device or session"
                unresolved.append(resource)
                continue
            try:
                if self._adb(["get-state"], timeout=5, record=False).stdout.strip() != "device":
                    raise AdbError("Phone is not connected and authorized")
                device = self.device()
                if not device.get("ready") or (resource.get("identity") and resource["identity"] != device.get("identity")):
                    raise AdbError("The connected phone does not match the saved session identity")
                if resource.get("launched"):
                    owned = set()
                    helper_name = {"native": "uiautomator", "inventory": "phonepolicy.PackageInventory"}.get(resource.get("kind"))
                    acknowledged = resource.get("completed_ack") or (resource.get("kind") == "inventory" and resource.get("launch_finished"))
                    if not acknowledged and not resource.get("pid") and not resource.get("pidfile"):
                        lease_seconds = 35 if resource.get("kind") == "inventory" else 20
                        if resource.get("heartbeat"):
                            heartbeat = self._adb(["shell", "stat", "-c", "%Y", resource["heartbeat"]], timeout=5, check=False, record=False)
                            device_time = self._adb(["shell", "date", "+%s"], timeout=5, record=False)
                            if heartbeat.returncode == 0 and heartbeat.stdout.strip().isdigit() and device_time.stdout.strip().isdigit():
                                remaining_lease = max(0, lease_seconds - (int(device_time.stdout.strip()) - int(heartbeat.stdout.strip())))
                            else:
                                remaining_lease = lease_seconds
                        else:
                            remaining_lease = lease_seconds
                        time.sleep(min(lease_seconds, remaining_lease))
                    processes = self._adb(["shell", "ps", "-A", "-o", "PID,ARGS"], timeout=5, record=False).stdout
                    candidates = set()
                    for line in processes.splitlines():
                        parts = line.split(None, 1)
                        if len(parts) == 2 and parts[0].isdigit():
                            if directory in parts[1] or helper_name in parts[1].split():
                                candidates.add(int(parts[0]))
                    if resource.get("pid"):
                        candidates.add(int(resource["pid"]))
                    if resource.get("pidfile"):
                        saved_pid = self._adb(["shell", "cat", resource["pidfile"]], timeout=5, check=False, record=False)
                        if saved_pid.returncode == 0 and saved_pid.stdout.strip().isdigit():
                            candidates.add(int(saved_pid.stdout.strip()))
                    for pid in candidates:
                        if self._owns_session_process(resource, pid):
                            owned.add(pid)
                    for pid in owned:
                        self._adb(["shell", "kill", "-TERM", str(pid)], timeout=5, check=False, record=False)
                    for attempt in range(6):
                        active = set()
                        for pid in owned:
                            if self._owns_session_process(resource, pid):
                                active.add(pid)
                        if not active:
                            break
                        if attempt == 4:
                            for pid in active:
                                self._adb(["shell", "kill", "-KILL", str(pid)], timeout=5, check=False, record=False)
                        if attempt == 5:
                            raise AdbError("The exact owned phone helper has not yet stopped")
                        time.sleep(0.2)
                    processes = self._adb(["shell", "ps", "-A", "-o", "PID,ARGS"], timeout=5, record=False).stdout
                    if directory in processes:
                        raise AdbError("The session still has a phone process; cleanup remains pending")
                    if helper_name:
                        for line in processes.splitlines():
                            parts = line.split(None, 1)
                            if len(parts) == 2 and parts[0].isdigit() and helper_name in parts[1].split():
                                if self._owns_session_process(resource, int(parts[0])):
                                    raise AdbError("The session still has a phone process; cleanup remains pending")
                    resource["helper_stopped"] = True
                else:
                    resource["helper_stopped"] = True
                self._adb(["shell", "rm", "-rf", directory], timeout=5, record=False)
                token = directory.rsplit("/", 1)[1]
                self._adb(["shell", "find", "/data/local/tmp/dalvik-cache", "-type", "f", "-name", "*" + token + "*", "-delete"], timeout=5, check=False, record=False)
            except AdbError as error:
                resource["cleanup_error"] = str(error)
                unresolved.append(resource)
        if unresolved:
            by_directory = {item["remote_dir"]: item for item in self.pending_cleanup}
            by_directory.update({item.get("remote_dir", ""): item for item in unresolved})
            self.pending_cleanup = list(by_directory.values())
        else:
            cleared = {item.get("remote_dir") for item in resources}
            self.pending_cleanup = [item for item in self.pending_cleanup if item.get("remote_dir") not in cleared]
        return unresolved
