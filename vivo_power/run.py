import copy
import datetime
import threading
import time

from .state import INSTALLATION_FIELDS, SUCCESS
from .presets import DEFAULT_PRESET_ID


def recover_phone(store, engine, device):
    scan_key = f"{device['identity']}:{device['profile']}"
    scans = store.data.setdefault("scan_resources", {})
    runs = store.history(device)
    saved_directories = {resource["remote_dir"] for resource in scans.get(scan_key, [])}
    saved_directories.update(resource["remote_dir"] for run in runs for resource in run.get("resources", []))
    pending = [copy.deepcopy(resource) for resource in engine.pending_cleanup if resource["remote_dir"] not in saved_directories]
    if pending:
        scans.setdefault(scan_key, []).extend(pending)
        store.persist()
    if scans.get(scan_key):
        scans[scan_key] = engine.recover_cleanup(scans[scan_key])
        store.persist()
        if scans[scan_key]:
            raise RuntimeError("Previous inventory cleanup is unconfirmed. Reconnect this phone to remove its temporary files.")
    for run in runs:
        if run.get("resources"):
            run["resources"] = engine.recover_cleanup(run["resources"])
            run["phone_available"] = not run["resources"]
            store.save_run(run)
            if run["resources"]:
                raise RuntimeError("Previous phone automation could not be confirmed stopped. Reconnect this phone to finish its cleanup.")


class RunController:
    def __init__(self, store, engine, run, progress_callback=None, preset_id=DEFAULT_PRESET_ID):
        if run.get("preset", {}).get("id", DEFAULT_PRESET_ID) != preset_id:
            raise ValueError("This run belongs to another preset. Select its preset before running it.")
        self.store = store
        self.engine = engine
        self.run = run
        self.progress_callback = progress_callback
        self.pause_requested = threading.Event()
        self.stop_requested = threading.Event()
        self.engine.resource_callback = self.record_resource
        self.started = None

    def record_resource(self, resource):
        if resource is not None:
            resources = self.run["resources"]
            resources[:] = [item for item in resources if item["remote_dir"] != resource["remote_dir"]]
            resources.append(copy.deepcopy(resource))
        else:
            self.run["resources"] = copy.deepcopy(self.engine.pending_cleanup)
        self.store.save_run(self.run)

    def pause(self):
        self.pause_requested.set()

    def stop(self):
        self.stop_requested.set()

    def checkpoint(self):
        if self.run["status"] in {"running", "pausing", "stopping"}:
            if self.stop_requested.is_set():
                self.run["status"] = "stopping"
            elif self.pause_requested.is_set():
                self.run["status"] = "pausing"
        if self.started is not None:
            self.run["elapsed_seconds"] = round(time.monotonic() - self.started, 1)
        completed = 0
        total = len(self.run["packages"]) * len(self.run["settings"]) * 2
        for record in self.run["packages"].values():
            for setting in self.run["settings"]:
                result = record["results"][setting]
                if result.get("skip_retry") or record.get("missing"):
                    completed += 2
                else:
                    completed += int(result.get("status") not in {"pending", "not_checked"})
                    completed += int(bool(result.get("final_checked")))
        self.run["completed_operations"] = completed
        self.run["total_operations"] = total
        if completed >= 4 and self.run.get("elapsed_seconds", 0) > 0:
            self.run["estimated_remaining_seconds"] = round(self.run["elapsed_seconds"] / completed * (total - completed))
        self.run["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.store.save_run(self.run)
        if self.progress_callback:
            self.progress_callback(copy.deepcopy(self.run))

    def execute(self):
        run = self.run
        started = time.monotonic()
        self.started = started
        run["status"] = "running"
        run.pop("error", None)
        self.checkpoint()
        try:
            device = self.engine.device()
            if device["identity"] != run["device"]["identity"] or device["profile"] != run["profile"]:
                raise RuntimeError("The selected phone or profile changed. This run cannot continue.")
            if not device["ready"]:
                raise RuntimeError(device["reason"])
            recover_phone(self.store, self.engine, device)
            run["resources"] = []
            run["stage"] = "Checking selected installations before any changes"
            self.checkpoint()
            inventory = {app["package"]: app for app in self.engine.inventory()}
            if self.engine.pending_cleanup:
                run["resources"] = copy.deepcopy(self.engine.pending_cleanup)
                raise RuntimeError("Inventory cleanup is outstanding. Reconnect before continuing.")
            for package, record in run["packages"].items():
                if record.get("missing"):
                    continue
                current = inventory.get(package)
                if current is None or any(record["installation"].get(field) != current.get(field) for field in INSTALLATION_FIELDS):
                    raise RuntimeError(f"{package} was removed, updated, or reinstalled after the preview. Rescan and review before continuing.")
            if not self.pause_requested.is_set() and not self.stop_requested.is_set() and run["timeout"].get("status") not in {"verified", "already_configured", "unchanged"}:
                run["stage"] = "Device-wide unused-app timeout"
                self.checkpoint()
                run["timeout"] = self.engine.timeout(run["timeout_action"], verify_only=run["mode"] == "verify")
                self.checkpoint()
            if not self.pause_requested.is_set() and not self.stop_requested.is_set():
                for phase in ("apply", "verify"):
                    if phase == "apply" and run["phase"] == "verify":
                        continue
                    run["phase"] = phase
                    for package, record in run["packages"].items():
                        if record.get("missing"):
                            continue
                        for setting in run["settings"]:
                            if self.pause_requested.is_set() or self.stop_requested.is_set():
                                break
                            previous = record["results"][setting]
                            if previous.get("skip_retry"):
                                continue
                            if phase == "apply" and previous.get("status") in SUCCESS:
                                continue
                            if phase == "verify" and previous.get("final_checked"):
                                continue
                            run["stage"] = f"{'Checking' if phase == 'verify' or run['mode'] == 'verify' else 'Applying'} {setting}"
                            run["current"] = [package, setting]
                            run["elapsed_seconds"] = round(time.monotonic() - started, 1)
                            self.checkpoint()
                            try:
                                result = self.engine.perform(package, setting, verify_only=phase == "verify" or run["mode"] == "verify")
                            except (OSError, RuntimeError, TimeoutError) as error:
                                result = {"status": "failed", "error": str(error), "checked_at": None}
                            if phase == "verify":
                                if setting == "doze":
                                    for key in ("request", "request_at", "response", "request_returncode"):
                                        if key in previous:
                                            result[key] = previous[key]
                                    if previous.get("request") in {"rejected", "rejected_unknown_package", "failed", "error"}:
                                        result["status"] = "rejected" if previous["request"].startswith("rejected") else "failed"
                                result["initial_result"] = previous.get("initial_result", copy.deepcopy(previous))
                                result["final_checked"] = True
                                if result["status"] == "verified" and previous.get("status") == "already_configured":
                                    result["status"] = "already_configured"
                            record["results"][setting] = result
                            run.pop("current", None)
                            self.checkpoint()
                            if self.engine.pending_cleanup:
                                run["resources"] = copy.deepcopy(self.engine.pending_cleanup)
                                raise RuntimeError("Native navigation or cleanup is unconfirmed; phone availability has not been established.")
                            if result["status"] == "failed":
                                connected = self.engine.device()
                                if not connected["ready"] or connected["identity"] != run["device"]["identity"]:
                                    raise RuntimeError("Phone disconnected or became unavailable. Completed results are saved; reconnect and resume.")
                        if self.pause_requested.is_set() or self.stop_requested.is_set():
                            break
                    if self.pause_requested.is_set() or self.stop_requested.is_set():
                        break
                if not self.pause_requested.is_set() and not self.stop_requested.is_set():
                    before_timeout = copy.deepcopy(run["timeout"])
                    run["timeout"] = self.engine.timeout(run["timeout_action"], verify_only=True)
                    run["timeout"]["initial_result"] = before_timeout
                    if before_timeout.get("status") == "failed":
                        run["timeout"]["status"] = "failed"
                        run["timeout"]["error"] = before_timeout.get("error", "The requested timeout operation failed.")
                    if run["mode"] != "verify" and run["timeout_action"] == "set" and (
                        run["timeout"].get("effective_value") != "9223372036854775807" or run["timeout"].get("override_value") != "9223372036854775807"
                    ):
                        run["timeout"]["status"] = "failed"
                        run["timeout"]["error"] = "The requested timeout override changed before final readback."
                    if run["mode"] != "verify" and run["timeout_action"] == "clear" and run["timeout"].get("override_value") not in {None, "", "null"}:
                        run["timeout"]["status"] = "failed"
                        run["timeout"]["error"] = "The local timeout override remains present."
            if run["resources"]:
                run["status"] = "interrupted"
                run["error"] = "Phone navigation stop or cleanup is unconfirmed. Reconnect for recovery."
            elif self.stop_requested.is_set():
                run["status"] = "stopped"
            elif self.pause_requested.is_set():
                run["status"] = "paused"
            else:
                limitations = any(result.get("status") not in {"verified", "already_configured", "not_selected"}
                                  for record in run["packages"].values() for result in record["results"].values())
                limitations |= any(record["results"].get("doze", {}).get("request") not in {None, "accepted", "not_requested"}
                                   for record in run["packages"].values())
                limitations |= run["timeout"].get("status") not in {"verified", "already_configured", "unchanged"}
                run["status"] = "completed_with_limitations" if limitations else "completed"
                run["phase"] = "finished"
            run["phone_available"] = not run["resources"]
        except (OSError, RuntimeError, TimeoutError) as error:
            run["status"] = "interrupted"
            run["error"] = str(error)
            run["phone_available"] = False
            if run.get("current"):
                package, setting = run.pop("current")
                run["packages"][package]["results"][setting] = {"status": "not_checked", "error": str(error)}
        finally:
            if self.engine.pending_cleanup:
                run["resources"] = copy.deepcopy(self.engine.pending_cleanup)
                run["phone_available"] = False
            run["elapsed_seconds"] = round(time.monotonic() - started, 1)
            run["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.checkpoint()
        return run
