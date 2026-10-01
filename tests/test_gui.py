import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QDialogButtonBox, QLineEdit, QPlainTextEdit

from vivo_power.gui import AppFilter, AppModel, MainWindow, WorkThread
from vivo_power.state import Store, create_run


DEVICE = {"serial": "test-phone", "identity": "test-hardware", "profile": 0,
          "name": "vivo X200 Pro", "model": "V2413", "android": "16",
          "origin_os": "OriginOS 6", "locale": "en-US", "configuration": "build:power:en-US",
          "ready": True, "reason": "Main profile", "native_supported": True,
          "native_reason": "Supported test configuration"}
APPS = [
    {"package": "com.example.notes", "label": "Notes", "system": False, "uid": 10001,
     "version_code": 1, "first_install_time": 10, "last_update_time": 20, "change": "new"},
    {"package": "com.example.system", "label": "System utility", "system": True, "uid": 10002,
     "version_code": 2, "first_install_time": 11, "last_update_time": 21, "change": "changed"},
]


class FakeEngine:
    calls = []
    hold = None
    entered = None

    def __init__(self, adb_path=None, serial=None, resource_callback=None):
        self.serial = serial
        self.resource_callback = resource_callback
        self.pending_cleanup = []

    def devices(self):
        return [{"serial": "test-phone", "state": "device", "model": "vivo X200 Pro"}]

    def device(self):
        return copy.deepcopy(DEVICE)

    def inventory(self):
        return copy.deepcopy(APPS)

    def timeout(self, action="unchanged", verify_only=False):
        self.calls.append(("timeout", action, verify_only))
        return {"status": "unchanged", "effective_value": "1000", "override_value": "null"}

    def perform(self, package, setting, verify_only=False):
        self.calls.append((package, setting, verify_only))
        if self.entered:
            self.entered.set()
        if self.hold:
            self.hold.wait(2)
        result = {"status": "verified", "checked_at": "2026-09-30T09:30:00+00:00",
                  "observed": "allow", "commands": [{"argv": ["adb", "mock"], "returncode": 0}]}
        if setting == "doze":
            result.update(request="accepted", membership=True)
        return result

    def recover_cleanup(self, resources):
        self.calls.append(("recover", copy.deepcopy(resources)))
        return []


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setOrganizationName("VivoGuiTests")
        cls.application.setApplicationName("IsolatedTests")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, self.directory.name)
        QSettings().clear()
        self.store = Store(Path(self.directory.name) / "state.json")
        self.window = MainWindow(self.store, FakeEngine)
        FakeEngine.calls = []
        FakeEngine.hold = None
        FakeEngine.entered = None

    def tearDown(self):
        if FakeEngine.hold:
            FakeEngine.hold.set()
        if self.window.worker:
            self.window.worker.wait(3000)
            self.application.processEvents()
        self.window.close()
        self.window.deleteLater()
        self.application.processEvents()
        self.directory.cleanup()

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), "GUI operation did not complete")

    def scan(self):
        self.window.detect_devices()
        self.wait_for(lambda: not self.window.busy)
        self.window.scan_inventory()
        self.wait_for(lambda: not self.window.busy)

    def test_model_selection_includes_system_and_freezes_for_running_queue(self):
        model = AppModel()
        model.observe_inventory(APPS)
        self.assertEqual(model.selected, {app["package"] for app in APPS})
        model.setData(model.index(0, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        self.assertEqual(model.selected, {"com.example.system"})
        model.frozen = True
        self.assertFalse(model.setData(model.index(1, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole))
        self.assertEqual(model.selected, {"com.example.system"})

    def test_filter_combines_search_installation_and_system_scope(self):
        model = AppModel()
        model.observe_inventory(APPS)
        proxy = AppFilter()
        proxy.setSourceModel(model)
        proxy.choose_filter("", "all", False)
        self.assertEqual(proxy.rowCount(), 2)
        proxy.choose_filter("", "all", True)
        self.assertEqual(proxy.rowCount(), 1)
        proxy.choose_filter("system", "changed", False)
        self.assertEqual(proxy.rowCount(), 1)
        proxy.choose_filter("system", "new", False)
        self.assertEqual(proxy.rowCount(), 0)

    def test_history_and_changed_installation_do_not_replace_live_evidence(self):
        self.window.model.observe_inventory(APPS)
        saved = create_run(DEVICE, APPS, ["native"])
        saved["packages"]["com.example.notes"]["results"]["native"] = {"status": "verified", "checked_at": "2026-09-29T01:00:00+00:00"}
        self.store.save_run(saved)
        self.window.reload_history()
        self.assertNotIn("results", self.window.model.apps[0])
        self.assertEqual(self.window.result_model.apps[0]["results"]["native"]["status"], "verified")
        saved["packages"]["com.example.notes"]["installation"]["last_update_time"] = 99
        self.window.model.observe_progress(saved)
        self.assertNotIn("results", self.window.model.apps[0])

    def test_legacy_unknown_verification_time_displays_without_crashing(self):
        model = AppModel()
        apps = copy.deepcopy(APPS)
        apps[0]["results"] = {"native": {"status": "verified", "checked_at": None}}
        model.observe_inventory(apps)
        self.assertEqual(model.data(model.index(0, 11)), "Not checked")

    def test_scan_defaults_readiness_and_timeout_are_read_only(self):
        self.scan()
        self.assertFalse(self.window.exclude.isChecked())
        self.assertEqual(self.window.preset_options.timeout.currentData(), "unchanged")
        self.assertEqual(len(self.window.model.selected), 2)
        self.assertTrue(self.window.preset_options.native.isChecked())
        self.assertFalse(self.window.preset_options.start.isEnabled())
        self.window.preset_options.phone_idle.setChecked(True)
        self.assertTrue(self.window.preset_options.start.isEnabled())
        self.assertFalse(self.window.pause.isEnabled())
        self.assertFalse(self.window.stop.isEnabled())
        self.assertIn(("timeout", "unchanged", True), FakeEngine.calls)
        self.assertIn("1000", self.window.preset_options.timeout_state.text())

    def test_unsupported_native_does_not_block_android_policies(self):
        self.window.devices.addItem("test-phone", "test-phone")
        self.window.device = {**DEVICE, "native_supported": False}
        self.window.model.observe_inventory(APPS)
        self.window.preset_options.native.setChecked(False)
        self.window.update_controls()
        self.assertFalse(self.window.preset_options.native.isEnabled())
        self.assertTrue(self.window.preset_options.start.isEnabled())

    def test_global_timeout_can_run_without_selected_apps(self):
        self.scan()
        self.window.clear_selection()
        self.window.preset_options.timeout.setCurrentIndex(self.window.preset_options.timeout.findData("set"))
        self.assertTrue(self.window.preset_options.native.isChecked())
        self.assertFalse(self.window.preset_options.phone_idle.isChecked())
        self.assertTrue(self.window.preset_options.start.isEnabled())
        self.window.preset_options.timeout.setCurrentIndex(self.window.preset_options.timeout.findData("unchanged"))
        self.window.preset_options.native.setChecked(False)
        for box in self.window.preset_options.policy_boxes:
            box.setChecked(False)
        self.assertFalse(self.window.preset_options.start.isEnabled())
        self.window.preset_options.timeout.setCurrentIndex(self.window.preset_options.timeout.findData("clear"))
        self.assertTrue(self.window.preset_options.start.isEnabled())
        run = create_run(DEVICE, [], [], timeout_action="clear")
        for status in ("running", "pausing", "stopping"):
            with self.subTest(status=status):
                run["status"] = status
                self.window.observe_progress(run)
                self.assertEqual(self.window.progress.value(), 0)
        run["status"] = "stopped"
        run["phone_available"] = True
        self.window.observe_progress(run)
        self.assertEqual(self.window.progress.value(), 1)

    def test_preview_enumerates_checked_apps_hidden_by_search(self):
        self.scan()
        self.window.preset_options.native.setChecked(False)
        self.window.search.setText("Notes")
        self.assertIn("1 hidden", self.window.selection.text())
        contents = []

        def inspect_preview():
            dialog = self.application.activeModalWidget()
            self.assertIsInstance(dialog, QDialog)
            contents.append(dialog.findChild(QPlainTextEdit).toPlainText())
            dialog.reject()

        QTimer.singleShot(0, inspect_preview)
        self.window.preview_run()
        self.assertIn("com.example.notes", contents[0])
        self.assertIn("com.example.system", contents[0])
        self.assertFalse(self.window.busy)

    def test_end_to_end_reviewed_run_uses_frozen_queue_and_separate_results(self):
        self.scan()
        self.window.preset_options.native.setChecked(False)
        self.window.exclude.setChecked(True)

        def accept_preview():
            dialog = self.application.activeModalWidget()
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

        QTimer.singleShot(0, accept_preview)
        self.window.preview_run()
        self.wait_for(lambda: not self.window.busy)
        self.assertEqual(set(self.window.current_run["packages"]), {"com.example.notes"})
        self.assertEqual(self.window.current_run["status"], "completed")
        self.assertEqual(self.window.result_model.apps[0]["results"]["doze"]["request"], "accepted")
        self.assertEqual(self.window.current_run["timeout_action"], "unchanged")
        self.assertFalse(self.window.pause.isEnabled())
        self.assertIn("Phone available", self.window.activity.text())

    def test_completed_and_paused_runs_require_a_fresh_idle_phone_confirmation(self):
        self.scan()
        for status in ("completed", "paused"):
            with self.subTest(status=status):
                self.window.preset_options.phone_idle.setChecked(True)
                if status == "paused":
                    FakeEngine.hold = threading.Event()
                    FakeEngine.entered = threading.Event()
                run = create_run(DEVICE, APPS[:1], ["native"])
                worker = WorkThread("run", FakeEngine(serial=DEVICE["serial"]), self.store, run, self.window)
                self.window.begin_work(worker)
                if status == "paused":
                    self.wait_for(FakeEngine.entered.is_set)
                    self.window.pause_run()
                    FakeEngine.hold.set()
                self.wait_for(lambda: not self.window.busy)
                self.assertEqual(self.window.current_run["status"], status)
                self.assertTrue(self.window.current_run["phone_available"])
                self.assertIsNotNone(self.window.device)
                self.assertFalse(self.window.preset_options.phone_idle.isChecked())
                self.assertFalse(self.window.preset_options.start.isEnabled())
                self.window.preset_options.phone_idle.setChecked(True)
                self.assertTrue(self.window.preset_options.start.isEnabled())
                FakeEngine.hold = None
                FakeEngine.entered = None

    def test_global_timeout_panel_tracks_measured_set_and_clear_results(self):
        class MeasuredTimeout(FakeEngine):
            override = "null"

            def timeout(self, action="unchanged", verify_only=False):
                if not verify_only and action == "set":
                    type(self).override = "9223372036854775807"
                elif not verify_only and action == "clear":
                    type(self).override = "null"
                return {"status": "unchanged" if action == "unchanged" else "verified",
                        "effective_value": "1000" if self.override == "null" else self.override,
                        "override_value": self.override, "checked_at": "2026-10-01T01:00:00+00:00"}

        self.scan()
        self.window.clear_selection()
        for action, effective, override in (("set", "9223372036854775807", "9223372036854775807"),
                                            ("clear", "1000", "null")):
            with self.subTest(action=action):
                run = create_run(DEVICE, [], [], timeout_action=action)
                worker = WorkThread("run", MeasuredTimeout(serial=DEVICE["serial"]), self.store, run, self.window)
                self.window.begin_work(worker)
                self.wait_for(lambda: not self.window.busy)
                self.assertEqual(self.window.current_run["status"], "completed")
                self.assertEqual(self.window.preset_options.timeout_state.text(),
                                 f"Effective: {effective}\nLocal override: {override}")

    def test_terminal_timeout_without_readback_preserves_scan_observation(self):
        self.scan()
        before = self.window.preset_options.timeout_state.text()
        self.window.preset_options.phone_idle.setChecked(True)
        self.window.preset_options.observe_run({"status": "paused", "timeout": {
            "status": "failed", "effective_value": "unmeasured", "override_value": "unmeasured", "checked_at": None,
        }})
        self.assertEqual(self.window.preset_options.timeout_state.text(), before)
        self.assertFalse(self.window.preset_options.phone_idle.isChecked())

    def test_close_pauses_and_waits_for_verified_cleanup_without_blocking_ui(self):
        self.scan()
        self.window.show()
        FakeEngine.hold = threading.Event()
        FakeEngine.entered = threading.Event()
        run = create_run(DEVICE, APPS, ["RUN_IN_BACKGROUND"])
        worker = WorkThread("run", FakeEngine(serial=DEVICE["serial"]), self.store, run, self.window)
        self.window.begin_work(worker)
        self.wait_for(FakeEngine.entered.is_set)
        self.assertTrue(self.window.pause.isEnabled())
        self.assertFalse(self.window.scan.isEnabled())
        self.assertFalse(self.window.preset_options.start.isEnabled())
        started = time.monotonic()
        self.window.close()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(self.window.isVisible())
        self.assertTrue(worker.controller.pause_requested.is_set())
        FakeEngine.hold.set()
        self.wait_for(lambda: not self.window.busy)
        self.wait_for(lambda: not self.window.isVisible())
        saved = self.store.history(DEVICE)[0]
        self.assertEqual(saved["status"], "paused")
        self.assertTrue(saved["phone_available"])
        self.assertEqual(saved["packages"]["com.example.notes"]["results"]["RUN_IN_BACKGROUND"]["status"], "verified")

    def test_imported_report_is_visible_but_cannot_resume(self):
        legacy = {"schema_version": 2, "device_serial": "test-phone", "started_at": "2026-09-29T01:00:00+00:00",
                  "exit_code": 0, "packages": {"com.example.notes": {"native": "allow_verified_after_reopen"}}}
        path = Path(self.directory.name) / "legacy.json"
        path.write_text(json.dumps(legacy))
        imported = self.store.import_report(path)
        self.scan()
        self.window.reload_history(prefer_id=imported["id"])
        self.assertEqual(self.window.viewed_run["id"], imported["id"])
        self.window.preset_options.mode.setCurrentIndex(self.window.preset_options.mode.findData("resume"))
        self.window.preset_options.phone_idle.setChecked(True)
        self.assertFalse(self.window.preset_options.start.isEnabled())

    def test_render_fixture(self):
        self.scan()
        self.window.show()
        QTest.qWait(30)
        self.assertTrue(self.window.scan.isEnabled())
        self.assertIn("Main profile", self.window.readiness.text())
        screenshot = os.environ.get("VIVO_GUI_SCREENSHOT")
        if screenshot:
            self.assertTrue(self.window.grab().save(screenshot))

    def test_inventory_cleanup_failure_is_durable_and_does_not_offer_run(self):
        class UncleanInventory(FakeEngine):
            def inventory(self):
                resource = {"remote_dir": "/data/local/tmp/vivo-background-" + "a" * 32,
                            "serial": DEVICE["serial"], "identity": DEVICE["identity"], "kind": "inventory"}
                self.pending_cleanup.append(resource)
                self.resource_callback(resource)
                return copy.deepcopy(APPS)

        errors = []
        observed = []
        worker = WorkThread("scan", UncleanInventory(serial=DEVICE["serial"]), self.store)
        worker.failed.connect(errors.append)
        worker.observed.connect(observed.append)
        worker.start()
        self.assertTrue(worker.wait(3000))
        self.application.processEvents()
        self.assertIn("temporary phone files", errors[0])
        self.assertEqual(observed, [])
        saved = Store(self.store.path)
        resources = saved.data["scan_resources"]["test-hardware:0"]
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0]["serial"], "test-phone")
        worker.deleteLater()

    def test_scan_recovers_saved_inventory_resources_before_new_inventory(self):
        resource = {"remote_dir": "/data/local/tmp/vivo-background-" + "b" * 32,
                    "serial": DEVICE["serial"], "identity": DEVICE["identity"], "kind": "inventory"}
        self.store.data["scan_resources"] = {"test-hardware:0": [resource]}
        self.store.persist()
        self.scan()
        self.assertEqual(self.store.data["scan_resources"]["test-hardware:0"], [])
        self.assertIn(("recover", [resource]), FakeEngine.calls)

    def test_failed_rescan_clears_run_readiness_and_preserves_saved_evidence(self):
        class UncleanInventory(FakeEngine):
            def inventory(self):
                resource = {"remote_dir": "/data/local/tmp/vivo-background-" + "a" * 32,
                            "serial": DEVICE["serial"], "identity": DEVICE["identity"], "kind": "inventory"}
                self.pending_cleanup.append(resource)
                self.resource_callback(resource)
                return copy.deepcopy(APPS)

        self.scan()
        saved = create_run(DEVICE, APPS, ["native"])
        self.store.save_run(saved)
        self.window.reload_history(prefer_id=saved["id"])
        baseline = copy.deepcopy(self.store.data["scans"])
        self.window.preset_options.phone_idle.setChecked(True)
        self.assertTrue(self.window.preset_options.start.isEnabled())
        self.window.engine_factory = UncleanInventory
        with patch("vivo_power.gui.QMessageBox.warning"):
            self.window.scan_inventory()
            self.wait_for(lambda: not self.window.busy)
        self.assertIsNone(self.window.device)
        self.assertEqual(self.window.model.apps, [])
        self.assertEqual(self.window.model.selected, set())
        self.assertFalse(self.window.preset_options.phone_idle.isChecked())
        self.assertFalse(self.window.preset_options.start.isEnabled())
        self.assertIn("temporary phone files", self.window.activity.text())
        self.assertTrue(self.store.data["scan_resources"]["test-hardware:0"])
        self.assertEqual(self.store.data["scans"], baseline)
        self.assertEqual(self.window.history.currentData()["id"], saved["id"])

    def test_rescan_engine_failure_clears_previous_scan_readiness(self):
        self.scan()
        self.window.preset_options.phone_idle.setChecked(True)
        self.assertTrue(self.window.preset_options.start.isEnabled())
        self.window.engine_factory = unittest.mock.Mock(side_effect=ValueError("ADB executable not found"))
        with patch("vivo_power.gui.QMessageBox.warning"):
            self.window.scan_inventory()
        self.assertIsNone(self.window.device)
        self.assertEqual(self.window.model.apps, [])
        self.assertFalse(self.window.preset_options.phone_idle.isChecked())
        self.assertFalse(self.window.preset_options.start.isEnabled())
        self.assertIsNone(self.window.worker)
        self.assertIn("ADB executable not found", self.window.activity.text())

    def test_run_cleanup_failure_requires_rescan_before_another_run(self):
        class UncleanNative(FakeEngine):
            def perform(self, package, setting, verify_only=False):
                resource = {"remote_dir": "/data/local/tmp/vivo-background-" + "c" * 32,
                            "serial": DEVICE["serial"], "identity": DEVICE["identity"], "kind": "native"}
                self.pending_cleanup.append(resource)
                self.resource_callback(resource)
                return {"status": "failed", "error": "Native helper cleanup remains unconfirmed"}

        self.scan()
        self.window.preset_options.phone_idle.setChecked(True)
        run = create_run(DEVICE, APPS[:1], ["native"])
        self.window.begin_work(WorkThread("run", UncleanNative(serial=DEVICE["serial"]), self.store, run, self.window))
        self.wait_for(lambda: not self.window.busy)
        self.assertEqual(self.window.current_run["status"], "interrupted")
        self.assertFalse(self.window.current_run["phone_available"])
        resource = self.window.current_run["resources"][0]
        self.assertIsNone(self.window.device)
        self.assertEqual(self.window.model.apps, [])
        self.assertFalse(self.window.preset_options.phone_idle.isChecked())
        self.assertFalse(self.window.preset_options.start.isEnabled())
        self.assertEqual(self.window.history.currentData()["id"], run["id"])
        self.assertIn(resource["remote_dir"], self.window.details.toPlainText())
        self.assertIn("Phone cleanup not confirmed", self.window.activity.text())
        self.window.scan_inventory()
        self.wait_for(lambda: not self.window.busy)
        recovered = next(saved for saved in self.store.history(DEVICE) if saved["id"] == run["id"])
        self.assertEqual(recovered["resources"], [])
        self.assertTrue(recovered["phone_available"])
        self.assertIn(("recover", [resource]), FakeEngine.calls)
        self.assertIsNotNone(self.window.device)
        self.assertEqual(len(self.window.model.apps), len(APPS))
        self.assertFalse(self.window.preset_options.phone_idle.isChecked())
        self.window.preset_options.phone_idle.setChecked(True)
        self.assertTrue(self.window.preset_options.start.isEnabled())

    def test_failed_run_worker_clears_live_readiness_without_a_terminal_result(self):
        class FailedNative(FakeEngine):
            def perform(self, package, setting, verify_only=False):
                resource = {"remote_dir": "/data/local/tmp/vivo-background-" + "d" * 32,
                            "serial": DEVICE["serial"], "identity": DEVICE["identity"], "kind": "native"}
                self.pending_cleanup.append(resource)
                self.resource_callback(resource)
                raise ValueError("The operation could not produce a terminal result")

        self.scan()
        self.window.preset_options.phone_idle.setChecked(True)
        run = create_run(DEVICE, APPS[:1], ["native"])
        with patch("vivo_power.gui.QMessageBox.warning"):
            self.window.begin_work(WorkThread("run", FailedNative(serial=DEVICE["serial"]), self.store, run, self.window))
            self.wait_for(lambda: not self.window.busy)
        self.assertIsNone(self.window.device)
        self.assertEqual(self.window.model.apps, [])
        self.assertFalse(self.window.preset_options.phone_idle.isChecked())
        self.assertFalse(self.window.preset_options.start.isEnabled())
        self.assertIn("could not produce a terminal result", self.window.activity.text())
        self.assertIn("could not produce a terminal result", self.window.details.toPlainText())
        saved = self.store.history(DEVICE)[0]
        self.assertEqual(saved["id"], run["id"])
        self.assertFalse(saved["phone_available"])
        self.assertTrue(saved["resources"])

    def test_shortcut_preferences_repair_worker_then_save_portable_preferences(self):
        root = Path(self.directory.name) / "portable"
        root.mkdir()
        (root / "setup_linux.sh").write_text("#!/bin/bash\n")
        (root / "setup_win.vbs").write_text("WScript.Quit\n")
        (root / "install.local.json").write_text(json.dumps({"desktop": True, "app_menu": True, "other_setting": "preserved"}))
        self.window.installation_root = root
        self.window.portable_installation = True
        self.window.update_controls()
        self.assertTrue(self.window.installation_action.isEnabled())
        custom = Path(self.directory.name) / "Shortcuts"

        def choose_preferences():
            dialog = self.application.activeModalWidget()
            dialog.findChild(QCheckBox, "desktop_shortcut").setChecked(True)
            dialog.findChild(QCheckBox, "app_menu_shortcut").setChecked(False)
            dialog.findChild(QLineEdit, "shortcut_directory").setText(str(custom))
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

        with patch("vivo_power.gui.ensure_shortcuts", return_value=[str(custom / "vivo-background-power.desktop")]) as repair:
            QTimer.singleShot(0, choose_preferences)
            self.window.shortcut_preferences()
            self.wait_for(lambda: not self.window.busy)
        repair.assert_called_once_with(root=root, desktop=True, app_menu=False, directory=custom)
        preferences = json.loads((root / "install.local.json").read_text())
        self.assertEqual(preferences, {"desktop": True, "app_menu": False, "other_setting": "preserved", "directory": str(custom)})
        self.assertIn("Shortcuts repaired", self.window.activity.text())

    def test_shortcut_startup_guard_skips_wheels_and_repairs_portable_tree(self):
        root = Path(self.directory.name) / "portable"
        root.mkdir()
        self.window.installation_root = root
        self.window.portable_installation = False
        with patch("vivo_power.gui.ensure_shortcuts", return_value=[]) as repair:
            self.window.repair_startup_shortcuts()
            repair.assert_not_called()
            (root / "setup_linux.sh").write_text("#!/bin/bash\n")
            (root / "setup_win.vbs").write_text("WScript.Quit\n")
            self.window.portable_installation = True
            self.window.repair_startup_shortcuts()
            self.wait_for(lambda: not self.window.busy)
            repair.assert_called_once_with(root=root, desktop=True, app_menu=True, directory=None)
        self.assertFalse((root / "install.local.json").exists())


if __name__ == "__main__":
    unittest.main()
