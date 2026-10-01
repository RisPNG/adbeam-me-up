from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGroupBox, QLabel,
    QLayout, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ..state import create_run, prepare_resume, select_apps
from . import RUN_LABELS


class PersistV2413Panel(QWidget):
    options_changed = Signal()
    run_requested = Signal()

    def __init__(self, preset, parent=None):
        super().__init__(parent)
        self.preset = preset
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setMinimumWidth(330)
        self.setMaximumWidth(400)
        options_panel = QWidget()
        options = QVBoxLayout(options_panel)
        options.setContentsMargins(12, 0, 0, 0)
        options.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        policies = QGroupBox("Per-app policies")
        policy_layout = QVBoxLayout(policies)
        self.background = QCheckBox("Allow Android background execution")
        self.native = QCheckBox("Allow vivo background power usage")
        self.doze = QCheckBox("Request Doze whitelist membership")
        self.permissions = QCheckBox("Keep permissions when unused")
        self.native.setObjectName("native_policy")
        self.policy_boxes = [self.background, self.native, self.doze, self.permissions]
        for box in self.policy_boxes:
            box.setChecked(True)
            policy_layout.addWidget(box)
        self.native_reason = QLabel("Native controls are checked after connecting.")
        self.native_reason.setWordWrap(True)
        policy_layout.addWidget(self.native_reason)
        note = QLabel("Doze acceptance and final membership are separate snapshots. UID-level settings affect apps sharing that UID.")
        note.setWordWrap(True)
        policy_layout.addWidget(note)
        cost = QLabel("Background allowances can use more battery. Run again for future apps; there is no install watcher.")
        cost.setWordWrap(True)
        policy_layout.addWidget(cost)
        options.addWidget(policies)

        global_policy = QGroupBox("Device-wide unused-app timeout")
        global_layout = QVBoxLayout(global_policy)
        self.timeout = QComboBox()
        self.timeout.setObjectName("timeout_action")
        self.timeout.addItem("Leave unchanged", "unchanged")
        self.timeout.addItem("Set effectively unreachable", "set")
        self.timeout.addItem("Remove local override", "clear")
        global_layout.addWidget(self.timeout)
        self.timeout_state = QLabel("Current value: scan to detect")
        self.timeout_state.setWordWrap(True)
        global_layout.addWidget(self.timeout_state)
        timeout_note = QLabel("Affects existing and future apps across profiles, including hibernation, regardless of app filters. Removing the override leaves per-app exemptions. Already revoked permissions are not restored.")
        timeout_note.setWordWrap(True)
        global_layout.addWidget(timeout_note)
        options.addWidget(global_policy)

        run_group = QGroupBox("Run")
        run_layout = QVBoxLayout(run_group)
        self.mode = QComboBox()
        for label, value in (("All selected apps", "all"), ("New apps only", "new"),
                             ("New or changed installations", "changed"), ("Resume unfinished work", "resume"),
                             ("Verify current settings", "verify"), ("Retry unverified work", "retry")):
            self.mode.addItem(label, value)
        run_layout.addWidget(self.mode)
        self.recheck = QCheckBox("Recheck unavailable native controls")
        run_layout.addWidget(self.recheck)
        self.phone_idle = QCheckBox("Phone is unlocked and idle")
        run_layout.addWidget(self.phone_idle)
        self.start = QPushButton("Preview and run…")
        self.start.setObjectName("start")
        run_layout.addWidget(self.start)
        run_note = QLabel("Native verification also uses Settings. Pause finishes the current operation; stop preserves completed changes.")
        run_note.setWordWrap(True)
        run_layout.addWidget(run_note)
        options.addStretch()
        options_scroll = QScrollArea()
        options_scroll.setWidgetResizable(True)
        options_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        options_scroll.setMinimumWidth(330)
        options_scroll.setMaximumWidth(400)
        options_scroll.setWidget(options_panel)
        layout.addWidget(options_scroll, 1)
        layout.addWidget(run_group)

        self.start.clicked.connect(self.run_requested)
        self.mode.currentIndexChanged.connect(self.options_changed)
        self.timeout.currentIndexChanged.connect(self.options_changed)
        self.phone_idle.toggled.connect(self.options_changed)
        self.recheck.toggled.connect(self.options_changed)
        for box in self.policy_boxes:
            box.toggled.connect(self.options_changed)

    def observe_device(self, device):
        self.phone_idle.setChecked(False)
        if device is None:
            self.timeout_state.setText("Current value: scan to detect")
            self.native_reason.setText("Native controls are checked after connecting.")
            return
        timeout = device.get("timeout", {})
        self.timeout_state.setText(f"Effective: {timeout.get('effective_value', 'Unknown')}\nLocal override: {timeout.get('override_value', 'Unknown')}")
        supported = device.get("native_supported", False)
        self.native_reason.setText(device.get("native_reason") or ("Native controls supported. Keep the phone unlocked and idle." if supported else "Native controls are unavailable for this configuration."))
        if not supported:
            self.native.setChecked(False)

    def observe_run(self, run):
        self.phone_idle.setChecked(False)
        timeout = run.get("timeout", {})
        if timeout.get("checked_at"):
            self.timeout_state.setText(f"Effective: {timeout.get('effective_value', 'Unknown')}\nLocal override: {timeout.get('override_value', 'Unknown')}")

    def update_availability(self, busy, device, selected, resumable):
        for widget in [self.mode, self.timeout, self.recheck, self.phone_idle, *self.policy_boxes]:
            widget.setEnabled(not busy)
        self.native.setEnabled(not busy and bool(device and device.get("native_supported")))
        can_resume = bool(resumable and device and not resumable.get("imported")
                          and resumable.get("preset", {}).get("id") == self.preset.id
                          and resumable.get("device", {}).get("identity") == device["identity"]
                          and resumable.get("status") in {"paused", "stopped", "interrupted", "failed"})
        native_wanted = (self.native.isChecked() and bool(selected)) if self.mode.currentData() != "resume" else bool(resumable and "native" in resumable.get("settings", []))
        has_work = can_resume if self.mode.currentData() == "resume" else bool((selected and any(box.isChecked() for box in self.policy_boxes)) or self.timeout.currentData() != "unchanged")
        self.start.setEnabled(not busy and device is not None and has_work and (not native_wanted or self.phone_idle.isChecked()))

    def review_run(self, store, device, inventory, selected, exclude_system, previous_scan, history):
        if self.mode.currentData() == "resume":
            run = deepcopy(history)
            try:
                review = prepare_resume(run, device, inventory, preset_id=self.preset.id)
            except ValueError as error:
                QMessageBox.warning(self, "Cannot resume this run", str(error))
                return
            apps = run.get("apps", [])
            settings = run.get("settings", [])
            timeout_action = run.get("timeout_action", "unchanged")
        else:
            selected = list(selected)
            mode = self.mode.currentData()
            apps = select_apps(inventory, mode, exclude_system, selected,
                               store.latest_results(device, preset_id=self.preset.id))
            settings = []
            if self.background.isChecked():
                settings.extend(["RUN_IN_BACKGROUND", "RUN_ANY_IN_BACKGROUND"])
            if self.permissions.isChecked():
                settings.append("AUTO_REVOKE_PERMISSIONS_IF_UNUSED")
            if self.native.isChecked():
                settings.append("native")
            if self.doze.isChecked():
                settings.append("doze")
            timeout_action = self.timeout.currentData()
            if mode == "verify" and timeout_action != "unchanged":
                QMessageBox.information(self, "Verification mode", "Verification measures the device-wide timeout without changing it. Choose an apply mode to set or remove the override.")
                timeout_action = "unchanged"
            if not apps and timeout_action == "unchanged":
                QMessageBox.information(self, "No work selected", "No installations match the selected mode and packages.")
                return
            run = create_run(device, apps, settings, mode, timeout_action,
                             previous_results=store.latest_results(device, preset_id=self.preset.id),
                             recheck_unavailable=self.recheck.isChecked(),
                             preset_id=self.preset.id, preset_name=self.preset.name)
            review = []
        preview = QDialog(self)
        preview.setWindowTitle("Review the exact run queue")
        preview.resize(740, 560)
        layout = QVBoxLayout(preview)
        explanation = QLabel(f"Preset: {self.preset.name}\nPhone: {device['serial']} • Main profile (0)\n{len(apps)} installations • {RUN_LABELS.get(run.get('status'), 'Ready')}\nPolicies: {', '.join(self.preset.setting_labels.get(setting, setting) for setting in settings) or 'None'}\nDevice-wide timeout: {timeout_action}")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        if review:
            note = QLabel("Resume review:\n" + "\n".join(review))
            note.setWordWrap(True)
            layout.addWidget(note)
        if not previous_scan and self.mode.currentData() in {"new", "changed"}:
            layout.addWidget(QLabel("There is no previous baseline; every observed installation is new."))
        listing = QPlainTextEdit()
        listing.setReadOnly(True)
        entries = []
        for app in apps:
            entries.append(f"{app.get('label') or app['package']} — {app['package']}")
            if app.get("shared_uid_packages"):
                entries.append("  UID-level changes also affect: " + ", ".join(app["shared_uid_packages"]))
        listing.setPlainText("\n".join(entries) or "Device-wide timeout action only; no per-app operations.")
        layout.addWidget(listing)
        notice = QLabel("This is the complete frozen queue, including checked apps hidden by the search or filter. Stop preserves completed changes. Native work requires the phone to remain unlocked and idle.")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Start run")
        buttons.accepted.connect(preview.accept)
        buttons.rejected.connect(preview.reject)
        layout.addWidget(buttons)
        if preview.exec() != QDialog.DialogCode.Accepted:
            return
        return run
