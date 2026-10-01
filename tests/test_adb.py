import subprocess
import shlex
import unittest
from unittest.mock import patch

from vivo_power.adb import AdbEngine, AdbError


class FakeAdb:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, command, **kwargs):
        arguments = command[3:]
        if arguments[0] == "shell":
            arguments = ["shell"] + shlex.split(arguments[1])
        self.calls.append(arguments)
        expected, stdout, stderr, code = self.responses.pop(0)
        if expected is not None and arguments != expected:
            raise AssertionError((arguments, expected))
        if "timeout" not in kwargs:
            raise AssertionError("Every ADB invocation must have a timeout")
        return subprocess.CompletedProcess(command, code, stdout, stderr)


class AdbPoliciesTest(unittest.TestCase):
    def test_background_repairs_uid_conflict_and_verifies(self):
        before = "Uid mode: RUN_IN_BACKGROUND: ignore\nRUN_IN_BACKGROUND: default\n"
        conflict = "Uid mode: RUN_IN_BACKGROUND: ignore\nRUN_IN_BACKGROUND: allow\n"
        allowed = "Uid mode: RUN_IN_BACKGROUND: allow\nRUN_IN_BACKGROUND: allow\n"
        fake = FakeAdb([
            (["shell", "cmd", "appops", "get", "--user", "0", "com.test", "RUN_IN_BACKGROUND"], before, "", 0),
            (["shell", "cmd", "appops", "set", "--user", "0", "com.test", "RUN_IN_BACKGROUND", "allow"], "", "", 0),
            (None, conflict, "", 0),
            (["shell", "cmd", "appops", "set", "--user", "0", "--uid", "com.test", "RUN_IN_BACKGROUND", "allow"], "", "", 0),
            (None, allowed, "", 0),
            (["shell", "cmd", "appops", "write-settings"], "", "", 0),
        ])
        with patch("vivo_power.adb.subprocess.run", fake):
            result = AdbEngine("adb", "SERIAL").perform("com.test", "RUN_IN_BACKGROUND")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["observed"]["uid_mode"], "allow")
        self.assertTrue(result["checked_at"])
        self.assertFalse(fake.responses)

    def test_uid_auto_revoke_requires_uid_ignore(self):
        fake = FakeAdb([
            (None, "AUTO_REVOKE_PERMISSIONS_IF_UNUSED: ignore\n", "", 0),
            (["shell", "cmd", "appops", "set", "--user", "0", "--uid", "com.test", "AUTO_REVOKE_PERMISSIONS_IF_UNUSED", "ignore"], "", "", 0),
            (None, "Uid mode: AUTO_REVOKE_PERMISSIONS_IF_UNUSED: ignore\n", "", 0),
            (None, "", "", 0),
        ])
        with patch("vivo_power.adb.subprocess.run", fake):
            result = AdbEngine("adb", "SERIAL").perform("com.test", "AUTO_REVOKE_PERMISSIONS_IF_UNUSED")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["observed"]["uid_mode"], "ignore")

    def test_verified_existing_policy_is_already_configured(self):
        fake = FakeAdb([(None, "RUN_ANY_IN_BACKGROUND: allow\n", "", 0)])
        with patch("vivo_power.adb.subprocess.run", fake):
            result = AdbEngine("adb", "SERIAL").perform("com.test", "RUN_ANY_IN_BACKGROUND")
        self.assertEqual(result["status"], "already_configured")
        self.assertEqual(len(fake.calls), 1)

    def test_verify_only_does_not_repair_uid_conflict(self):
        fake = FakeAdb([(None, "Uid mode: RUN_IN_BACKGROUND: ignore\nRUN_IN_BACKGROUND: allow\n", "", 0)])
        with patch("vivo_power.adb.subprocess.run", fake):
            result = AdbEngine("adb", "SERIAL").perform("com.test", "RUN_IN_BACKGROUND", verify_only=True)
        self.assertEqual(result["status"], "not_present")
        self.assertEqual(len(fake.calls), 1)
        self.assertNotIn("set", fake.calls[0])

    def test_doze_acceptance_and_later_membership_are_separate(self):
        fake = FakeAdb([
            (["shell", "cmd", "deviceidle", "whitelist", "+com.test"], "Added: com.test\n", "", 0),
            (None, "system-excidle,com.test,1000\n", "", 0),
            (None, "user,com.other,10001\n", "", 0),
        ])
        engine = AdbEngine("adb", "SERIAL")
        with patch("vivo_power.adb.subprocess.run", fake):
            applied = engine.perform("com.test", "doze")
            audited = engine.perform("com.test", "doze", verify_only=True)
        self.assertEqual(applied["request"], "accepted")
        self.assertFalse(applied["membership"])
        self.assertEqual(applied["status"], "not_present")
        self.assertEqual(audited["request"], "not_requested")
        self.assertFalse(audited["membership"])
        self.assertEqual(len(fake.calls), 3)

    def test_doze_zero_exit_unknown_package_is_rejected(self):
        fake = FakeAdb([(None, "Unknown package: com.test\n", "", 0), (None, "user,com.other,10001\n", "", 0)])
        with patch("vivo_power.adb.subprocess.run", fake):
            result = AdbEngine("adb", "SERIAL").perform("com.test", "doze")
        self.assertEqual(result["request"], "rejected_unknown_package")
        self.assertEqual(result["status"], "rejected")

    def test_doze_membership_never_turns_unknown_response_into_acceptance(self):
        fake = FakeAdb([(None, "", "", 0), (None, "user,com.test,10001\n", "", 0)])
        with patch("vivo_power.adb.subprocess.run", fake):
            result = AdbEngine("adb", "SERIAL").perform("com.test", "doze")
        self.assertEqual(result["request"], "unconfirmed")
        self.assertEqual(result["status"], "not_checked")
        self.assertTrue(result["membership"])

    def test_timeout_unchanged_preserves_existing_override_without_writes(self):
        fake = FakeAdb([(None, "9223372036854775807\n", "", 0), (None, "9223372036854775807\n", "", 0)])
        with patch("vivo_power.adb.subprocess.run", fake):
            result = AdbEngine("adb", "SERIAL").timeout()
        self.assertEqual(result["override"], "9223372036854775807")
        self.assertEqual(result["status"], "unchanged")
        self.assertTrue(all("get" in command for command in fake.calls))

    def test_timeout_set_verifies_both_values_without_apps(self):
        fake = FakeAdb([(None, "1000", "", 0), (None, "null", "", 0),
                        (["shell", "device_config", "override", "permissions", "auto_revoke_unused_threshold_millis2", "9223372036854775807"], "", "", 0),
                        (None, "9223372036854775807", "", 0), (None, "9223372036854775807", "", 0)])
        with patch("vivo_power.adb.subprocess.run", fake):
            result = AdbEngine("adb", "SERIAL").timeout("set")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["effective"], result["override"])

    def test_timeout_verify_only_never_sets_override(self):
        fake = FakeAdb([(None, "1000", "", 0), (None, "null", "", 0)])
        with patch("vivo_power.adb.subprocess.run", fake):
            result = AdbEngine("adb", "SERIAL").timeout("set", verify_only=True)
        self.assertEqual(result["status"], "not_present")
        self.assertEqual(len(fake.calls), 2)

    def test_timeout_clear_checks_stored_override_removed(self):
        fake = FakeAdb([(None, "9223372036854775807", "", 0), (None, "9223372036854775807", "", 0),
                        (["shell", "device_config", "clear_override", "permissions", "auto_revoke_unused_threshold_millis2"], "", "", 0),
                        (None, "1000", "", 0), (None, "null", "", 0)])
        with patch("vivo_power.adb.subprocess.run", fake):
            result = AdbEngine("adb", "SERIAL").timeout("clear")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["effective"], "1000")
        self.assertEqual(result["override"], "null")

    def test_failed_command_preserves_details(self):
        fake = FakeAdb([(None, "", "device offline", 1)])
        with patch("vivo_power.adb.subprocess.run", fake):
            result = AdbEngine("adb", "SERIAL").perform("com.test", "RUN_IN_BACKGROUND")
        self.assertEqual(result["status"], "failed")
        self.assertIn("device offline", result["error"])
        self.assertIsNone(result["checked_at"])

    def test_cleanup_does_not_target_another_device(self):
        engine = AdbEngine("adb", "SERIAL")
        with patch("vivo_power.adb.subprocess.run") as command:
            unresolved = engine.recover_cleanup([{"serial": "OTHER", "remote_dir": "/data/local/tmp/vivo-background-" + "a" * 32}])
        self.assertEqual(len(unresolved), 1)
        command.assert_not_called()

    def test_cleanup_keeps_disconnected_session_visible(self):
        fake = FakeAdb([(None, "", "device offline", 1)])
        engine = AdbEngine("adb", "SERIAL")
        resource = {"serial": "SERIAL", "remote_dir": "/data/local/tmp/vivo-background-" + "a" * 32, "launched": True}
        with patch("vivo_power.adb.subprocess.run", fake):
            unresolved = engine.recover_cleanup([resource])
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(engine.pending_cleanup[0]["remote_dir"], resource["remote_dir"])


    def test_inventory_is_main_profile_and_does_not_navigate(self):
        app = {"package": "com.test", "label": "Test", "uid": 10001, "system": False,
               "version_code": 2, "first_install_time": 100, "last_update_time": 200}
        import json
        fake = FakeAdb([(None, "", "", 0), (None, "", "", 0),
                        (None, "VIVO_PID 123\nVIVO_APP " + json.dumps(app) + "\nVIVO_INVENTORY_COMPLETE 1\n", "", 0),
                        (None, "device\n", "", 0), (None, "PID ARGS\n", "", 0),
                        (None, "123", "", 0), (None, "", "No such file", 1), (None, "", "", 1),
                        (None, "PID ARGS\n", "", 0),
                        (None, "", "", 0), (None, "", "", 0)])
        resources = []
        engine = AdbEngine("adb", "SERIAL", resources.append)
        engine._device_info = {"identity": "identity", "ready": True}
        with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value=engine._device_info):
            inventory = engine.inventory()
        self.assertEqual(inventory, [dict(app, shared_uid_packages=[])])
        launched = fake.calls[2]
        self.assertIn("phonepolicy.PackageInventory --user 0", launched[-1])
        self.assertIn("--nice-name=" + resources[0]["remote_dir"], launched[-1])
        self.assertIn("--pidfile " + resources[0]["pidfile"], launched[-1])
        self.assertFalse(any("uiautomator" in command for command in fake.calls))
        self.assertFalse(engine.pending_cleanup)
        self.assertIsNone(resources[-1])
        self.assertTrue(resources[1]["launched"])
        self.assertEqual(resources[2]["pid"], 123)

    def test_native_verify_requires_closed_screens_and_persisted_allow(self):
        import json
        outcome = {"package": "com.test", "before": "allow", "after": "allow", "status": "verified"}
        output = "VIVO_PID 123\nVIVO_RESULT " + json.dumps(outcome) + "\nVIVO_END {\"phone_free\":true}\n"
        fake = FakeAdb([(None, "", "", 0), (None, "", "", 0), (None, "", "", 0),
                        (None, output, "", 0), (None, "device\n", "", 0), (None, "PID ARGS\n", "", 0),
                        (None, "123", "", 0), (None, "", "No such file", 1), (None, "", "", 1),
                        (None, "PID ARGS\n", "", 0), (None, "", "", 0), (None, "", "", 0)])
        resources = []
        engine = AdbEngine("adb", "SERIAL", resources.append)
        engine._device_info = {"identity": "identity", "ready": True, "native_supported": True}
        engine._inventory = {"com.test": {"label": "Test App"}}
        with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value=engine._device_info):
            result = engine.perform("com.test", "native", verify_only=True)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["observed"], "allow")
        self.assertIn("verify", fake.calls[3])
        self.assertIn("Test App", fake.calls[3])
        self.assertIn("ADBEAM_SESSION=" + resources[0]["remote_dir"], fake.calls[3])
        self.assertFalse(engine.pending_cleanup)
        self.assertIsNone(resources[-1])

    def test_native_does_not_accept_result_without_phone_free(self):
        fake = FakeAdb([(None, "", "", 0), (None, "", "", 0), (None, "", "", 0),
                        (None, 'VIVO_RESULT {"package":"com.test","status":"verified","after":"allow"}\n', "", 0),
                        (None, "device\n", "", 0),
                        (None, "PID ARGS\n", "", 0), (None, "", "No such file", 1),
                        (None, "PID ARGS\n", "", 0), (None, "", "", 0), (None, "", "", 0)])
        engine = AdbEngine("adb", "SERIAL")
        engine._device_info = {"identity": "identity", "ready": True, "native_supported": True}
        with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value=engine._device_info):
            result = engine.perform("com.test", "native", verify_only=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("closed-screen", result["error"])
        self.assertFalse(engine.pending_cleanup)

    def test_interrupted_inventory_recovers_pid_and_stops_exact_named_process(self):
        directory = "/data/local/tmp/vivo-background-" + "a" * 32
        resources = []
        engine = AdbEngine("adb", "SERIAL", resources.append)
        device = {"identity": "identity", "ready": True}
        responses = [subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 0, "", ""),
                     AdbError("inventory connection lost"), subprocess.CompletedProcess([], 0, "device\n", ""),
                     subprocess.CompletedProcess([], 0, "PID ARGS\n123 " + directory + "\n", ""),
                     subprocess.CompletedProcess([], 0, "123", ""),
                     subprocess.CompletedProcess([], 0, directory + "\0" * 8, ""),
                     subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 1, "", "No such file"),
                     subprocess.CompletedProcess([], 1, "", ""), subprocess.CompletedProcess([], 0, "PID ARGS\n", ""),
                     subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 0, "", "")]
        with patch.object(engine, "_adb", side_effect=responses) as command, patch.object(engine, "device", return_value=device), patch("vivo_power.adb.uuid.uuid4") as token, patch("vivo_power.adb.time.sleep") as sleep:
            token.return_value.hex = "a" * 32
            with self.assertRaisesRegex(AdbError, "inventory connection lost"):
                engine.inventory()
        calls = [call.args[0] for call in command.call_args_list]
        self.assertIn(["shell", "kill", "-TERM", "123"], calls)
        self.assertLess(calls.index(["shell", "test", "-d", "/proc/123"]), calls.index(["shell", "rm", "-rf", directory]))
        self.assertFalse(engine.pending_cleanup)
        self.assertIsNone(resources[-1])
        sleep.assert_not_called()

    def test_inventory_pidfile_does_not_authorize_killing_reused_pid(self):
        directory = "/data/local/tmp/vivo-background-" + "a" * 32
        fake = FakeAdb([(None, "device\n", "", 0), (None, "PID ARGS\n123 unrelated\n", "", 0),
                        (None, "123", "", 0), (None, "unrelated-command\0", "", 0),
                        (None, "PID ARGS\n", "", 0), (None, "", "", 0), (None, "", "", 0)])
        engine = AdbEngine("adb", "SERIAL")
        resource = {"serial": "SERIAL", "identity": "identity", "kind": "inventory", "remote_dir": directory,
                    "launched": True, "pidfile": directory + "/pid"}
        with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value={"identity": "identity", "ready": True}), patch("vivo_power.adb.time.sleep") as sleep:
            self.assertFalse(engine.recover_cleanup([resource]))
        self.assertFalse(any("kill" in command for command in fake.calls))
        sleep.assert_not_called()

    def test_native_cleanup_stops_token_owned_process_with_rewritten_argv(self):
        directory = "/data/local/tmp/vivo-background-" + "a" * 32
        fake = FakeAdb([(None, "device\n", "", 0), (None, "PID ARGS\n123 uiautomator\n", "", 0),
                        (None, "123", "", 0), (None, "uiautomator\0" * 8, "", 0),
                        (None, "PATH=/system/bin\0ADBEAM_SESSION=" + directory + "\0CLASSPATH=helper.jar\0", "", 0),
                        (None, "", "", 0), (None, "", "No such file", 1), (None, "", "", 1),
                        (None, "PID ARGS\n", "", 0), (None, "", "", 0), (None, "", "", 0)])
        engine = AdbEngine("adb", "SERIAL")
        resource = {"serial": "SERIAL", "identity": "identity", "kind": "native", "remote_dir": directory,
                    "launched": True, "pidfile": directory + "/pid"}
        with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value={"identity": "identity", "ready": True}), patch("vivo_power.adb.time.sleep") as sleep:
            self.assertFalse(engine.recover_cleanup([resource]))
        self.assertIn(["shell", "kill", "-TERM", "123"], fake.calls)
        self.assertLess(fake.calls.index(["shell", "test", "-d", "/proc/123"]), fake.calls.index(["shell", "rm", "-rf", directory]))
        sleep.assert_not_called()

    def test_native_cleanup_requires_exact_environment_token(self):
        directory = "/data/local/tmp/vivo-background-" + "a" * 32
        for environment in ("ADBEAM_SESSION=" + directory + "-other\0", "OTHER_ADBEAM_SESSION=" + directory + "\0"):
            with self.subTest(environment=environment):
                responses = [(None, "device\n", "", 0), (None, "PID ARGS\n123 uiautomator\n", "", 0),
                             (None, "uiautomator\0", "", 0), (None, environment, "", 0)]
                if environment.startswith("ADBEAM_SESSION="):
                    responses += [(None, "PID ARGS\n", "", 0), (None, "", "", 0), (None, "", "", 0)]
                fake = FakeAdb(responses)
                engine = AdbEngine("adb", "SERIAL")
                resource = {"serial": "SERIAL", "identity": "identity", "kind": "native", "remote_dir": directory,
                            "launched": True, "pid": 123}
                with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value={"identity": "identity", "ready": True}):
                    unresolved = engine.recover_cleanup([resource])
                self.assertFalse(any("kill" in command for command in fake.calls))
                self.assertEqual(bool(unresolved), not environment.startswith("ADBEAM_SESSION="))

    def test_native_cleanup_cannot_release_owned_process_after_failed_kills(self):
        directory = "/data/local/tmp/vivo-background-" + "a" * 32
        ownership = [(None, "uiautomator\0", "", 0), (None, "ADBEAM_SESSION=" + directory + "\0", "", 0)]
        fake = FakeAdb([(None, "device\n", "", 0), (None, "PID ARGS\n123 uiautomator\n", "", 0),
                        (None, "", "No such file", 1)] + ownership + [(None, "", "permission denied", 1)]
                       + ownership * 5 + [(None, "", "permission denied", 1)] + ownership)
        engine = AdbEngine("adb", "SERIAL")
        resource = {"serial": "SERIAL", "identity": "identity", "kind": "native", "remote_dir": directory,
                    "launched": True, "pidfile": directory + "/pid"}
        with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value={"identity": "identity", "ready": True}), patch("vivo_power.adb.time.sleep"):
            unresolved = engine.recover_cleanup([resource])
        self.assertEqual(len(unresolved), 1)
        self.assertIn("exact owned", unresolved[0]["cleanup_error"])
        self.assertIn(["shell", "kill", "-TERM", "123"], fake.calls)
        self.assertIn(["shell", "kill", "-KILL", "123"], fake.calls)
        self.assertFalse(any("rm" in command for command in fake.calls))

    def test_native_cleanup_keeps_existing_helper_pending_when_environment_is_unreadable(self):
        directory = "/data/local/tmp/vivo-background-" + "a" * 32
        fake = FakeAdb([(None, "device\n", "", 0), (None, "PID ARGS\n123 uiautomator\n", "", 0),
                        (None, "uiautomator\0", "", 0), (None, "", "Permission denied", 1), (None, "", "", 0)])
        engine = AdbEngine("adb", "SERIAL")
        resource = {"serial": "SERIAL", "identity": "identity", "kind": "native", "remote_dir": directory,
                    "launched": True, "pid": 123}
        with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value={"identity": "identity", "ready": True}):
            unresolved = engine.recover_cleanup([resource])
        self.assertEqual(len(unresolved), 1)
        self.assertIn("ownership cannot be read", unresolved[0]["cleanup_error"])
        self.assertFalse(any("kill" in command or "rm" in command for command in fake.calls))

    def test_legacy_opaque_native_helper_remains_pending_even_after_acknowledgement(self):
        directory = "/data/local/tmp/vivo-background-" + "a" * 32
        fake = FakeAdb([(None, "device\n", "", 0), (None, "PID ARGS\n123 uiautomator\n", "", 0),
                        (None, "123", "", 0), (None, "uiautomator\0", "", 0), (None, "PATH=/system/bin\0", "", 0)])
        engine = AdbEngine("adb", "SERIAL")
        resource = {"serial": "SERIAL", "identity": "identity", "kind": "native", "remote_dir": directory,
                    "launched": True, "pidfile": directory + "/pid", "completed_ack": True}
        with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value={"identity": "identity", "ready": True}):
            unresolved = engine.recover_cleanup([resource])
        self.assertEqual(len(unresolved), 1)
        self.assertIn("ownership", unresolved[0]["cleanup_error"])
        self.assertFalse(any("kill" in command or "rm" in command for command in fake.calls))

    def test_legacy_inventory_without_pid_waits_for_full_deadline(self):
        directory = "/data/local/tmp/vivo-background-" + "a" * 32
        fake = FakeAdb([(None, "device\n", "", 0), (None, "PID ARGS\n", "", 0),
                        (None, "PID ARGS\n", "", 0), (None, "", "", 0), (None, "", "", 0)])
        engine = AdbEngine("adb", "SERIAL")
        resource = {"serial": "SERIAL", "identity": "identity", "kind": "inventory", "remote_dir": directory, "launched": True}
        with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value={"identity": "identity", "ready": True}), patch("vivo_power.adb.time.sleep") as sleep:
            self.assertFalse(engine.recover_cleanup([resource]))
        sleep.assert_called_once_with(35)

    def test_legacy_inventory_still_active_after_deadline_remains_pending(self):
        directory = "/data/local/tmp/vivo-background-" + "a" * 32
        for commandline in ("phonepolicy.PackageInventory\0", "\0".join(("app_process", "/system/bin", "phonepolicy.PackageInventory", "--user", "0", ""))):
            with self.subTest(commandline=commandline):
                fake = FakeAdb([(None, "device\n", "", 0),
                                (None, "PID ARGS\n123 " + commandline.replace("\0", " ") + "\n", "", 0),
                                (None, commandline, "", 0)])
                engine = AdbEngine("adb", "SERIAL")
                resource = {"serial": "SERIAL", "identity": "identity", "kind": "inventory", "remote_dir": directory, "launched": True}
                with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value={"identity": "identity", "ready": True}), patch("vivo_power.adb.time.sleep") as sleep:
                    unresolved = engine.recover_cleanup([resource])
                self.assertEqual(len(unresolved), 1)
                self.assertIn("does not identify its session", unresolved[0]["cleanup_error"])
                self.assertFalse(any("kill" in command or "rm" in command for command in fake.calls))
                sleep.assert_called_once_with(35)

    def test_cleanup_never_kills_reused_pid(self):
        fake = FakeAdb([(None, "device\n", "", 0), (None, "PID ARGS\n123 unrelated\n", "", 0),
                        (None, "unrelated-command", "", 0), (None, "PID ARGS\n", "", 0), (None, "", "", 0), (None, "", "", 0)])
        engine = AdbEngine("adb", "SERIAL")
        engine._device_info = {"identity": "identity", "ready": True}
        resource = {"serial": "SERIAL", "identity": "identity", "remote_dir": "/data/local/tmp/vivo-background-" + "a" * 32,
                    "launched": True, "pid": 123}
        with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value=engine._device_info):
            unresolved = engine.recover_cleanup([resource])
        self.assertFalse(unresolved)
        self.assertFalse(any("kill" in command for command in fake.calls))


    def test_cleanup_does_not_trust_ps_absence_after_failed_kill(self):
        directory = "/data/local/tmp/vivo-background-" + "a" * 32
        commandline = "app_process " + directory + "/vivo-background-power.jar"
        fake = FakeAdb([(None, "device\n", "", 0), (None, "PID ARGS\n", "", 0),
                        (None, commandline, "", 0), (None, "", "permission denied", 1)]
                       + [(None, commandline, "", 0)] * 5
                       + [(None, "", "permission denied", 1), (None, commandline, "", 0)])
        engine = AdbEngine("adb", "SERIAL")
        engine._device_info = {"identity": "identity", "ready": True}
        resource = {"serial": "SERIAL", "identity": "identity", "remote_dir": directory, "launched": True, "pid": 123}
        with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value=engine._device_info), patch("vivo_power.adb.time.sleep"):
            unresolved = engine.recover_cleanup([resource])
        self.assertEqual(len(unresolved), 1)
        self.assertIn("exact", unresolved[0]["cleanup_error"])
        self.assertFalse(any("rm" in command for command in fake.calls))

    def test_unknown_pid_waits_for_saved_lease_before_availability(self):
        directory = "/data/local/tmp/vivo-background-" + "a" * 32
        fake = FakeAdb([(None, "device\n", "", 0), (None, "100", "", 0), (None, "105", "", 0),
                        (None, "PID ARGS\n", "", 0), (None, "PID ARGS\n", "", 0),
                        (None, "", "", 0), (None, "", "", 0)])
        engine = AdbEngine("adb", "SERIAL")
        engine._device_info = {"identity": "identity", "ready": True}
        resource = {"serial": "SERIAL", "identity": "identity", "remote_dir": directory,
                    "launched": True, "heartbeat": directory + "/heartbeat"}
        with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value=engine._device_info), patch("vivo_power.adb.time.sleep") as sleep:
            unresolved = engine.recover_cleanup([resource])
        self.assertFalse(unresolved)
        sleep.assert_called_once_with(15)

    def test_cleanup_refreshes_identity_before_touching_saved_session(self):
        engine = AdbEngine("adb", "SERIAL")
        engine._device_info = {"identity": "saved", "ready": True}
        resource = {"serial": "SERIAL", "identity": "saved", "remote_dir": "/data/local/tmp/vivo-background-" + "a" * 32}
        fake = FakeAdb([(None, "device\n", "", 0)])
        with patch("vivo_power.adb.subprocess.run", fake), patch.object(engine, "device", return_value={"identity": "other", "ready": True}) as fresh:
            unresolved = engine.recover_cleanup([resource])
        self.assertEqual(len(unresolved), 1)
        fresh.assert_called_once()
        self.assertEqual(len(fake.calls), 1)


    def test_tested_phone_identity_is_stable_across_transport_serials(self):
        properties = "\n".join(("[ro.serialno]: [HARDWARE]", "[ro.product.model]: [V2413]",
                                 "[ro.product.model.bbk]: [PD2405F_EX]", "[ro.build.version.sdk]: [36]",
                                 "[ro.vivo.os.version]: [7.0]", "[ro.vivo.os.build.display.id]: [OriginOS 6]",
                                 "[persist.sys.locale]: [en-US]", "[ro.build.version.release]: [16]"))
        identities = []
        for serial in ("USB", "TCP"):
            engine = AdbEngine("adb", serial)
            outputs = [subprocess.CompletedProcess([], 0, properties, ""),
                       subprocess.CompletedProcess([], 0, "package:/system/power.apk", ""),
                       subprocess.CompletedProcess([], 0, "versionCode=6", ""),
                       subprocess.CompletedProcess([], 0, "0\n", "")]
            with patch.object(engine, "devices", return_value=[{"serial": serial, "state": "device"}]), patch.object(engine, "_adb", side_effect=outputs):
                info = engine.device()
            identities.append(info["identity"])
            self.assertEqual(info["name"], "vivo X200 Pro")
            self.assertEqual(info["origin_os"], "OriginOS 6")
            self.assertTrue(info["native_supported"])
            self.assertEqual(info["profile"], 0)
        self.assertEqual(identities[0], identities[1])

    def test_native_unsupported_does_not_block_android_policy(self):
        engine = AdbEngine("adb", "SERIAL")
        engine._device_info = {"ready": True, "native_supported": False, "native_reason": "Unsupported native controls"}
        fake = FakeAdb([(None, "RUN_IN_BACKGROUND: allow\n", "", 0)])
        with patch("vivo_power.adb.subprocess.run", fake):
            native = engine.perform("com.test", "native")
            background = engine.perform("com.test", "RUN_IN_BACKGROUND", verify_only=True)
        self.assertEqual(native["status"], "unavailable")
        self.assertEqual(background["status"], "verified")
        self.assertEqual(len(fake.calls), 1)


    def test_second_engine_cannot_start_native_for_locked_phone(self):
        from PySide6.QtCore import QLockFile
        from pathlib import Path
        import tempfile
        lock = QLockFile(str(Path(tempfile.gettempdir()) / "vivo-background-native-lock-test.lock"))
        lock.setStaleLockTime(0)
        self.assertTrue(lock.tryLock(0))
        engine = AdbEngine("adb", "SERIAL")
        engine._device_info = {"identity": "lock-test", "ready": True, "native_supported": True}
        try:
            with patch("vivo_power.adb.subprocess.run") as commands:
                result = engine.perform("com.test", "native", verify_only=True)
            self.assertEqual(result["status"], "failed")
            self.assertIn("another process", result["error"])
            commands.assert_not_called()
            self.assertTrue(engine._native_lock.acquire(blocking=False))
            engine._native_lock.release()
        finally:
            lock.unlock()


    def test_future_firmware_and_other_profiles_are_not_native_validated(self):
        for sdk, origin, user in ((37, "OriginOS 6", "0"), (36, "OriginOS 7", "0"), (36, "OriginOS 6", "10")):
            properties = "\n".join(("[ro.product.model]: [V2413]", "[ro.product.model.bbk]: [PD2405F_EX]",
                                     f"[ro.build.version.sdk]: [{sdk}]", f"[ro.vivo.os.build.display.id]: [{origin}]",
                                     "[persist.sys.locale]: [en-US]"))
            engine = AdbEngine("adb", "SERIAL")
            outputs = [subprocess.CompletedProcess([], 0, properties, ""),
                       subprocess.CompletedProcess([], 0, "package:/system/power.apk", ""),
                       subprocess.CompletedProcess([], 0, "versionCode=6", ""),
                       subprocess.CompletedProcess([], 0, user, "")]
            with patch.object(engine, "devices", return_value=[{"serial": "SERIAL", "state": "device"}]), patch.object(engine, "_adb", side_effect=outputs):
                info = engine.device()
            self.assertFalse(info["native_supported"])
            self.assertEqual(info["ready"], user == "0")
            self.assertEqual(info["active_profile"], int(user))
            if user != "0":
                self.assertIn("main profile", info["reason"])


if __name__ == "__main__":
    unittest.main()
