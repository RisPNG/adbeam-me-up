import copy
import datetime
import json
import os
from pathlib import Path
import tempfile
import uuid

from .branding import APP_NAME
from .presets import DEFAULT_PRESET_ID, DEFAULT_PRESET_NAME


SETTINGS = (
    "RUN_IN_BACKGROUND",
    "RUN_ANY_IN_BACKGROUND",
    "AUTO_REVOKE_PERMISSIONS_IF_UNUSED",
    "native",
    "doze",
)
SUCCESS = {"verified", "already_configured", "unavailable"}
INSTALLATION_FIELDS = ("version_code", "first_install_time", "last_update_time", "uid")


def select_apps(apps, mode, exclude_system=False, packages=None, latest_results=None):
    selected = []
    for app in apps:
        if exclude_system and app["system"]:
            continue
        if packages is not None and app["package"] not in packages:
            continue
        if mode == "new" and app.get("change") != "new":
            continue
        if mode == "changed" and app.get("change") not in {"new", "changed"}:
            continue
        if mode == "unfinished":
            results = (latest_results or {}).get(app["package"], {})
            if results and all(result.get("status") in SUCCESS for result in results.values()):
                continue
        selected.append(copy.deepcopy(app))
    return selected


def create_run(device, apps, settings, mode="all", timeout_action="unchanged", previous_results=None, recheck_unavailable=False,
               preset_id=DEFAULT_PRESET_ID, preset_name=DEFAULT_PRESET_NAME):
    apps = [{key: copy.deepcopy(value) for key, value in app.items() if key not in {"results", "evidence_run"}} for app in apps]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    run = {
        "schema_version": 3,
        "id": uuid.uuid4().hex,
        "preset": {"id": preset_id, "name": preset_name},
        "device": copy.deepcopy(device),
        "profile": 0,
        "apps": copy.deepcopy(apps),
        "settings": [setting for setting in SETTINGS if setting in settings],
        "mode": mode,
        "timeout_action": timeout_action,
        "timeout": {"status": "pending"},
        "status": "paused",
        "phase": "apply",
        "started_at": now,
        "updated_at": now,
        "finished_at": None,
        "resources": [],
        "packages": {
            app["package"]: {
                "installation": copy.deepcopy(app),
                "results": {
                    setting: {"status": "pending" if setting in settings else "not_selected"}
                    for setting in SETTINGS
                },
            }
            for app in apps
        },
        "observations": [],
    }
    if mode == "retry":
        for app in apps:
            record = run["packages"][app["package"]]
            for setting in run["settings"]:
                previous = (previous_results or {}).get(app["package"], {}).get(setting, {})
                same_installation = all(previous.get("installation", {}).get(field) == app.get(field) for field in INSTALLATION_FIELDS)
                if same_installation and previous.get("status") in SUCCESS and not (setting == "native" and recheck_unavailable and previous.get("status") == "unavailable"):
                    record["results"][setting] = copy.deepcopy(previous)
                    record["results"][setting]["skip_retry"] = True
    return run


def prepare_resume(run, device, inventory, preset_id=DEFAULT_PRESET_ID):
    if run.get("imported"):
        raise ValueError("Imported reports are historical observations and cannot be resumed.")
    if run.get("preset", {}).get("id", DEFAULT_PRESET_ID) != preset_id:
        raise ValueError("This run belongs to another preset. Select its preset before resuming.")
    if run["device"]["identity"] != device["identity"] or run["profile"] != device["profile"]:
        raise ValueError("This run belongs to another phone or profile.")
    installed = {app["package"]: {key: copy.deepcopy(value) for key, value in app.items() if key not in {"results", "evidence_run"}} for app in inventory}
    review = []
    configuration_changed = run["device"].get("configuration") != device.get("configuration")
    for package, record in run["packages"].items():
        current = installed.get(package)
        if current is None:
            review.append(f"{package}: removed; its unfinished work will be skipped.")
            if not record.get("missing"):
                run["observations"].append({"package": package, "installation": copy.deepcopy(record["installation"]), "results": copy.deepcopy(record["results"])})
            record["missing"] = True
            for setting in run["settings"]:
                record["results"][setting] = {"status": "not_checked", "error": "App removed since this run was selected."}
            continue
        record.pop("missing", None)
        old = record["installation"]
        if any(old.get(field) != current.get(field) for field in INSTALLATION_FIELDS):
            review.append(f"{package}: updated or reinstalled; selected settings will be checked again.")
            run["observations"].append({"package": package, "installation": copy.deepcopy(old), "results": copy.deepcopy(record["results"])})
            record["installation"] = copy.deepcopy(current)
            for setting in run["settings"]:
                record["results"][setting] = {"status": "pending"}
            run["phase"] = "apply"
        else:
            for setting in run["settings"]:
                result = record["results"][setting]
                if setting == "native" and configuration_changed:
                    review.append(f"{package}: phone firmware or native Settings changed; the native control will be rechecked.")
                    run["observations"].append({"package": package, "installation": copy.deepcopy(old), "device": copy.deepcopy(run["device"]), "results": {"native": copy.deepcopy(result)}})
                    record["results"][setting] = {"status": "pending"}
                    run["phase"] = "apply"
                    continue
                if result.get("status") not in SUCCESS:
                    result.pop("final_checked", None)
                    run["phase"] = "apply"
    run["device"] = copy.deepcopy(device)
    run["resume_review"] = review
    run["finished_at"] = None
    return review


class Store:
    def __init__(self, path=None):
        if path is None:
            if os.name == "nt":
                base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            else:
                base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
            path = base / "vivo-background-power" / "state.json"
        self.path = Path(path)
        self.data = {"schema_version": 3, "scans": {}, "runs": []}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("schema_version") != 3:
                raise ValueError("Unsupported saved-state version; preserve the file and use report import.")
            for run in self.data["runs"]:
                run.setdefault("preset", {"id": DEFAULT_PRESET_ID, "name": DEFAULT_PRESET_NAME})
                if run["status"] in {"running", "pausing", "stopping"}:
                    run["status"] = "interrupted"
                    run["error"] = "The application closed before this run completed. Reconnect and resume."
                    if run.get("current"):
                        package, setting = run["current"]
                        if package in run["packages"]:
                            run["packages"][package]["results"][setting] = {"status": "not_checked", "error": "Operation interrupted; readback required."}
                    run.pop("current", None)

    def persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                json.dump(self.data, output, indent=2, ensure_ascii=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def record_scan_resource(self, device, engine, resource):
        key = f"{device['identity']}:{device['profile']}"
        resources = self.data.setdefault("scan_resources", {}).setdefault(key, [])
        if resource is not None:
            resources[:] = [item for item in resources if item["remote_dir"] != resource["remote_dir"]]
            resources.append(copy.deepcopy(resource))
        else:
            resources[:] = copy.deepcopy(engine.pending_cleanup)
        self.persist()

    def save_scan(self, device, apps):
        key = f"{device['identity']}:{device['profile']}"
        previous = self.data["scans"].get(key)
        before = {app["package"]: app for app in previous["apps"]} if previous else {}
        classified = copy.deepcopy(apps)
        for app in classified:
            old = before.get(app["package"])
            app["change"] = "new" if old is None else (
                "changed" if any(old.get(field) != app.get(field) for field in INSTALLATION_FIELDS) else "same"
            )
        self.data["scans"][key] = {
            "device": copy.deepcopy(device),
            "scanned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "previous_scan": previous is not None,
            "apps": classified,
        }
        self.persist()
        return classified

    def save_run(self, run):
        snapshot = copy.deepcopy(run)
        snapshot.setdefault("preset", {"id": DEFAULT_PRESET_ID, "name": DEFAULT_PRESET_NAME})
        for index, saved in enumerate(self.data["runs"]):
            if saved["id"] == run["id"]:
                self.data["runs"][index] = snapshot
                break
        else:
            self.data["runs"].insert(0, snapshot)
        self.persist()

    def history(self, device=None, preset_id=None):
        runs = self.data["runs"]
        if device is not None:
            runs = [run for run in runs if run["device"].get("identity") == device["identity"] and run["profile"] == device["profile"]]
        if preset_id is not None:
            runs = [run for run in runs if run.get("preset", {}).get("id", DEFAULT_PRESET_ID) == preset_id]
        return copy.deepcopy(runs)

    def latest_results(self, device, preset_id=DEFAULT_PRESET_ID):
        latest = {}
        verification_times = {}
        scan = self.data["scans"].get(f"{device['identity']}:{device['profile']}")
        installed = {app["package"]: app for app in scan["apps"]} if scan else None
        for run in sorted(self.history(device, preset_id=preset_id), key=lambda item: item.get("started_at") or "", reverse=True):
            for package, record in run["packages"].items():
                if installed is not None:
                    current = installed.get(package)
                    if current is None or any(record["installation"].get(field) != current.get(field) for field in INSTALLATION_FIELDS):
                        continue
                for setting, result in record["results"].items():
                    if result.get("status") == "not_selected":
                        continue
                    if setting == "native" and run["device"].get("configuration") != device.get("configuration"):
                        continue
                    stamp = result.get("checked_at") or run.get("updated_at") or run.get("started_at") or ""
                    try:
                        observed_at = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        observed_at = 0
                    if observed_at <= verification_times.get((package, setting), -1):
                        continue
                    observation = copy.deepcopy(result)
                    observation["installation"] = copy.deepcopy(record["installation"])
                    latest.setdefault(package, {})[setting] = observation
                    verification_times[(package, setting)] = observed_at
        return latest

    def import_report(self, path):
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        version = report.get("schema_version", 1)
        if version == 3:
            if not all(key in report for key in ("device", "packages", "settings", "started_at")):
                raise ValueError("Report is missing required run fields.")
            run = copy.deepcopy(report)
            if "preset" in run:
                preset = run["preset"]
                if not isinstance(preset, dict) or any(not isinstance(preset.get(field), str) or not preset[field].strip() for field in ("id", "name")):
                    raise ValueError("Report preset must contain a nonempty id and name.")
            if not isinstance(run["packages"], dict):
                raise ValueError("Report packages must be an object.")
            for package, record in run["packages"].items():
                if not isinstance(record, dict) or not isinstance(record.get("results"), dict):
                    raise ValueError(f"Invalid results for {package}.")
                record.setdefault("installation", {"package": package, "label": package})
                if not isinstance(record["installation"], dict):
                    raise ValueError(f"Invalid installation metadata for {package}.")
                if any(not isinstance(result, dict) for result in record["results"].values()):
                    raise ValueError(f"Invalid setting observations for {package}.")
                for setting in SETTINGS:
                    record["results"].setdefault(setting, {"status": "not_checked"})
            run.setdefault("profile", 0)
            run.setdefault("status", "interrupted")
            run.setdefault("apps", [])
        elif version in {1, 2}:
            if not isinstance(report.get("packages"), dict) or not report.get("device_serial"):
                raise ValueError("Legacy report requires a device serial and package results.")
            run = create_run({"serial": report["device_serial"], "identity": None, "profile": report.get("profile", 0)}, [], [], "historical")
            run["started_at"] = report.get("started_at")
            run["finished_at"] = report.get("finished_at")
            run["status"] = "completed_with_limitations" if report.get("exit_code") in {0, 2} else "interrupted"
            run["timeout"] = report.get("unused_app_timeout", {"status": "not_checked"})
            for package, old in report["packages"].items():
                results = {setting: {"status": "not_checked"} for setting in SETTINGS}
                for setting in SETTINGS[:3]:
                    value = old.get("appops", {}).get(setting)
                    if value:
                        results[setting] = {
                            "status": "verified" if value.endswith("_verified") else "not_checked",
                            "observed": value,
                            "checked_at": None,
                            "reported_at": report.get("finished_at"),
                        }
                native = old.get("native", "pending")
                results["native"] = {
                    "status": "verified" if native == "allow_verified_after_reopen" else ("unavailable" if native.startswith("skipped_") else "not_checked"),
                    "observed": native,
                    "checked_at": None,
                    "reported_at": report.get("finished_at"),
                }
                doze = old.get("doze_whitelist")
                if doze:
                    membership = doze.get("retained_at_end")
                    results["doze"] = {
                        "status": "verified" if membership is True else ("not_present" if membership is False else "not_checked"),
                        "request": doze.get("request", "not_checked"),
                        "membership": membership,
                        "checked_at": report.get("doze_whitelist_readback", {}).get("checked_at"),
                        "response": doze.get("response"),
                    }
                run["packages"][package] = {"installation": {"package": package, "label": package}, "results": results}
        else:
            raise ValueError(f"Unsupported report schema: {version}")
        run["id"] = uuid.uuid4().hex
        run["schema_version"] = 3
        run.setdefault("preset", {"id": DEFAULT_PRESET_ID, "name": DEFAULT_PRESET_NAME})
        run["imported"] = True
        run["resources"] = []
        run["imported_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.save_run(run)
        return run

    def export_report(self, run, path, readable=False):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not readable:
            path.write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return
        device = run["device"]
        preset = run.get("preset", {"id": DEFAULT_PRESET_ID, "name": DEFAULT_PRESET_NAME})
        lines = [f"{APP_NAME} report", f"Preset: {preset['name']} ({preset['id']})",
                 f"Phone: {device.get('name', device.get('serial'))} / {device.get('serial')}",
                 f"Profile: {run['profile']} (main profile only)", f"Started: {run.get('started_at')}",
                 f"Finished: {run.get('finished_at')}", f"Outcome: {run['status']}",
                 f"Device-wide timeout: {json.dumps(run.get('timeout', {}), ensure_ascii=False)}", "",
                 "Results are observations at their verification times. Doze membership can change later.",
                 "Unavailable native controls are not verified Allow. Shared-UID changes can affect related apps.", ""]
        for package, record in run["packages"].items():
            lines.append(f"{record['installation'].get('label', package)} ({package})")
            for setting, result in record["results"].items():
                lines.append(f"  {setting}: {result.get('status', 'not_checked')}; checked {result.get('checked_at') or 'unknown'}")
                for field in ("request", "membership", "before", "observed", "error"):
                    if field in result:
                        lines.append(f"    {field}: {result[field]}")
            lines.append("")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
