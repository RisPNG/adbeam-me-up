import copy
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

from vivo_power.adb import AdbError
from vivo_power.run import RunController, recover_phone
from vivo_power.state import SETTINGS, Store, create_run, prepare_resume, select_apps


DEVICE = {"serial": "phone-one", "identity": "hardware-one", "profile": 0, "ready": True, "reason": "ready"}


def app(package, system=False, version=1, installed=100):
    return {"package": package, "label": package, "uid": 10001, "system": system,
            "version_code": version, "first_install_time": installed, "last_update_time": installed + version}


class FakeEngine:
    def __init__(self):
        self.pending_cleanup = []
        self.calls = []
        self.actions = []
        self.override = "1234"
        self.callback = None
        self.identity = DEVICE["identity"]
        self.fail = None
        self.cleanup_calls = []
        self.cleanup_fails = False
        self.inventory_calls = 0

    def device(self):
        return {**DEVICE, "identity": self.identity}

    def inventory(self):
        self.inventory_calls += 1
        return [app(package) for package in ("first", "second", "user", "old", "updated", "reinstalled")]

    def recover_cleanup(self, resources):
        self.cleanup_calls.append(copy.deepcopy(resources))
        self.pending_cleanup = copy.deepcopy(resources) if self.cleanup_fails else []
        return copy.deepcopy(self.pending_cleanup)

    def timeout(self, action, verify_only=False):
        self.actions.append((action, verify_only))
        if not verify_only and action == "set":
            self.override = "9223372036854775807"
        if not verify_only and action == "clear":
            self.override = "null"
        configured = self.override == "9223372036854775807" if action == "set" else self.override in {"", "null"}
        status = "unchanged" if action == "unchanged" else "verified" if configured else "not_present" if verify_only else "failed"
        return {"action": action, "status": status, "effective_value": self.override,
                "override_value": self.override, "checked_at": "now"}

    def perform(self, package, setting, verify_only=False):
        self.calls.append((package, setting, verify_only))
        if self.callback:
            self.callback(package, setting, verify_only)
        if setting == self.fail:
            return {"status": "failed", "error": "request failed"}
        if setting == "doze":
            return {"status": "not_present" if verify_only else "verified", "request": "not_requested" if verify_only else "accepted", "membership": not verify_only, "checked_at": "now"}
        return {"status": "verified", "observed": "allow", "checked_at": "now"}


class StateRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "state.json")
        self.engine = FakeEngine()

    def test_cli_initial_inventory_failure_keeps_a_durable_cleanup_checkpoint(self):
        resource = {"serial": DEVICE["serial"], "identity": DEVICE["identity"],
                    "remote_dir": "/data/local/tmp/vivo-background-" + "d" * 32,
                    "kind": "inventory", "launched": True}

        class FailedInventory(FakeEngine):
            def device(self):
                return {**DEVICE, "native_supported": True}

            def inventory(self):
                self.pending_cleanup.append(copy.deepcopy(resource))
                self.resource_callback(resource)
                raise AdbError("Inventory transport disconnected")

        engine = FailedInventory()
        entry = Path(__file__).resolve().parents[1] / "presets" / "vivo-background-power" / "allow-background-power.py"
        errors = io.StringIO()
        with patch("vivo_power.adb.AdbEngine", return_value=engine), patch.object(sys, "argv", [str(entry), "--state", str(self.store.path)]), patch.object(sys, "path", sys.path.copy()), redirect_stderr(errors):
            with self.assertRaises(SystemExit) as exited:
                runpy.run_path(str(entry), run_name="__main__")
        self.assertEqual(exited.exception.code, 2)
        self.assertIn("Inventory transport disconnected", errors.getvalue())
        saved = Store(self.store.path)
        self.assertEqual(saved.data["scan_resources"]["hardware-one:0"], [resource])
        self.assertEqual(saved.history(), [])
        self.assertEqual(engine.calls, [])
        self.assertEqual(engine.actions, [])

    def test_pause_and_stop_requests_remain_visible_until_the_terminal_checkpoint(self):
        for request, pending_status, final_status in (("pause", "pausing", "paused"), ("stop", "stopping", "stopped")):
            with self.subTest(request=request):
                engine = FakeEngine()
                run = create_run(DEVICE, [app("user")], ["native"])
                snapshots = []
                controller = RunController(self.store, engine, run, snapshots.append)
                engine.callback = lambda *_: getattr(controller, request)()
                controller.execute()
                self.assertIn(pending_status, [snapshot["status"] for snapshot in snapshots])
                self.assertEqual(snapshots[-1]["status"], final_status)
                self.assertTrue(run["phone_available"])

    def test_restart_marks_an_unfinished_stop_as_interrupted(self):
        run = create_run(DEVICE, [app("user")], ["native"])
        run["status"] = "stopping"
        run["current"] = ["user", "native"]
        self.store.save_run(run)
        restarted = Store(self.store.path)
        saved = restarted.history()[0]
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["packages"]["user"]["results"]["native"]["status"], "not_checked")
        self.assertNotIn("current", saved)

    def test_legacy_history_is_assigned_to_the_current_preset(self):
        legacy = create_run(DEVICE, [app("first")], ["native"])
        legacy.pop("preset")
        self.store.data["runs"].append(legacy)
        self.store.persist()
        restarted = Store(self.store.path)
        saved = restarted.history(DEVICE, preset_id="persist-v2413")[0]
        self.assertEqual(saved["preset"], {"id": "persist-v2413", "name": "Persist V2413"})
        prepare_resume(saved, DEVICE, [app("first")])
        self.assertEqual(restarted.history(DEVICE, preset_id="another-preset"), [])
        source = Path(self.temporary.name) / "legacy-report.json"
        source.write_text(json.dumps(legacy))
        imported = restarted.import_report(source)
        self.assertEqual(imported["preset"]["id"], "persist-v2413")

    def test_presets_keep_history_and_retry_evidence_separate(self):
        self.store.save_scan(DEVICE, [app("first")])
        original = create_run(DEVICE, [app("first")], ["native"])
        original["packages"]["first"]["results"]["native"] = {"status": "verified", "checked_at": "2026-09-30T09:30:00+00:00"}
        other = create_run(DEVICE, [app("first")], ["native"], preset_id="another-preset", preset_name="Another preset")
        other["packages"]["first"]["results"]["native"] = {"status": "failed", "checked_at": "2026-09-30T10:30:00+00:00"}
        other["resources"] = [{"remote_dir": "/data/local/tmp/vivo-background-other"}]
        self.store.save_run(original)
        self.store.save_run(other)
        self.assertEqual(self.store.latest_results(DEVICE)["first"]["native"]["status"], "verified")
        self.assertEqual(self.store.latest_results(DEVICE, "another-preset")["first"]["native"]["status"], "failed")
        self.assertEqual(len(self.store.history(DEVICE)), 2)
        self.assertEqual(len(self.store.history(DEVICE, "persist-v2413")), 1)
        self.assertEqual(self.store.history(DEVICE, "another-preset")[0]["resources"], other["resources"])
        retry = create_run(DEVICE, [app("first")], ["native"], mode="retry",
                           previous_results=self.store.latest_results(DEVICE, "another-preset"),
                           preset_id="another-preset", preset_name="Another preset")
        self.assertNotIn("skip_retry", retry["packages"]["first"]["results"]["native"])

    def test_foreign_preset_resume_and_execution_fail_before_device_work(self):
        run = create_run(DEVICE, [app("first")], ["native"], preset_id="another-preset", preset_name="Another preset")
        before = copy.deepcopy(run)
        with self.assertRaisesRegex(ValueError, "another preset"):
            prepare_resume(run, DEVICE, [app("first")])
        self.assertEqual(run, before)
        with self.assertRaisesRegex(ValueError, "another preset"):
            RunController(self.store, self.engine, run)
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.engine.actions, [])
        self.assertEqual(self.store.history(), [])
        prepare_resume(run, DEVICE, [app("first")], preset_id="another-preset")
        RunController(self.store, self.engine, run, preset_id="another-preset").execute()
        self.assertEqual(run["status"], "completed")

    def test_malformed_report_preset_is_rejected_without_changing_history(self):
        run = create_run(DEVICE, [app("first")], ["native"])
        path = Path(self.temporary.name) / "invalid-preset.json"
        for preset in (None, "persist-v2413", [], {}, {"id": "", "name": "Preset"}, {"id": "preset", "name": 1}):
            with self.subTest(preset=preset):
                run["preset"] = preset
                path.write_text(json.dumps(run))
                with self.assertRaisesRegex(ValueError, "Report preset"):
                    self.store.import_report(path)
                self.assertEqual(self.store.history(), [])

    def test_reports_preserve_preset_identity(self):
        run = create_run(DEVICE, [app("first")], ["native"], preset_id="another-preset", preset_name="Another preset")
        path = Path(self.temporary.name) / "preset-report.json"
        self.store.export_report(run, path)
        imported = self.store.import_report(path)
        self.assertEqual(imported["preset"], run["preset"])
        path = Path(self.temporary.name) / "preset-report.txt"
        self.store.export_report(run, path, readable=True)
        self.assertIn("ADBeam me up report", path.read_text())
        self.assertIn("Preset: Another preset (another-preset)", path.read_text())

    def test_five_new_apps_and_updates_reinstalls(self):
        existing = [app("old"), app("updated"), app("reinstalled")]
        self.store.save_scan(DEVICE, existing)
        current = [app("old"), app("updated", version=2), app("reinstalled", installed=200)] + [app(f"new{i}") for i in range(5)]
        scanned = self.store.save_scan(DEVICE, current)
        self.assertEqual(len(select_apps(scanned, "new")), 5)
        self.assertEqual(len(select_apps(scanned, "changed")), 7)
        self.assertEqual(Store(self.store.path).data["scans"]["hardware-one:0"]["apps"], scanned)

    def test_system_exclusion_and_fixed_selection(self):
        apps = [app("user"), app("system", system=True)]
        selected = select_apps(apps, "all", exclude_system=True)
        run = create_run(DEVICE, selected, ["native"])
        apps.append(app("later"))
        self.assertEqual(list(run["packages"]), ["user"])
        RunController(self.store, self.engine, run).execute()
        self.assertTrue(all(call[0] == "user" for call in self.engine.calls))

    def test_each_setting_independent_and_verify_only(self):
        for setting in SETTINGS:
            with self.subTest(setting=setting):
                self.engine.calls.clear()
                run = create_run(DEVICE, [app("user")], [setting], mode="verify", timeout_action="set")
                RunController(self.store, self.engine, run).execute()
                self.assertTrue(all(call[1] == setting and call[2] for call in self.engine.calls))
                self.assertEqual(self.engine.override, "1234")
                self.assertTrue(all(result["status"] == "not_selected" for key, result in run["packages"]["user"]["results"].items() if key != setting))

    def test_global_only_and_preserve_override(self):
        run = create_run(DEVICE, [], [], timeout_action="unchanged")
        self.assertEqual(RunController(self.store, self.engine, run).execute()["status"], "completed")
        self.assertEqual(self.engine.override, "1234")
        run = create_run(DEVICE, [], [], timeout_action="set")
        self.assertEqual(RunController(self.store, self.engine, run).execute()["status"], "completed")
        self.assertEqual(self.engine.override, "9223372036854775807")
        run = create_run(DEVICE, [], [], timeout_action="clear")
        RunController(self.store, self.engine, run).execute()
        self.assertEqual(self.engine.override, "null")
        self.assertFalse(self.engine.calls)

    def test_timeout_verification_retains_requested_set_and_clear_mismatches(self):
        for action, override in (("set", "1234"), ("clear", "9223372036854775807")):
            with self.subTest(action=action):
                self.engine.override = override
                self.engine.actions.clear()
                run = create_run(DEVICE, [], [], mode="verify", timeout_action=action)
                RunController(self.store, self.engine, run).execute()
                self.assertEqual(run["status"], "completed_with_limitations")
                self.assertEqual(run["timeout"]["action"], action)
                self.assertEqual(run["timeout"]["status"], "not_present")
                self.assertEqual(run["timeout"]["initial_result"]["status"], "not_present")
                self.assertEqual(self.engine.override, override)
                self.assertEqual(self.engine.actions, [(action, True), (action, True)])

    def test_timeout_verification_preserves_configured_set_and_clear_results(self):
        for action, override in (("set", "9223372036854775807"), ("clear", "null")):
            with self.subTest(action=action):
                self.engine.override = override
                self.engine.actions.clear()
                run = create_run(DEVICE, [], [], mode="verify", timeout_action=action)
                RunController(self.store, self.engine, run).execute()
                self.assertEqual(run["status"], "completed")
                self.assertEqual(run["timeout"]["action"], action)
                self.assertEqual(run["timeout"]["status"], "verified")
                self.assertEqual(self.engine.override, override)
                self.assertEqual(self.engine.actions, [(action, True), (action, True)])

    def test_timeout_verification_leaves_existing_override_unchanged(self):
        run = create_run(DEVICE, [], [], mode="verify")
        RunController(self.store, self.engine, run).execute()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["timeout"]["action"], "unchanged")
        self.assertEqual(run["timeout"]["status"], "unchanged")
        self.assertEqual(self.engine.override, "1234")
        self.assertEqual(self.engine.actions, [("unchanged", True), ("unchanged", True)])

    def test_timeout_apply_detects_override_changes_at_final_readback(self):
        for action, changed in (("set", "1234"), ("clear", "9223372036854775807")):
            with self.subTest(action=action):
                self.engine.actions.clear()
                self.engine.callback = lambda *_: setattr(self.engine, "override", changed)
                run = create_run(DEVICE, [app("first")], ["native"], timeout_action=action)
                RunController(self.store, self.engine, run).execute()
                self.assertEqual(run["status"], "completed_with_limitations")
                self.assertEqual(run["timeout"]["status"], "failed")
                self.assertEqual(run["timeout"]["initial_result"]["status"], "verified")
                self.assertEqual(self.engine.actions, [(action, False), (action, True)])

    def test_timeout_initial_failure_survives_configured_final_readback(self):
        original_timeout = self.engine.timeout
        def timeout(action, verify_only=False):
            result = original_timeout(action, verify_only)
            if not verify_only:
                result.update(status="failed", error="The timeout command failed")
            return result
        self.engine.timeout = timeout
        run = create_run(DEVICE, [], [], timeout_action="set")
        RunController(self.store, self.engine, run).execute()
        self.assertEqual(run["status"], "completed_with_limitations")
        self.assertEqual(run["timeout"]["status"], "failed")
        self.assertEqual(run["timeout"]["error"], "The timeout command failed")
        self.assertEqual(run["timeout"]["effective_value"], "9223372036854775807")

    def test_pause_restart_resume_skips_completed_mutations(self):
        run = create_run(DEVICE, [app("first"), app("second")], ["native"])
        controller = RunController(self.store, self.engine, run)
        self.engine.callback = lambda *_: controller.pause()
        controller.execute()
        self.assertEqual(run["status"], "paused")
        self.assertTrue(run["phone_available"])
        self.assertEqual(run["packages"]["first"]["results"]["native"]["status"], "verified")
        restarted = Store(self.store.path)
        run = restarted.history(DEVICE)[0]
        prepare_resume(run, DEVICE, [app("first"), app("second"), app("new")])
        self.engine.calls.clear()
        self.engine.callback = None
        RunController(restarted, self.engine, run).execute()
        self.assertNotIn(("first", "native", False), self.engine.calls)
        self.assertIn(("second", "native", False), self.engine.calls)
        self.assertTrue(all(call[0] != "new" for call in self.engine.calls))

    def test_stop_retains_completed_changes(self):
        run = create_run(DEVICE, [app("first"), app("second")], ["native"])
        controller = RunController(self.store, self.engine, run)
        self.engine.callback = lambda *_: controller.stop()
        controller.execute()
        self.assertEqual(run["status"], "stopped")
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(run["packages"]["first"]["results"]["native"]["status"], "verified")

    def test_native_pending_cleanup_never_declares_available(self):
        run = create_run(DEVICE, [app("first")], ["native"])
        def lose_connection(*_):
            self.engine.pending_cleanup = [{"remote_dir": "owned", "serial": DEVICE["serial"]}]
        self.engine.callback = lose_connection
        RunController(self.store, self.engine, run).execute()
        self.assertEqual(run["status"], "interrupted")
        self.assertFalse(run["phone_available"])
        self.assertTrue(run["resources"])

    def test_new_run_recovers_pending_history_across_presets_before_changes(self):
        resource = {"remote_dir": "/data/local/tmp/vivo-background-" + "a" * 32,
                    "serial": DEVICE["serial"], "identity": DEVICE["identity"], "kind": "native"}
        previous = create_run(DEVICE, [app("first")], ["native"], preset_id="another-preset", preset_name="Another preset")
        previous.update(status="interrupted", resources=[resource], phone_available=False)
        self.store.save_run(previous)
        self.engine.cleanup_fails = True
        run = create_run(DEVICE, [app("first")], ["RUN_IN_BACKGROUND"], timeout_action="set")
        RunController(self.store, self.engine, run).execute()
        self.assertEqual(run["status"], "interrupted")
        self.assertIn("Previous phone automation", run["error"])
        self.assertFalse(run["phone_available"])
        self.assertEqual(self.engine.inventory_calls, 0)
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.engine.actions, [])
        saved = Store(self.store.path).history(DEVICE, preset_id="another-preset")[0]
        self.assertEqual(saved["resources"], [resource])
        self.assertFalse(saved["phone_available"])
        self.engine.cleanup_fails = False
        RunController(self.store, self.engine, run).execute()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["resources"], [])
        self.assertTrue(run["phone_available"])
        self.assertEqual(self.store.history(DEVICE, preset_id="another-preset")[0]["resources"], [])

    def test_pending_scan_cleanup_blocks_new_run_until_recovery(self):
        resource = {"remote_dir": "/data/local/tmp/vivo-background-" + "b" * 32,
                    "serial": DEVICE["serial"], "identity": DEVICE["identity"], "kind": "inventory"}
        scan_key = f"{DEVICE['identity']}:{DEVICE['profile']}"
        self.store.data["scan_resources"] = {scan_key: [resource]}
        self.store.persist()
        self.engine.cleanup_fails = True
        run = create_run(DEVICE, [app("first")], ["native"], timeout_action="set")
        RunController(self.store, self.engine, run).execute()
        self.assertEqual(run["status"], "interrupted")
        self.assertIn("Previous inventory cleanup", run["error"])
        self.assertEqual(self.engine.inventory_calls, 0)
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.engine.actions, [])
        self.assertEqual(Store(self.store.path).data["scan_resources"][scan_key], [resource])
        self.engine.cleanup_fails = False
        RunController(self.store, self.engine, run).execute()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(self.store.data["scan_resources"][scan_key], [])

    def test_recovery_journals_unsaved_engine_resources_before_blocking(self):
        resource = {"remote_dir": "/data/local/tmp/vivo-background-" + "c" * 32,
                    "serial": DEVICE["serial"], "identity": DEVICE["identity"], "kind": "inventory"}
        self.engine.pending_cleanup = [resource]
        self.engine.cleanup_fails = True
        with self.assertRaisesRegex(RuntimeError, "Previous inventory cleanup"):
            recover_phone(self.store, self.engine, DEVICE)
        scan_key = f"{DEVICE['identity']}:{DEVICE['profile']}"
        self.assertEqual(Store(self.store.path).data["scan_resources"][scan_key], [resource])
        self.assertEqual(self.engine.inventory_calls, 0)
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.engine.actions, [])

    def test_recovery_syncs_current_run_resources_after_saved_checkpoint(self):
        resource = {"remote_dir": "/data/local/tmp/vivo-background-" + "d" * 32,
                    "serial": DEVICE["serial"], "identity": DEVICE["identity"], "kind": "native"}
        run = create_run(DEVICE, [app("first")], ["native"])
        run["resources"] = [resource]
        RunController(self.store, self.engine, run).execute()
        self.assertEqual(self.engine.cleanup_calls, [[resource]])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["resources"], [])
        self.assertTrue(run["phone_available"])
        self.assertEqual(Store(self.store.path).history(DEVICE)[0]["resources"], [])

    def test_recovery_preserves_resources_for_other_phones_and_profiles(self):
        for identity, profile in (("another-phone", 0), (DEVICE["identity"], 10)):
            foreign = {**DEVICE, "identity": identity, "profile": profile}
            resource = {"remote_dir": "/data/local/tmp/vivo-background-" + ("e" if profile == 0 else "f") * 32,
                        "serial": foreign["serial"], "identity": identity, "kind": "native"}
            run = create_run(foreign, [app("first")], ["native"])
            run.update(profile=profile, resources=[resource], phone_available=False)
            self.store.save_run(run)
            self.store.data.setdefault("scan_resources", {})[f"{identity}:{profile}"] = [resource]
        self.store.persist()
        before = copy.deepcopy(self.store.data)
        recover_phone(self.store, self.engine, DEVICE)
        self.assertEqual(self.engine.cleanup_calls, [])
        self.assertEqual(self.store.data, before)

    def test_other_phone_execution_stops_before_resource_recovery(self):
        resource = {"remote_dir": "/data/local/tmp/vivo-background-" + "a" * 32,
                    "serial": DEVICE["serial"], "identity": DEVICE["identity"], "kind": "native"}
        run = create_run(DEVICE, [app("first")], ["native"])
        run["resources"] = [resource]
        self.engine.identity = "another-phone"
        RunController(self.store, self.engine, run).execute()
        self.assertEqual(run["status"], "interrupted")
        self.assertIn("selected phone or profile changed", run["error"])
        self.assertEqual(self.engine.cleanup_calls, [])
        self.assertEqual(self.engine.inventory_calls, 0)
        self.assertEqual(run["resources"], [resource])

    def test_changed_and_removed_apps_review_and_other_device_refused(self):
        run = create_run(DEVICE, [app("updated"), app("removed")], ["native"])
        run["packages"]["updated"]["results"]["native"] = {"status": "verified"}
        with self.assertRaises(ValueError):
            prepare_resume(run, {**DEVICE, "identity": "another-phone"}, [])
        review = prepare_resume(run, DEVICE, [app("updated", version=2), app("unselected")])
        self.assertEqual(len(review), 2)
        self.assertEqual(run["packages"]["updated"]["results"]["native"]["status"], "pending")
        self.assertTrue(run["packages"]["removed"]["missing"])
        self.assertNotIn("unselected", run["packages"])

    def test_crash_retains_results_and_marks_inflight_unverified(self):
        run = create_run(DEVICE, [app("first")], ["native", "RUN_IN_BACKGROUND"])
        run["status"] = "running"
        run["current"] = ["first", "native"]
        run["packages"]["first"]["results"]["RUN_IN_BACKGROUND"] = {"status": "verified", "checked_at": "before"}
        self.store.save_run(run)
        recovered = Store(self.store.path).history(DEVICE)[0]
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["packages"]["first"]["results"]["native"]["status"], "not_checked")
        self.assertEqual(recovered["packages"]["first"]["results"]["RUN_IN_BACKGROUND"]["checked_at"], "before")

    def test_doze_request_separate_from_final_membership_and_other_success(self):
        run = create_run(DEVICE, [app("first")], ["doze", "RUN_IN_BACKGROUND"])
        RunController(self.store, self.engine, run).execute()
        results = run["packages"]["first"]["results"]
        self.assertEqual(results["doze"]["request"], "accepted")
        self.assertFalse(results["doze"]["membership"])
        self.assertEqual(results["doze"]["status"], "not_present")
        self.assertEqual(results["RUN_IN_BACKGROUND"]["status"], "verified")
        self.assertEqual(run["status"], "completed_with_limitations")

    def test_retry_only_failed_settings_and_explicit_unavailable_recheck(self):
        run = create_run(DEVICE, [app("first")], ["native", "RUN_IN_BACKGROUND"])
        run["packages"]["first"]["results"]["native"] = {"status": "unavailable", "checked_at": "before"}
        run["packages"]["first"]["results"]["RUN_IN_BACKGROUND"] = {"status": "failed"}
        self.store.save_run(run)
        latest = self.store.latest_results(DEVICE)
        retry = create_run(DEVICE, [app("first")], ["native", "RUN_IN_BACKGROUND"], mode="retry", previous_results=latest)
        RunController(self.store, self.engine, retry).execute()
        self.assertTrue(all(call[1] == "RUN_IN_BACKGROUND" for call in self.engine.calls))
        self.engine.calls.clear()
        retry = create_run(DEVICE, [app("first")], ["native"], mode="retry", previous_results=latest, recheck_unavailable=True)
        RunController(self.store, self.engine, retry).execute()
        self.assertTrue(self.engine.calls)

    def test_legacy_unknown_dates_and_reports_historical(self):
        legacy = {"schema_version": 1, "device_serial": "old", "started_at": "2020", "finished_at": "2021", "exit_code": 0,
                  "packages": {"first": {"appops": {"RUN_IN_BACKGROUND": "allow_verified"}, "native": "skipped_no_native_control"}}}
        path = Path(self.temporary.name) / "legacy.json"
        path.write_text(json.dumps(legacy))
        imported = self.store.import_report(path)
        results = imported["packages"]["first"]["results"]
        self.assertEqual(results["native"]["status"], "unavailable")
        self.assertEqual(results["doze"]["status"], "not_checked")
        self.assertIsNone(results["RUN_IN_BACKGROUND"]["checked_at"])
        self.assertEqual(imported["started_at"], "2020")
        with self.assertRaises(ValueError):
            prepare_resume(imported, DEVICE, [])
        exported = Path(self.temporary.name) / "report.json"
        self.store.export_report(imported, exported)
        self.assertEqual(json.loads(exported.read_text())["started_at"], "2020")
        readable = Path(self.temporary.name) / "report.txt"
        self.store.export_report(imported, readable, readable=True)
        self.assertIn("not_checked", readable.read_text())

    def test_changed_installations_do_not_inherit_historical_success(self):
        self.store.save_scan(DEVICE, [app("first")])
        run = create_run(DEVICE, [app("first")], ["native"])
        run["packages"]["first"]["results"]["native"] = {"status": "verified", "checked_at": "before"}
        self.store.save_run(run)
        self.assertIn("native", self.store.latest_results(DEVICE)["first"])
        self.store.save_scan(DEVICE, [app("first", version=2)])
        self.assertNotIn("first", self.store.latest_results(DEVICE))

    def test_unconfirmed_doze_request_never_claims_full_success(self):
        def unconfirmed(package, setting, verify_only=False):
            return {"status": "verified", "request": "not_requested" if verify_only else "unconfirmed", "membership": True, "checked_at": "now"}
        self.engine.perform = unconfirmed
        run = create_run(DEVICE, [app("first")], ["doze"])
        RunController(self.store, self.engine, run).execute()
        self.assertEqual(run["status"], "completed_with_limitations")

    def test_installation_changed_after_preview_stops_before_writes(self):
        run = create_run(DEVICE, [app("first", version=2)], ["native"], timeout_action="set")
        RunController(self.store, self.engine, run).execute()
        self.assertEqual(run["status"], "interrupted")
        self.assertIn("after the preview", run["error"])
        self.assertFalse(self.engine.calls)
        self.assertFalse(self.engine.actions)

    def test_firmware_change_rechecks_native_and_preserves_history(self):
        run = create_run({**DEVICE, "configuration": "before"}, [app("first")], ["native"])
        run["packages"]["first"]["results"]["native"] = {"status": "unavailable", "final_checked": True}
        review = prepare_resume(run, {**DEVICE, "configuration": "after"}, [app("first")])
        self.assertTrue(review)
        self.assertEqual(run["packages"]["first"]["results"]["native"]["status"], "pending")
        self.assertEqual(run["observations"][0]["results"]["native"]["status"], "unavailable")

    def test_stop_during_preflight_does_not_start_timeout_write(self):
        run = create_run(DEVICE, [], [], timeout_action="set")
        controller = RunController(self.store, self.engine, run)
        def inventory():
            controller.stop()
            return []
        self.engine.inventory = inventory
        controller.execute()
        self.assertEqual(run["status"], "stopped")
        self.assertFalse(self.engine.actions)

    def test_unavailable_recheck_preserves_verified_siblings(self):
        run = create_run(DEVICE, [app("first"), app("second")], ["native"])
        run["packages"]["first"]["results"]["native"] = {"status": "unavailable"}
        run["packages"]["second"]["results"]["native"] = {"status": "verified"}
        self.store.save_run(run)
        retry = create_run(DEVICE, [app("first"), app("second")], ["native"], mode="retry", previous_results=self.store.latest_results(DEVICE), recheck_unavailable=True)
        RunController(self.store, self.engine, retry).execute()
        self.assertTrue(all(call[0] == "first" for call in self.engine.calls))

    def test_latest_observation_uses_verification_time_after_resume(self):
        older = create_run(DEVICE, [app("first")], ["native"])
        older["started_at"] = "2026-09-20T00:00:00+00:00"
        older["packages"]["first"]["results"]["native"] = {"status": "verified", "checked_at": "2026-09-30T00:00:00+00:00"}
        newer = create_run(DEVICE, [app("first")], ["native"])
        newer["started_at"] = "2026-09-25T00:00:00+00:00"
        newer["packages"]["first"]["results"]["native"] = {"status": "not_present", "checked_at": "2026-09-25T00:00:00+00:00"}
        self.store.save_run(older)
        self.store.save_run(newer)
        self.assertEqual(self.store.latest_results(DEVICE)["first"]["native"]["status"], "verified")

    def test_installation_snapshots_exclude_presented_history(self):
        presented = {**app("first"), "results": {"native": {"status": "verified", "installation": app("first")}}, "evidence_run": "old"}
        run = create_run(DEVICE, [presented], ["native"])
        self.assertNotIn("results", run["apps"][0])
        self.assertNotIn("evidence_run", run["packages"]["first"]["installation"])
        updated = {**presented, "version_code": 2}
        prepare_resume(run, DEVICE, [updated])
        self.assertNotIn("results", run["packages"]["first"]["installation"])


if __name__ == "__main__":
    unittest.main()
