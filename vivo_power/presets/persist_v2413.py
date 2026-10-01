from ..adb import AdbEngine
from ..run import RunController
from . import DEFAULT_PRESET_ID, DEFAULT_PRESET_NAME, Preset, ResultColumn
from .persist_v2413_gui import PersistV2413Panel


PERSIST_V2413 = Preset(
    id=DEFAULT_PRESET_ID,
    name=DEFAULT_PRESET_NAME,
    description="Verified Android policies and vivo Settings • Main profile (0)",
    scope="Validated scope: vivo X200 Pro • Android 16 • OriginOS 6 • English Settings",
    engine_type=AdbEngine,
    controller_type=RunController,
    panel_type=PersistV2413Panel,
    result_columns=(
        ResultColumn("Background", "RUN_IN_BACKGROUND"),
        ResultColumn("Any background", "RUN_ANY_IN_BACKGROUND"),
        ResultColumn("Keep permissions", "AUTO_REVOKE_PERMISSIONS_IF_UNUSED"),
        ResultColumn("vivo power", "native"),
        ResultColumn("Doze request", "doze", "request_status", "request"),
        ResultColumn("Doze readback", "doze"),
    ),
    setting_labels={
        "RUN_IN_BACKGROUND": "Background execution",
        "RUN_ANY_IN_BACKGROUND": "Any background execution",
        "AUTO_REVOKE_PERMISSIONS_IF_UNUSED": "Keep permissions when unused",
        "native": "vivo background power", "doze": "Doze membership",
    },
    device_observations={"timeout": ("timeout", {"action": "unchanged", "verify_only": True})},
)
