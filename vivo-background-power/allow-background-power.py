import argparse
import datetime
from functools import partial
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vivo_power.adb import AdbEngine, AdbError
from vivo_power.run import RunController, recover_phone
from vivo_power.state import SETTINGS, Store, create_run, select_apps


parser = argparse.ArgumentParser(
    description="Apply and verify Android background policies and vivo's native background power setting."
)
parser.add_argument("--serial", help="ADB serial when multiple devices are connected")
parser.add_argument("--adb", help="Path to adb")
parser.add_argument("--package", action="append", default=[], help="Process only this installed package; repeat for several")
parser.add_argument("--exclude-system", action="store_true", help="Exclude system packages from all per-package changes")
parser.add_argument("--unreachable-unused-timeout", action="store_true", help="Set a persistent device-wide unused-app timeout override for existing and future apps")
parser.add_argument("--report", type=Path, help="Save the result report here")
parser.add_argument("--verify-only", action="store_true", help="Measure selected settings without changing policies")
parser.add_argument("--no-native", action="store_true", help="Run Android policies without navigating native Settings")
parser.add_argument("--state", type=Path, help="Path to the shared GUI/CLI checkpoint store")
args = parser.parse_args()

engine = AdbEngine(args.adb, args.serial)
store = Store(args.state)
try:
    device = engine.device()
    if not device["ready"]:
        parser.error(device["reason"])
    if not args.no_native and not device["native_supported"]:
        parser.error(device["native_reason"] + "; use --no-native for Android policies")
    recover_phone(store, engine, device)
    engine.resource_callback = partial(store.record_scan_resource, device, engine)
    inventory = engine.inventory()
    unknown = set(args.package) - {app["package"] for app in inventory}
    if unknown:
        parser.error("Not installed in the main profile: " + ", ".join(sorted(unknown)))
    selected = select_apps(inventory, "all", args.exclude_system, set(args.package) if args.package else None)
    settings = [setting for setting in SETTINGS if setting != "native" or not args.no_native]
    run = create_run(device, selected, settings, "verify" if args.verify_only else "all",
                     "set" if args.unreachable_unused_timeout else "unchanged")
    store.save_scan(device, inventory)
    store.save_run(run)
except (AdbError, OSError, RuntimeError, ValueError) as error:
    parser.error(str(error))

started = datetime.datetime.now().astimezone()
report_path = args.report or Path.cwd() / ("vivo-background-power-" + started.strftime("%Y%m%d-%H%M%S") + ".json")
print(f"Device {device['serial']}: {len(selected)} selected main-profile packages.", flush=True)
if not args.no_native:
    print("Unlock the phone and leave it idle. Native checks navigate Settings.", flush=True)
print("Ctrl+C saves completed work and stops the owned session. Applied changes are retained.", flush=True)

controller = RunController(store, engine, run, progress_callback=lambda snapshot:
                           print(f"{snapshot['stage']}: {snapshot['current'][0]}", flush=True) if snapshot.get("current") else None)
exit_code = 0
try:
    controller.execute()
except KeyboardInterrupt:
    controller.stop()
    run["status"] = "interrupted"
    run["error"] = "Interrupted by Ctrl+C; completed changes are retained."
    if run.get("current"):
        package, setting = run.pop("current")
        run["packages"][package]["results"][setting] = {"status": "not_checked", "error": "Interrupted before verification"}
    run["resources"] = engine.pending_cleanup
    run["phone_available"] = not engine.pending_cleanup
    store.save_run(run)
    exit_code = 130

results = [result for record in run["packages"].values() for result in record["results"].values()]
if exit_code == 0:
    if run["status"] == "interrupted" or run["timeout"].get("status") == "failed" or any(result.get("status") == "failed" for result in results):
        exit_code = 1
    elif any(record["results"]["doze"].get("request") not in {"accepted", "not_requested"}
             or record["results"]["doze"].get("membership") is not True for record in run["packages"].values()):
        exit_code = 2
run["exit_code"] = exit_code
store.save_run(run)
store.export_report(run, report_path)
for setting in settings:
    counts = {}
    for record in run["packages"].values():
        status = record["results"][setting].get("status", "not_checked")
        counts[status] = counts.get(status, 0) + 1
    print(setting + ": " + ", ".join(f"{count} {status.replace('_', ' ')}" for status, count in sorted(counts.items())), flush=True)
print("Run: " + run["status"].replace("_", " "), flush=True)
if run.get("error"):
    print(run["error"], file=sys.stderr)
if run["resources"]:
    print("Phone cleanup is unconfirmed. Reconnect this phone and use Resume in the GUI.", file=sys.stderr)
print(f"Report: {report_path.resolve()}", flush=True)
sys.exit(exit_code)
