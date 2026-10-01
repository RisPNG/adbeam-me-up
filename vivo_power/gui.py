from __future__ import annotations

import argparse
from copy import deepcopy
from functools import partial
import json
from pathlib import Path
import sys

from PySide6.QtCore import (
    QAbstractTableModel, QIODevice, QLockFile, QModelIndex, QSaveFile, QSettings, QSortFilterProxyModel,
    QStandardPaths, Qt, QThread, QTimer, Signal, Slot,
)
from PySide6.QtGui import QCloseEvent, QFontDatabase, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSplitter, QStackedWidget, QTabWidget, QTableView, QVBoxLayout, QWidget,
)

from .branding import APP_ID, APP_NAME, STORAGE_APPLICATION, STORAGE_ORGANIZATION
from .presets import RUN_LABELS
from .presets.registry import DEFAULT_PRESET, PRESETS
from .run import recover_phone
from .shortcuts import ensure_shortcuts
from .state import INSTALLATION_FIELDS, Store


STATUS_LABELS = {
    "verified": "Verified", "already_configured": "Already configured",
    "unavailable": "Unavailable", "rejected": "Rejected",
    "not_present": "Not present at readback", "failed": "Failed",
    "pending": "Pending", "not_selected": "Not selected",
    "not_checked": "Not checked", "unverified": "Unverified",
    "accepted": "Accepted", "not_retained": "Not present at readback",
    "rejected_unknown_package": "Rejected", "unconfirmed": "Not confirmed",
    "not_requested": "Not requested", "error": "Failed",
}


class AppModel(QAbstractTableModel):
    selection_changed = Signal()

    def __init__(self, parent=None, selectable=True, preset=DEFAULT_PRESET):
        super().__init__(parent)
        self.apps = []
        self.selected = set()
        self.selectable = selectable
        self.frozen = False
        self.preset = preset
        self.columns = ("Apply", "App", "Package", "Type", "Installation",
                        *(column.label for column in preset.result_columns), "Last verification")

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.apps)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.columns[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        app = self.apps[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.UserRole:
            return app
        if role == Qt.ItemDataRole.CheckStateRole and column == 0 and self.selectable:
            return Qt.CheckState.Checked if app["package"] in self.selected else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.ToolTipRole:
            shared = app.get("shared_uid_packages", [])
            if shared:
                return "UID-level settings also affect: " + ", ".join(shared)
            return json.dumps(app, indent=2, ensure_ascii=False)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if column == 0:
            return None
        if column == 1:
            return app.get("label") or app["package"]
        if column == 2:
            return app["package"]
        if column == 3:
            return "System/component" if app.get("system") else "User app"
        if column == 4:
            return {"new": "New installation", "changed": "Changed installation", "same": "Unchanged"}.get(app.get("change"), "Unknown")
        results = app.get("results", {})
        if column == len(self.columns) - 1:
            checked = max((result.get("checked_at") or "" for result in results.values()), default="")
            return checked or "Not checked"
        definition = self.preset.result_columns[column - 5]
        result = results.get(definition.setting, {})
        status = result.get(definition.value_key, result.get(definition.legacy_key, "not_checked"))
        if isinstance(status, dict):
            status = status.get("status", "not_checked")
        return STATUS_LABELS.get(status, status.replace("_", " ").capitalize())

    def flags(self, index):
        flags = super().flags(index)
        if self.selectable and not self.frozen and index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if (not index.isValid() or index.column() != 0 or not self.selectable
                or self.frozen or role != Qt.ItemDataRole.CheckStateRole):
            return False
        package = self.apps[index.row()]["package"]
        if value == Qt.CheckState.Checked or value == Qt.CheckState.Checked.value:
            self.selected.add(package)
        else:
            self.selected.discard(package)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.selection_changed.emit()
        return True

    def observe_inventory(self, apps):
        self.beginResetModel()
        self.apps = deepcopy(apps)
        self.selected = {app["package"] for app in apps} if self.selectable else set()
        self.endResetModel()
        self.selection_changed.emit()

    def observe_progress(self, run):
        for row, app in enumerate(self.apps):
            record = run.get("packages", {}).get(app["package"])
            if not record:
                continue
            installation = record.get("installation", {})
            if any(installation.get(field) != app.get(field) for field in INSTALLATION_FIELDS):
                continue
            app["results"] = deepcopy(record.get("results", {}))
            app["evidence_run"] = run["id"]
            self.dataChanged.emit(self.index(row, 5), self.index(row, len(self.columns) - 1))


class AppFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.search = ""
        self.kind = "all"
        self.exclude_system = False
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def choose_filter(self, search, kind, exclude_system):
        self.search = search.casefold()
        self.kind = kind
        self.exclude_system = exclude_system
        self.invalidate()

    def filterAcceptsRow(self, source_row, source_parent):
        app = self.sourceModel().apps[source_row]
        if self.exclude_system and app.get("system"):
            return False
        if self.search and self.search not in (app["package"] + " " + app.get("label", "")).casefold():
            return False
        if self.kind == "new":
            return app.get("change") == "new"
        if self.kind == "changed":
            return app.get("change") in {"new", "changed"}
        results = app.get("results", {})
        if self.kind == "unfinished":
            return not results or any(result.get("status") in {"pending", "failed", "unverified", "not_checked", "rejected", "not_present"} for result in results.values())
        if self.kind == "results":
            return any(result.get("checked_at") for result in results.values())
        return True


class WorkThread(QThread):
    observed = Signal(object)
    progressed = Signal(object)
    failed = Signal(str)

    def __init__(self, action, engine, store, run=None, parent=None, shortcut_options=None, preset=DEFAULT_PRESET):
        super().__init__(parent)
        self.action = action
        self.engine = engine
        self.store = store
        self.job = run
        self.preset = preset
        self.controller = preset.controller_type(store, engine, run, self.progressed.emit, preset_id=preset.id) if run else None
        self.shortcut_options = shortcut_options

    def run(self):
        try:
            if self.action == "shortcuts":
                result = ensure_shortcuts(**self.shortcut_options)
            elif self.action == "devices":
                result = self.engine.devices()
            elif self.action == "scan":
                device = self.engine.device()
                if not device["ready"]:
                    raise RuntimeError(device["reason"])
                recover_phone(self.store, self.engine, device)
                for key, (method, arguments) in self.preset.device_observations.items():
                    device[key] = getattr(self.engine, method)(**arguments)
                self.engine.resource_callback = partial(self.store.record_scan_resource, device, self.engine)
                apps = self.engine.inventory()
                if self.engine.pending_cleanup:
                    raise RuntimeError("Inventory completed, but temporary phone files could not be cleaned up. Reconnect this phone before beginning a run.")
                previous_scan = f"{device['identity']}:{device['profile']}" in self.store.data["scans"]
                apps = self.store.save_scan(device, apps)
                latest = self.store.latest_results(device, preset_id=self.preset.id)
                for app in apps:
                    app["results"] = deepcopy(latest.get(app["package"], {}))
                result = {"device": device, "apps": apps, "previous_scan": previous_scan}
            else:
                result = self.controller.execute()
            self.observed.emit(result)
        except Exception as error:
            self.failed.emit(str(error))


class MainWindow(QMainWindow):
    def __init__(self, store=None, engine_factory=None, adb_path=None, parent=None, installation_root=None, presets=PRESETS):
        super().__init__(parent)
        self.store = store or Store()
        self.engine_factory = engine_factory
        self.preferences = QSettings()
        self.presets = presets
        chosen_preset = self.preferences.value("preset", DEFAULT_PRESET.id)
        self.preset = next((preset for preset in presets if preset.id == chosen_preset), presets[0])
        self.device = None
        self.previous_scan = False
        self.current_run = None
        self.viewed_run = None
        self.worker = None
        self.busy = False
        self.closing = False
        self.installation_root = Path(installation_root) if installation_root else Path(__file__).resolve().parents[1]
        self.portable_installation = (self.installation_root / "setup_linux.sh").is_file() and (self.installation_root / "setup_win.vbs").is_file()
        self.pending_shortcut_preferences = None
        self.automatic_shortcut_repair = False
        installation_menu = self.menuBar().addMenu("Installation")
        self.installation_action = installation_menu.addAction("Repair shortcuts…")
        self.installation_action.setObjectName("repair_shortcuts")
        self.installation_action.setToolTip("Shortcut repair is available for the portable folder with its setup launchers.")
        self.installation_action.triggered.connect(self.shortcut_preferences)
        self.shutdown_wait = QTimer(self)
        self.shutdown_wait.setSingleShot(True)
        self.shutdown_wait.setInterval(180_000)
        self.shutdown_wait.timeout.connect(self.shutdown_delayed)
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 880)
        central = QWidget(self)
        self.setCentralWidget(central)
        page = QVBoxLayout(central)
        page.setContentsMargins(18, 16, 18, 16)
        heading = QLabel(APP_NAME)
        font = heading.font()
        font.setPointSize(font.pointSize() + 5)
        font.setBold(True)
        heading.setFont(font)
        page.addWidget(heading)
        self.description = QLabel(self.preset.description)
        self.description.setWordWrap(True)
        page.addWidget(self.description)

        connection = QGroupBox("Connect and scan")
        connection_form = QFormLayout(connection)
        self.preset_selector = QComboBox()
        self.preset_selector.setObjectName("preset")
        for preset in presets:
            self.preset_selector.addItem(preset.name, preset.id)
        self.preset_selector.setCurrentIndex(self.preset_selector.findData(self.preset.id))
        connection_form.addRow("Preset", self.preset_selector)
        paths = QHBoxLayout()
        self.adb_path = QLineEdit(adb_path or self.preferences.value("adb_path", ""))
        self.adb_path.setPlaceholderText("ADB found automatically, or choose executable")
        self.adb_path.setObjectName("adb_path")
        browse = QPushButton("Browse…")
        paths.addWidget(self.adb_path)
        paths.addWidget(browse)
        connection_form.addRow("ADB", paths)
        devices_row = QHBoxLayout()
        self.devices = QComboBox()
        self.devices.setObjectName("devices")
        self.detect = QPushButton("Find devices")
        self.scan = QPushButton("Scan apps")
        self.scan.setObjectName("scan")
        devices_row.addWidget(self.devices, 1)
        devices_row.addWidget(self.detect)
        devices_row.addWidget(self.scan)
        connection_form.addRow("Phone", devices_row)
        self.readiness = QLabel("Connect a phone and authorize USB debugging. Scanning does not navigate Settings.")
        self.readiness.setWordWrap(True)
        connection_form.addRow(self.readiness)
        self.scope = QLabel(self.preset.scope)
        self.scope.setWordWrap(True)
        connection_form.addRow(self.scope)
        page.addWidget(connection)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        page.addWidget(splitter, 1)
        self.tabs = QTabWidget()
        splitter.addWidget(self.tabs)
        apps_page = QWidget()
        apps_layout = QVBoxLayout(apps_page)
        app_actions = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search app or package")
        self.search.setClearButtonEnabled(True)
        self.filter = QComboBox()
        for label, value in (("All apps", "all"), ("New installations", "new"),
                             ("New or changed", "changed"), ("Unfinished work", "unfinished"),
                             ("Previous results", "results")):
            self.filter.addItem(label, value)
        app_actions.addWidget(self.search, 1)
        app_actions.addWidget(self.filter)
        apps_layout.addLayout(app_actions)
        selection_actions = QHBoxLayout()
        self.select_all = QPushButton("Select visible")
        self.clear = QPushButton("Clear all")
        self.exclude = QCheckBox("Exclude system apps")
        self.exclude.setObjectName("exclude_system")
        self.selection = QLabel()
        selection_actions.addWidget(self.select_all)
        selection_actions.addWidget(self.clear)
        selection_actions.addWidget(self.exclude)
        selection_actions.addStretch()
        selection_actions.addWidget(self.selection)
        apps_layout.addLayout(selection_actions)
        self.model = AppModel(self, preset=self.preset)
        self.proxy = AppFilter(self)
        self.proxy.setSourceModel(self.model)
        self.app_table = QTableView()
        self.app_table.setModel(self.proxy)
        self.app_table.setSortingEnabled(True)
        self.app_table.setAlternatingRowColors(True)
        self.app_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.app_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.app_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.app_table.horizontalHeader().setStretchLastSection(True)
        self.app_table.verticalHeader().hide()
        apps_layout.addWidget(self.app_table)
        self.baseline = QLabel("Scan once to establish the installation baseline. Saved results show their verification time.")
        self.baseline.setWordWrap(True)
        apps_layout.addWidget(self.baseline)
        self.tabs.addTab(apps_page, "Apps")

        history_page = QWidget()
        history_layout = QVBoxLayout(history_page)
        history_actions = QHBoxLayout()
        self.history = QComboBox()
        self.history.setObjectName("history")
        self.refresh_history = QPushButton("Refresh")
        self.import_button = QPushButton("Import JSON…")
        self.export_json = QPushButton("Export JSON…")
        self.export_summary = QPushButton("Export summary…")
        history_actions.addWidget(self.history, 1)
        history_actions.addWidget(self.refresh_history)
        history_layout.addLayout(history_actions)
        report_actions = QHBoxLayout()
        report_actions.addWidget(self.import_button)
        report_actions.addWidget(self.export_json)
        report_actions.addWidget(self.export_summary)
        report_actions.addStretch()
        history_layout.addLayout(report_actions)
        self.report_notice = QLabel("Saved evidence is separate from the current inventory. Imports cannot be resumed.")
        self.report_notice.setWordWrap(True)
        history_layout.addWidget(self.report_notice)
        self.result_model = AppModel(self, selectable=False, preset=self.preset)
        self.result_table = QTableView()
        self.result_table.setModel(self.result_model)
        self.result_table.setColumnHidden(0, True)
        self.result_table.setColumnHidden(4, True)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.result_table.verticalHeader().hide()
        history_layout.addWidget(self.result_table)
        self.tabs.addTab(history_page, "Results and history")

        detail_page = QWidget()
        detail_layout = QVBoxLayout(detail_page)
        detail_layout.addWidget(QLabel("Installation identity, shared UID packages, verification times, commands, and errors"))
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.details.setMaximumBlockCount(8000)
        detail_layout.addWidget(self.details)
        self.tabs.addTab(detail_page, "Advanced details")

        self.preset_panels = QStackedWidget()
        self.preset_panels.setMinimumWidth(330)
        self.preset_panels.setMaximumWidth(400)
        for preset in presets:
            panel = preset.panel_type(preset, self)
            panel.options_changed.connect(self.update_controls)
            panel.run_requested.connect(self.preview_run)
            self.preset_panels.addWidget(panel)
        self.preset_panels.setCurrentIndex(self.preset_selector.currentIndex())
        self.preset_options = self.preset_panels.currentWidget()
        splitter.addWidget(self.preset_panels)
        splitter.setStretchFactor(0, 1)

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.pause = QPushButton("Pause")
        self.stop = QPushButton("Stop")
        self.pause.setObjectName("pause")
        self.stop.setObjectName("stop")
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.pause)
        progress_row.addWidget(self.stop)
        page.addLayout(progress_row)
        self.activity = QLabel("Ready. No phone settings changed.")
        self.activity.setWordWrap(True)
        page.addWidget(self.activity)

        browse.clicked.connect(self.choose_adb)
        self.detect.clicked.connect(self.detect_devices)
        self.scan.clicked.connect(self.scan_inventory)
        self.devices.currentIndexChanged.connect(self.device_selected)
        self.preset_selector.currentIndexChanged.connect(self.preset_selected)
        self.search.textChanged.connect(self.filter_apps)
        self.filter.currentIndexChanged.connect(self.filter_apps)
        self.exclude.toggled.connect(self.filter_apps)
        self.select_all.clicked.connect(self.select_visible)
        self.clear.clicked.connect(self.clear_selection)
        self.model.selection_changed.connect(self.update_controls)
        self.pause.clicked.connect(self.pause_run)
        self.stop.clicked.connect(self.stop_run)
        self.history.currentIndexChanged.connect(self.view_history)
        self.refresh_history.clicked.connect(self.reload_history)
        self.import_button.clicked.connect(self.import_report)
        self.export_json.clicked.connect(self.export_report)
        self.export_summary.clicked.connect(self.export_readable_report)
        self.app_table.selectionModel().currentChanged.connect(self.inspect_app)
        self.result_table.selectionModel().currentChanged.connect(self.inspect_result)
        if self.preferences.contains("geometry"):
            self.restoreGeometry(self.preferences.value("geometry"))
        self.reload_history()
        self.update_controls()

    @Slot()
    def preset_selected(self):
        if self.busy:
            self.preset_selector.blockSignals(True)
            self.preset_selector.setCurrentIndex(self.preset_selector.findData(self.preset.id))
            self.preset_selector.blockSignals(False)
            return
        self.preset = self.presets[self.preset_selector.currentIndex()]
        self.preset_panels.setCurrentIndex(self.preset_selector.currentIndex())
        self.preset_options = self.preset_panels.currentWidget()
        self.description.setText(self.preset.description)
        self.scope.setText(self.preset.scope)
        self.device = None
        self.previous_scan = False
        self.current_run = None
        self.viewed_run = None
        self.preset_options.observe_device(None)
        self.search.clear()
        self.filter.setCurrentIndex(0)
        self.exclude.setChecked(False)
        self.model.beginResetModel()
        self.model.preset = self.preset
        self.model.columns = ("Apply", "App", "Package", "Type", "Installation",
                              *(column.label for column in self.preset.result_columns), "Last verification")
        self.model.apps = []
        self.model.selected = set()
        self.model.endResetModel()
        self.result_model.beginResetModel()
        self.result_model.preset = self.preset
        self.result_model.columns = self.model.columns
        self.result_model.apps = []
        self.result_model.endResetModel()
        self.proxy.choose_filter("", "all", False)
        self.progress.setValue(0)
        self.details.clear()
        self.baseline.setText("Scan once to establish the installation baseline. Saved results show their verification time.")
        self.readiness.setText("Scan the selected authorized phone before choosing a run.")
        self.activity.setText(f"{self.preset.name} selected. Scan the phone to load this preset's inventory and evidence.")
        self.reload_history()
        self.update_controls()

    @Slot()
    def choose_adb(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose ADB executable", self.adb_path.text())
        if path:
            self.adb_path.setText(path)
            self.device = None
            self.devices.clear()
            self.model.observe_inventory([])
            self.update_controls()

    @Slot()
    def detect_devices(self):
        try:
            engine = (self.engine_factory or self.preset.engine_type)(adb_path=self.adb_path.text().strip() or None)
        except (OSError, RuntimeError, ValueError) as error:
            self.work_failed(str(error))
            return
        self.begin_work(WorkThread("devices", engine, self.store, parent=self, preset=self.preset))
        self.activity.setText("Finding ADB devices…")

    @Slot()
    def scan_inventory(self):
        serial = self.devices.currentData()
        self.device_selected()
        try:
            engine = (self.engine_factory or self.preset.engine_type)(adb_path=self.adb_path.text().strip() or None, serial=serial)
        except (OSError, RuntimeError, ValueError) as error:
            self.work_failed(str(error))
            return
        self.begin_work(WorkThread("scan", engine, self.store, parent=self, preset=self.preset))
        self.activity.setText("Scanning main-profile inventory. Settings are not being navigated…")

    def begin_work(self, worker):
        self.worker = worker
        self.busy = True
        self.model.frozen = True
        worker.observed.connect(self.observe_work)
        worker.progressed.connect(self.observe_progress)
        worker.failed.connect(self.work_failed)
        worker.finished.connect(self.finish_work)
        self.update_controls()
        worker.start()

    @Slot(object)
    def observe_work(self, result):
        if self.worker.action == "shortcuts":
            if self.pending_shortcut_preferences is not None:
                output = QSaveFile(str(self.installation_root / "install.local.json"))
                payload = (json.dumps(self.pending_shortcut_preferences, indent=2) + "\n").encode("utf-8")
                if not output.open(QIODevice.OpenModeFlag.WriteOnly) or output.write(payload) != len(payload) or not output.commit():
                    self.work_failed("Shortcuts were repaired, but installation preferences could not be saved: " + output.errorString())
                    return
                self.pending_shortcut_preferences = None
            self.activity.setText("Shortcuts repaired for this folder." if result else "Desktop and app-menu shortcuts are disabled.")
            self.details.setPlainText("Shortcut locations:\n" + "\n".join(result))
        elif self.worker.action == "devices":
            previous = self.devices.currentData() or self.preferences.value("serial", "")
            self.devices.blockSignals(True)
            self.devices.clear()
            selected = -1
            for device in result:
                state = device.get("state", "device")
                label = f"{device.get('model') or device.get('name') or device['serial']} • {device['serial']} • {state}"
                self.devices.addItem(label, device["serial"] if state == "device" else None)
                if device["serial"] == previous and state == "device":
                    selected = self.devices.count() - 1
            if selected >= 0:
                self.devices.setCurrentIndex(selected)
            self.devices.blockSignals(False)
            self.device_selected()
            self.activity.setText("Select the intended authorized phone and scan its apps." if result else "No ADB devices found. Check USB debugging, cable, and ADB path.")
        elif self.worker.action == "scan":
            self.device = result["device"]
            self.previous_scan = result["previous_scan"]
            self.model.observe_inventory(result["apps"])
            self.baseline.setText("Compared installation identities with the saved baseline. Saved results show their own verification time."
                                  if self.previous_scan else "First scan: there is no previous inventory baseline; all observed installations are new.")
            self.preset_options.observe_device(self.device)
            self.readiness.setText(f"{self.device.get('name') or self.device.get('model', 'Phone')} • Android {self.device.get('android', '?')} • {self.device.get('origin_os', '?')} • {self.device['serial']} • Main profile (0)")
            self.activity.setText(f"Scan complete: {len(result['apps'])} main-profile apps. System apps included by default.")
            self.reload_history()
        else:
            self.current_run = result
            self.observe_progress(result)
            self.reload_history(prefer_id=result["id"])
            if result.get("phone_available") is not True:
                self.device_selected()
            self.preset_options.observe_run(result)

    @Slot()
    def finish_work(self):
        worker = self.worker
        self.worker = None
        self.busy = False
        self.model.frozen = False
        self.shutdown_wait.stop()
        self.automatic_shortcut_repair = False
        self.pending_shortcut_preferences = None
        self.update_controls()
        worker.deleteLater()
        if self.closing:
            QTimer.singleShot(0, self.close)

    @Slot(str)
    def work_failed(self, message):
        if self.worker is not None and self.worker.action in {"scan", "run"}:
            self.device_selected()
        self.activity.setText("Failed: " + message)
        self.details.setPlainText(message)
        if not self.closing and not self.automatic_shortcut_repair:
            QMessageBox.warning(self, "Operation failed", message)

    @Slot()
    def device_selected(self):
        self.device = None
        self.model.observe_inventory([])
        self.preset_options.observe_device(None)
        self.readiness.setText("Scan the selected authorized phone before choosing a run.")
        self.update_controls()

    @Slot()
    def filter_apps(self):
        self.proxy.choose_filter(self.search.text(), self.filter.currentData(), self.exclude.isChecked())
        self.update_controls()

    @Slot()
    def select_visible(self):
        for row in range(self.proxy.rowCount()):
            source = self.proxy.mapToSource(self.proxy.index(row, 0))
            self.model.setData(source, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)

    @Slot()
    def clear_selection(self):
        for row in range(self.model.rowCount()):
            self.model.setData(self.model.index(row, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)

    @Slot()
    def update_controls(self):
        running = self.busy and self.worker is not None and self.worker.controller is not None
        selected = [app for app in self.model.apps if app["package"] in self.model.selected
                    and not (self.exclude.isChecked() and app.get("system"))]
        visible_selected = sum(self.proxy.index(row, 0).data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked for row in range(self.proxy.rowCount()))
        hidden = len(selected) - visible_selected
        self.selection.setText(f"{len(selected)} selected" + (f" ({hidden} hidden by search/filter)" if hidden else ""))
        for widget in [self.adb_path, self.devices, self.detect, self.exclude, self.select_all, self.clear,
                       self.preset_selector, self.import_button, self.refresh_history]:
            widget.setEnabled(not self.busy)
        self.scan.setEnabled(not self.busy and bool(self.devices.currentData()))
        self.preset_options.update_availability(self.busy, self.device, selected, self.history.currentData())
        self.pause.setEnabled(running)
        self.stop.setEnabled(running)
        self.export_json.setEnabled(self.viewed_run is not None and not self.busy)
        self.export_summary.setEnabled(self.viewed_run is not None and not self.busy)
        self.installation_action.setEnabled(self.portable_installation and not self.busy)

    @Slot()
    def shortcut_preferences(self):
        preferences_file = self.installation_root / "install.local.json"
        try:
            preferences = json.loads(preferences_file.read_text(encoding="utf-8")) if preferences_file.is_file() else {}
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Installation preferences unavailable", str(error))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Repair installation shortcuts")
        dialog.setObjectName("shortcut_preferences")
        dialog.resize(580, 340)
        layout = QVBoxLayout(dialog)
        location = QLabel("Portable folder: " + str(self.installation_root))
        location.setWordWrap(True)
        layout.addWidget(location)
        desktop = QCheckBox("Desktop shortcut")
        desktop.setObjectName("desktop_shortcut")
        desktop.setChecked(preferences.get("desktop", True))
        app_menu = QCheckBox("Applications menu / Start menu shortcut")
        app_menu.setObjectName("app_menu_shortcut")
        app_menu.setChecked(preferences.get("app_menu", True))
        layout.addWidget(desktop)
        layout.addWidget(app_menu)
        layout.addWidget(QLabel("Optional desktop shortcut folder (blank uses the system desktop):"))
        folder_row = QHBoxLayout()
        directory = QLineEdit(preferences.get("directory") or "")
        directory.setObjectName("shortcut_directory")
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: directory.setText(QFileDialog.getExistingDirectory(dialog, "Choose shortcut folder", directory.text()) or directory.text()))
        folder_row.addWidget(directory)
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)
        explanation = QLabel("Enabled shortcuts are repaired on launch. Disabled shortcuts are removed. After moving this folder, run its setup launcher once to point shortcuts at the new location. Windows also keeps a launcher shortcut inside this folder.")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Repair shortcuts")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        custom_directory = Path(directory.text()).expanduser().resolve() if directory.text().strip() else None
        preferences.update(desktop=desktop.isChecked(), app_menu=app_menu.isChecked(),
                           directory=str(custom_directory) if custom_directory else None)
        self.pending_shortcut_preferences = preferences
        options = {"root": self.installation_root, "desktop": desktop.isChecked(),
                   "app_menu": app_menu.isChecked(), "directory": custom_directory}
        self.begin_work(WorkThread("shortcuts", None, self.store, parent=self, shortcut_options=options))
        self.activity.setText("Repairing the selected installation shortcuts…")

    @Slot()
    def repair_startup_shortcuts(self):
        if not self.portable_installation or self.busy:
            return
        preferences_file = self.installation_root / "install.local.json"
        try:
            preferences = json.loads(preferences_file.read_text(encoding="utf-8")) if preferences_file.is_file() else {}
        except (OSError, ValueError) as error:
            self.activity.setText("Automatic shortcut repair could not read installation preferences: " + str(error))
            return
        options = {"root": self.installation_root, "desktop": preferences.get("desktop", True),
                   "app_menu": preferences.get("app_menu", True), "directory": preferences.get("directory")}
        if not options["desktop"] and not options["app_menu"] and sys.platform != "win32":
            return
        self.automatic_shortcut_repair = True
        self.begin_work(WorkThread("shortcuts", None, self.store, parent=self, shortcut_options=options))
        self.activity.setText("Checking installation shortcuts…")

    @Slot()
    def preview_run(self):
        run = self.preset_options.review_run(
            self.store, self.device, self.model.apps, self.model.selected,
            self.exclude.isChecked(), self.previous_scan, self.history.currentData(),
        )
        if run is None:
            return
        self.current_run = run
        try:
            engine = (self.engine_factory or self.preset.engine_type)(adb_path=self.adb_path.text().strip() or None, serial=self.device["serial"])
            worker = WorkThread("run", engine, self.store, run, self, preset=self.preset)
        except (OSError, RuntimeError, ValueError) as error:
            self.work_failed(str(error))
            return
        self.begin_work(worker)
        self.activity.setText("Starting the reviewed queue…")

    @Slot(object)
    def observe_progress(self, run):
        self.current_run = deepcopy(run)
        self.model.observe_progress(run)
        status = run.get("status", "running")
        selected_settings = run.get("settings", [])
        total = run.get("total_operations", 0)
        completed = run.get("completed_operations", 0)
        failures = 0
        skips = 0
        for record in run.get("packages", {}).values():
            for setting in selected_settings:
                result = record.get("results", {}).get(setting, {})
                failures += result.get("status") in {"failed", "rejected", "not_present"}
                skips += result.get("status") == "unavailable"
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(completed if total else int(status not in {"running", "pausing", "stopping"}))
        elapsed = int(run.get("elapsed_seconds", 0))
        remaining = run.get("estimated_remaining_seconds")
        timing = f"Elapsed {elapsed // 60}m {elapsed % 60}s"
        if remaining is not None:
            timing += f" • Estimated remaining {int(remaining) // 60}m"
        label = RUN_LABELS.get(status, status)
        current = run.get("current")
        stage = run.get("stage") or run.get("phase", "")
        package = current[0] if current else ""
        if current:
            stage = ("Checking " if run.get("phase") == "verify" or run.get("mode") == "verify" else "Applying ") + self.preset.setting_labels.get(current[1], current[1])
        phone = ""
        if status in {"paused", "stopped", "interrupted", "completed", "completed_with_limitations", "failed"}:
            phone = " • Phone available" if run.get("phone_available") is True else " • Phone cleanup not confirmed; review details before using the phone"
        self.activity.setText(f"{label} • {stage} • {package}\n{completed}/{total} operations • {skips} unavailable • {failures} issues • {timing}{phone}")
        self.details.setPlainText(json.dumps(run, indent=2, ensure_ascii=False))
        if status in {"pausing", "stopping"}:
            self.pause.setEnabled(False)

    @Slot()
    def pause_run(self):
        if self.worker and self.worker.controller:
            self.worker.controller.pause()
            self.activity.setText("Pausing after current operation. Waiting for verified checkpoint and phone cleanup…")
            self.pause.setEnabled(False)

    @Slot()
    def stop_run(self):
        if self.worker and self.worker.controller:
            self.worker.controller.stop()
            self.activity.setText("Stopping after current operation. Completed changes are retained; waiting for phone cleanup…")
            self.pause.setEnabled(False)
            self.stop.setEnabled(False)

    @Slot()
    def reload_history(self, prefer_id=None):
        previous_id = prefer_id or (self.history.currentData() or {}).get("id")
        reports = self.store.history(preset_id=self.preset.id)
        self.history.blockSignals(True)
        self.history.clear()
        chosen = -1
        for run in reports:
            label = f"{run.get('preset', {}).get('name', self.preset.name)} • {run.get('started_at', '')} • {run.get('device', {}).get('serial', 'Unknown phone')} • {RUN_LABELS.get(run.get('status'), run.get('status', 'Imported'))} • {len(run.get('packages', {}))} apps"
            if run.get("imported"):
                label += " • Imported evidence"
            self.history.addItem(label, run)
            if run["id"] == previous_id:
                chosen = self.history.count() - 1
        if chosen >= 0:
            self.history.setCurrentIndex(chosen)
        self.history.blockSignals(False)
        self.view_history()

    @Slot()
    def view_history(self):
        self.viewed_run = self.history.currentData()
        apps = []
        if self.viewed_run:
            for package, record in self.viewed_run.get("packages", {}).items():
                app = deepcopy(record.get("installation", {}))
                app["package"] = package
                app["label"] = app.get("label") or record.get("label") or package
                app["results"] = deepcopy(record.get("results", {}))
                apps.append(app)
            self.report_notice.setText(f"Preset: {self.viewed_run.get('preset', {}).get('name', self.preset.name)}\nSaved evidence from {self.viewed_run.get('started_at', 'unknown time')} • {self.viewed_run['id']}\nViewing history does not replace the current inventory or its measured state.")
        else:
            self.report_notice.setText("Saved evidence is separate from the current inventory. Imports cannot be resumed.")
        self.result_model.observe_inventory(apps)
        self.update_controls()

    @Slot(QModelIndex, QModelIndex)
    def inspect_app(self, current, previous):
        if current.isValid():
            app = self.proxy.mapToSource(current).data(Qt.ItemDataRole.UserRole)
            self.details.setPlainText(json.dumps(app, indent=2, ensure_ascii=False))

    @Slot(QModelIndex, QModelIndex)
    def inspect_result(self, current, previous):
        if current.isValid():
            app = current.data(Qt.ItemDataRole.UserRole)
            self.details.setPlainText(json.dumps(app, indent=2, ensure_ascii=False))

    @Slot()
    def import_report(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import saved report", "", "JSON reports (*.json)")
        if path:
            try:
                run = self.store.import_report(path)
                self.reload_history(prefer_id=run["id"])
                self.tabs.setCurrentIndex(1)
            except (OSError, ValueError, KeyError) as error:
                QMessageBox.warning(self, "Import failed", str(error))

    @Slot()
    def export_report(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export report", f"{APP_ID}.json", "JSON reports (*.json)")
        if path:
            try:
                self.store.export_report(self.viewed_run, path)
            except OSError as error:
                QMessageBox.warning(self, "Export failed", str(error))

    @Slot()
    def export_readable_report(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export readable summary", f"{APP_ID}.txt", "Text reports (*.txt)")
        if path:
            try:
                self.store.export_report(self.viewed_run, path, readable=True)
            except OSError as error:
                QMessageBox.warning(self, "Export failed", str(error))

    def closeEvent(self, event: QCloseEvent):
        if self.busy:
            self.closing = True
            if not self.shutdown_wait.isActive():
                self.shutdown_wait.start()
            if self.worker and self.worker.controller:
                self.pause_run()
                self.activity.setText("Closing after current operation and phone cleanup. The window remains responsive while the checkpoint is saved…")
            else:
                self.activity.setText("Closing after the current request finishes…")
            event.ignore()
            return
        self.preferences.setValue("preset", self.preset.id)
        self.preferences.setValue("adb_path", self.adb_path.text())
        self.preferences.setValue("serial", self.devices.currentData() or "")
        self.preferences.setValue("geometry", self.saveGeometry())
        event.accept()

    @Slot()
    def shutdown_delayed(self):
        self.closing = False
        self.activity.setText("The current operation has not confirmed phone cleanup yet. The window remains open and the pause request remains active. Review details; it is not safe to announce the phone available.")


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} desktop GUI")
    parser.add_argument("--adb", help="ADB executable path")
    parser.add_argument("--state-dir", type=Path, help="Writable checkpoint and report directory")
    args = parser.parse_args()
    application = QApplication(sys.argv[:1])
    application.setApplicationName(STORAGE_APPLICATION)
    application.setApplicationDisplayName(APP_NAME)
    application.setDesktopFileName(APP_ID)
    application.setOrganizationName(STORAGE_ORGANIZATION)
    application.setPalette(QPalette(application.palette()))
    state_dir = args.state_dir or Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_dir / "application.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.warning(None, "Application already running", f"Another {APP_NAME} window is already open. Use that window so one application owns phone automation.")
        return 1
    window = MainWindow(Store(state_dir / "state.json"), adb_path=args.adb)
    window.show()
    QTimer.singleShot(0, window.repair_startup_shortcuts)
    result = application.exec()
    lock.unlock()
    return result
