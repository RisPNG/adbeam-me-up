import copy
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFormLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from test_gui import APPS, DEVICE, FakeEngine
from vivo_power.branding import APP_NAME
from vivo_power.gui import MainWindow, WorkThread
from vivo_power.presets import Preset, ResultColumn
from vivo_power.presets.registry import DEFAULT_PRESET, PRESETS
from vivo_power.run import RunController
from vivo_power.state import Store, create_run


class OtherEngine(FakeEngine):
    detected = 0

    def devices(self):
        type(self).detected += 1
        return super().devices()

    def inventory(self):
        return [{**copy.deepcopy(APPS[0]), "label": "Other module notes"}]


class OtherController(RunController):
    created = 0

    def __init__(self, *args, **kwargs):
        type(self).created += 1
        super().__init__(*args, **kwargs)


class OtherPanel(QWidget):
    options_changed = Signal()
    run_requested = Signal()

    def __init__(self, preset, parent=None):
        super().__init__(parent)
        self.preset = preset
        self.device = None
        self.observed_run = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Other module options"))
        self.start = QPushButton("Run other module")
        self.start.clicked.connect(self.run_requested)
        layout.addWidget(self.start)

    def observe_device(self, device):
        self.device = device

    def observe_run(self, run):
        self.observed_run = run

    def update_availability(self, busy, device, selected, resumable):
        self.start.setEnabled(not busy and device is not None and bool(selected))

    def review_run(self, store, device, inventory, selected, exclude_system, previous_scan, history):
        return create_run(device, inventory, ["RUN_IN_BACKGROUND"],
                          preset_id=self.preset.id, preset_name=self.preset.name)


OTHER = Preset(
    id="test-other", name="Other test preset", description="Other workflow", scope="Other scope",
    engine_type=OtherEngine, controller_type=OtherController, panel_type=OtherPanel,
    result_columns=(ResultColumn("Other policy", "RUN_IN_BACKGROUND"),),
    setting_labels={"RUN_IN_BACKGROUND": "Other policy"}, device_observations={},
)


class PresetTests(unittest.TestCase):
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
        self.window = MainWindow(self.store, presets=(replace(DEFAULT_PRESET, engine_type=FakeEngine), OTHER))
        FakeEngine.calls = []
        FakeEngine.hold = None
        FakeEngine.entered = None
        OtherEngine.detected = 0
        OtherController.created = 0

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

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def scan(self):
        self.window.detect_devices()
        self.wait_for(lambda: not self.window.busy)
        self.window.scan_inventory()
        self.wait_for(lambda: not self.window.busy)

    def test_current_preset_and_branding_are_shipped_without_test_module(self):
        self.assertEqual([(preset.id, preset.name) for preset in PRESETS], [("persist-v2413", "Persist V2413")])
        self.assertEqual(self.window.windowTitle(), APP_NAME)
        self.assertEqual(self.window.preset_selector.currentText(), "Persist V2413")
        form = self.window.preset_selector.parentWidget().layout()
        self.assertEqual(form.itemAt(0, QFormLayout.ItemRole.LabelRole).widget().text(), "Preset")
        self.assertEqual(form.itemAt(1, QFormLayout.ItemRole.LabelRole).widget().text(), "ADB")
        self.assertFalse(hasattr(self.window, "native"))

    def test_switch_replaces_options_backend_and_columns_and_clears_live_state(self):
        self.scan()
        original_panel = self.window.preset_options
        original_panel.phone_idle.setChecked(True)
        self.window.search.setText("Notes")
        self.window.exclude.setChecked(True)
        self.window.current_run = create_run(DEVICE, APPS, ["native"])
        self.window.preset_selector.setCurrentIndex(1)
        self.assertIsInstance(self.window.preset_options, OtherPanel)
        self.assertIs(self.window.preset_options, self.window.preset_panels.currentWidget())
        self.assertIsNone(self.window.device)
        self.assertIsNone(self.window.current_run)
        self.assertEqual(self.window.model.apps, [])
        self.assertEqual(self.window.model.selected, set())
        self.assertEqual(self.window.search.text(), "")
        self.assertFalse(self.window.exclude.isChecked())
        self.assertIn("Other policy", self.window.model.columns)
        self.assertNotIn("vivo power", self.window.model.columns)
        self.assertEqual(self.window.scope.text(), "Other scope")
        self.scan()
        self.assertEqual(OtherEngine.detected, 1)
        self.assertEqual(self.window.model.apps[0]["label"], "Other module notes")
        self.assertNotIn("timeout", self.window.device)
        self.window.preset_options.start.click()
        self.wait_for(lambda: not self.window.busy)
        self.assertEqual(OtherController.created, 1)
        self.assertEqual(self.window.current_run["preset"], {"id": OTHER.id, "name": OTHER.name})
        self.assertEqual(self.window.preset_options.observed_run["id"], self.window.current_run["id"])
        self.assertEqual(self.window.preset_options.observed_run["status"], "completed")
        self.window.preset_selector.setCurrentIndex(0)
        self.assertIs(self.window.preset_options, original_panel)
        self.assertFalse(original_panel.phone_idle.isChecked())
        self.assertFalse(original_panel.start.isEnabled())

    def test_history_and_scan_results_are_isolated_between_presets(self):
        for preset, status in [(DEFAULT_PRESET, "verified"), (OTHER, "rejected")]:
            run = create_run(DEVICE, APPS, ["RUN_IN_BACKGROUND"], preset_id=preset.id, preset_name=preset.name)
            run["packages"][APPS[0]["package"]]["results"]["RUN_IN_BACKGROUND"] = {
                "status": status, "checked_at": "2026-09-30T12:00:00+00:00",
            }
            self.store.save_run(run)
        self.scan()
        self.assertEqual(self.window.history.count(), 1)
        self.assertEqual(self.window.model.apps[0]["results"]["RUN_IN_BACKGROUND"]["status"], "verified")
        self.assertIn("Preset: Persist V2413", self.window.report_notice.text())
        self.window.preset_selector.setCurrentIndex(1)
        self.scan()
        self.assertEqual(self.window.history.count(), 1)
        self.assertEqual(self.window.viewed_run["preset"]["id"], OTHER.id)
        self.assertEqual(self.window.model.apps[0]["results"]["RUN_IN_BACKGROUND"]["status"], "rejected")
        self.assertIn("Other test preset", self.window.history.currentText())

    def test_selector_is_locked_during_run_and_cannot_switch_programmatically(self):
        self.scan()
        FakeEngine.hold = threading.Event()
        FakeEngine.entered = threading.Event()
        run = create_run(DEVICE, APPS, ["RUN_IN_BACKGROUND"])
        worker = WorkThread("run", FakeEngine(serial=DEVICE["serial"]), self.store, run, self.window)
        self.window.begin_work(worker)
        self.wait_for(FakeEngine.entered.is_set)
        self.assertFalse(self.window.preset_selector.isEnabled())
        self.window.preset_selector.setCurrentIndex(1)
        self.assertEqual(self.window.preset.id, DEFAULT_PRESET.id)
        self.assertEqual(self.window.preset_selector.currentIndex(), 0)
        FakeEngine.hold.set()
        self.wait_for(lambda: not self.window.busy)
        self.assertTrue(self.window.preset_selector.isEnabled())

    def test_scan_recovers_phone_resources_from_another_preset(self):
        resource = {"remote_dir": "/data/local/tmp/vivo-background-" + "c" * 32,
                    "serial": DEVICE["serial"], "identity": DEVICE["identity"], "kind": "native"}
        foreign = create_run(DEVICE, APPS, ["native"], preset_id=OTHER.id, preset_name=OTHER.name)
        foreign["resources"] = [resource]
        self.store.save_run(foreign)
        self.scan()
        self.assertIn(("recover", [resource]), FakeEngine.calls)
        self.assertEqual(self.store.history(DEVICE, preset_id=OTHER.id)[0]["resources"], [])
        self.assertEqual(self.window.history.count(), 0)

    def test_concrete_preset_import_does_not_depend_on_registry_import_order(self):
        result = subprocess.run(
            [os.sys.executable, "-c", "from vivo_power.presets.persist_v2413 import PERSIST_V2413; print(PERSIST_V2413.name)"],
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "Persist V2413")


if __name__ == "__main__":
    unittest.main()
