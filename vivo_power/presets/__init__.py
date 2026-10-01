from __future__ import annotations

from dataclasses import dataclass


RUN_LABELS = {
    "running": "Running", "pausing": "Pausing after current operation",
    "stopping": "Stopping after current operation",
    "paused": "Paused", "stopped": "Stopped — completed changes retained",
    "interrupted": "Interrupted", "completed": "Run completed",
    "completed_with_limitations": "Completed with limitations", "failed": "Failed",
}


@dataclass(frozen=True)
class ResultColumn:
    label: str
    setting: str
    value_key: str = "status"
    legacy_key: str | None = None


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    description: str
    scope: str
    engine_type: type
    controller_type: type
    panel_type: type
    result_columns: tuple[ResultColumn, ...]
    setting_labels: dict[str, str]
    device_observations: dict[str, tuple[str, dict]]


DEFAULT_PRESET_ID = "persist-v2413"
DEFAULT_PRESET_NAME = "Persist V2413"
