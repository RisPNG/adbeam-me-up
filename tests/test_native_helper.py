import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


class NativeBundleTests(unittest.TestCase):
    def test_bundle_contains_matching_sources_and_packaged_copy(self):
        bundle = ROOT / "presets" / "vivo-background-power" / "vivo-background-power.jar"
        with zipfile.ZipFile(bundle) as archive:
            self.assertTrue(archive.read("classes.dex").startswith(b"dex\n"))
            checksums = archive.read("META-INF/sources.sha256").decode().splitlines()
            for line in checksums:
                checksum, name = line.split()
                self.assertEqual(checksum, hashlib.sha256((bundle.parent / name).read_bytes()).hexdigest())
        self.assertEqual(bundle.read_bytes(), (ROOT / "vivo_power" / "resources" / bundle.name).read_bytes())


@unittest.skipUnless(shutil.which("javac") and shutil.which("java") and os.name != "nt", "JDK and a POSIX test command are required")
class NativeNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="vivo-native-tests-")
        cls.directory = Path(cls.temporary.name)
        sources = {
            "android/os/Bundle.java": '''package android.os;
public class Bundle extends java.util.HashMap<String, String> {
    public String getString(String name) { return get(name); }
    public String getString(String name, String defaultValue) { return getOrDefault(name, defaultValue); }
}''',
            "android/os/Process.java": '''package android.os;
public class Process {
    public static int myPid() { return 12345; }
    public static void killProcess(int pid) { System.exit(99); }
}''',
            "android/os/SystemClock.java": '''package android.os;
public class SystemClock {
    public static long elapsedRealtime() { return System.nanoTime() / 1000000; }
    public static void sleep(long millis) {
        try { Thread.sleep(millis); } catch (InterruptedException finished) { Thread.currentThread().interrupt(); }
    }
}''',
            "android/os/Looper.java": '''package android.os;
public class Looper {
    private static Looper current;
    public static Looper myLooper() { return current; }
    public static void prepare() { current = new Looper(); }
}''',
            "android/app/ActivityThread.java": '''package android.app;
public class ActivityThread {
    public static ActivityThread currentActivityThread() { return null; }
    public static ActivityThread systemMain() throws Exception {
        String pid = new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(System.getProperty("inventory.pidfile"))));
        if (!pid.equals("12345") || android.os.Looper.myLooper() == null) throw new AssertionError("Inventory ownership must be recorded before metadata access");
        return new ActivityThread();
    }
    public android.content.Context getSystemContext() { return new android.content.Context(); }
}''',
            "android/content/Context.java": '''package android.content;
public class Context {
    public android.content.pm.PackageManager getPackageManager() { return new android.content.pm.PackageManager(); }
}''',
            "android/content/pm/ApplicationInfo.java": '''package android.content.pm;
public class ApplicationInfo {
    public static final int FLAG_SYSTEM = 1;
    public int uid = 10001;
    public int flags;
    public CharSequence loadLabel(PackageManager manager) { return "Example app"; }
}''',
            "android/content/pm/PackageInfo.java": '''package android.content.pm;
public class PackageInfo {
    public String packageName = "org.example.app";
    public ApplicationInfo applicationInfo = new ApplicationInfo();
    public String versionName = "2.0";
    public long firstInstallTime = 100;
    public long lastUpdateTime = 200;
    public long getLongVersionCode() { return 4; }
}''',
            "android/content/pm/PackageManager.java": '''package android.content.pm;
public class PackageManager {
    public java.util.List<PackageInfo> getInstalledPackages(int flags) {
        if (Boolean.getBoolean("inventory.fail")) throw new IllegalStateException("metadata unavailable");
        return java.util.Collections.singletonList(new PackageInfo());
    }
}''',
            "android/content/Intent.java": '''package android.content;
public class Intent {
    public static final int FLAG_ACTIVITY_NEW_TASK = 0x10000000;
    public static final int FLAG_ACTIVITY_CLEAR_TASK = 0x00008000;
}''',
            "org/json/JSONObject.java": '''package org.json;
public class JSONObject {
    public static final Object NULL = new Object();
    private final java.util.Map<String, Object> values = new java.util.LinkedHashMap<>();
    public JSONObject put(String key, Object value) { values.put(key, value); return this; }
    public String toString() {
        StringBuilder output = new StringBuilder("{");
        for (java.util.Map.Entry<String, Object> entry : values.entrySet()) {
            if (output.length() > 1) output.append(",");
            output.append("\\\"").append(entry.getKey()).append("\\\":");
            Object value = entry.getValue();
            if (value == NULL) output.append("null");
            else if (value instanceof String) output.append("\\\"").append(value.toString().replace("\\\"", "\\\\\\\"")).append("\\\"");
            else output.append(value);
        }
        return output.append("}").toString();
    }
}''',
            "com/android/uiautomator/testrunner/UiAutomatorTestCase.java": '''package com.android.uiautomator.testrunner;
public class UiAutomatorTestCase {
    public static final android.os.Bundle params = new android.os.Bundle();
    public android.os.Bundle getParams() { return params; }
    public com.android.uiautomator.core.UiDevice getUiDevice() { return new com.android.uiautomator.core.UiDevice(); }
    public void assertTrue(String message, boolean value) { if (!value) throw new AssertionError(message); }
    public void assertEquals(String message, Object expected, Object actual) { if (!expected.equals(actual)) throw new AssertionError(message); }
    public void fail(String message) { throw new AssertionError(message); }
}''',
            "com/android/uiautomator/core/Configurator.java": '''package com.android.uiautomator.core;
public class Configurator {
    public static Configurator getInstance() { return new Configurator(); }
    public Configurator setWaitForIdleTimeout(long timeout) { return this; }
    public Configurator setWaitForSelectorTimeout(long timeout) { return this; }
}''',
            "com/android/uiautomator/core/UiSelector.java": '''package com.android.uiautomator.core;
public class UiSelector {
    String id;
    String text;
    Boolean checked;
    boolean regex;
    public UiSelector resourceId(String id) { this.id = id; return this; }
    public UiSelector resourceIdMatches(String id) { this.id = id; regex = true; return this; }
    public UiSelector text(String text) { this.text = text; return this; }
    public UiSelector textMatches(String text) { return this; }
    public UiSelector checked(boolean checked) { this.checked = checked; return this; }
}''',
            "com/android/uiautomator/core/UiDevice.java": '''package com.android.uiautomator.core;
public class UiDevice {
    public boolean pressBack() { phonepolicy.NativeHarness.screen--; return true; }
    public boolean pressHome() { phonepolicy.NativeHarness.screen = 0; return true; }
    public String getCurrentPackageName() { return phonepolicy.NativeHarness.screen > 0 ? "com.iqoo.powersaving" : "launcher"; }
}''',
            "com/android/uiautomator/core/UiObject.java": '''package com.android.uiautomator.core;
import phonepolicy.NativeHarness;
public class UiObject {
    private final UiSelector selector;
    public UiObject(UiSelector selector) { this.selector = selector; }
    public boolean waitForExists(long timeout) {
        if (NativeHarness.blocked) android.os.SystemClock.sleep(1000);
        if (selector.id.endsWith("app_name")) return NativeHarness.screen > 0 && (selector.text == null || selector.text.equals(NativeHarness.label));
        if (selector.id.endsWith("title")) return NativeHarness.screen == 1 && !NativeHarness.missing;
        if (NativeHarness.screen != 2) return false;
        if (selector.regex) return true;
        return selector.checked == null || selector.checked == isChecked();
    }
    public boolean waitUntilGone(long timeout) { return !waitForExists(timeout); }
    public String getText() { return NativeHarness.label; }
    public boolean clickAndWaitForNewWindow(long timeout) { NativeHarness.screen = 2; return true; }
    public boolean click() { NativeHarness.clicks++; NativeHarness.choice = "allow"; return true; }
    public boolean isEnabled() { return !NativeHarness.disabled; }
    public boolean isChecked() {
        return selector.id.endsWith("all_opt") ? NativeHarness.choice.equals("allow") :
            selector.id.endsWith("all_intel") ? NativeHarness.choice.equals("smart") : NativeHarness.choice.equals("restrict");
    }
}''',
            "phonepolicy/NativeHarness.java": '''package phonepolicy;
import com.android.uiautomator.testrunner.UiAutomatorTestCase;
public class NativeHarness {
    public static String choice;
    public static String label = "Example app";
    public static int screen = 1;
    public static int clicks;
    public static boolean disabled;
    public static boolean missing;
    public static boolean blocked;
    public static void main(String[] args) throws Exception {
        UiAutomatorTestCase.params.put("package", "org.example.app");
        UiAutomatorTestCase.params.put("pidfile", args[0]);
        UiAutomatorTestCase.params.put("mode", args[1]);
        UiAutomatorTestCase.params.put("expected_label", args[3].equals("wrong_label") ? "Different app" : label);
        choice = args[2];
        disabled = args[3].equals("disabled");
        missing = args[3].equals("missing");
        blocked = args[3].equals("host_lost");
        if (blocked) UiAutomatorTestCase.params.put("heartbeat", args[4]);
        try { new VivoBackgroundPower().testAllowBackgroundPowerUsage(); }
        catch (AssertionError failure) { System.out.println("ASSERTION " + failure.getMessage()); }
        System.out.println("SCREEN " + screen);
        System.out.println("CLICKS " + clicks);
    }
}''',
            "phonepolicy/InventoryHarness.java": '''package phonepolicy;
public class InventoryHarness {
    public static void main(String[] args) throws Exception {
        System.setProperty("inventory.pidfile", args[0]);
        System.setProperty("inventory.fail", args[1]);
        PackageInventory.main(new String[] {"--user", "0", "--pidfile", args[0]});
    }
}''',
        }
        for path, content in sources.items():
            destination = cls.directory / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
        binary = cls.directory / "bin"
        binary.mkdir()
        command = binary / "am"
        command.write_text("#!/bin/sh\nexit 0\n")
        command.chmod(0o755)
        cls.environment = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"])
        subprocess.run(["javac", "--release", "8", "-d", str(cls.directory), *map(str, cls.directory.rglob("*.java")),
                        str(ROOT / "presets" / "vivo-background-power" / "VivoBackgroundPower.java"),
                        str(ROOT / "presets" / "vivo-background-power" / "PackageInventory.java")], check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def invoke_native(self, mode, choice, scenario="normal"):
        heartbeat = self.directory / "heartbeat"
        heartbeat.touch()
        if scenario == "host_lost":
            os.utime(heartbeat, (1, 1))
        result = subprocess.run(["java", "-cp", str(self.directory), "phonepolicy.NativeHarness", str(self.directory / "pid"), mode, choice, scenario, str(heartbeat)], capture_output=True, text=True, env=self.environment, timeout=10)
        observed = [json.loads(line[12:]) for line in result.stdout.splitlines() if line.startswith("VIVO_RESULT ")]
        return result, observed

    def test_verification_preserves_each_current_choice(self):
        for choice in ("allow", "smart", "restrict"):
            with self.subTest(choice=choice):
                process, records = self.invoke_native("verify", choice)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(records[0]["before"], choice)
                self.assertEqual(records[0]["after"], choice)
                self.assertTrue(records[0]["phone_free"])
                self.assertIn("CLICKS 0", process.stdout)
                self.assertIn("SCREEN 0", process.stdout)
                self.assertNotIn("VIVO_ALLOWED ", process.stdout)

    def test_apply_verifies_changed_and_existing_allow(self):
        for choice in ("allow", "smart", "restrict"):
            with self.subTest(choice=choice):
                process, records = self.invoke_native("apply", choice)
                self.assertEqual(records[0]["after"], "allow")
                self.assertEqual(records[0]["status"], "already_configured" if choice == "allow" else "verified")
                self.assertIn("CLICKS " + ("0" if choice == "allow" else "1"), process.stdout)
                self.assertIn("SCREEN 0", process.stdout)

    def test_label_mismatch_cannot_change_another_app(self):
        process, records = self.invoke_native("apply", "smart", "wrong_label")
        self.assertEqual(records[0]["status"], "failed")
        self.assertTrue(records[0]["phone_free"])
        self.assertIn("CLICKS 0", process.stdout)
        self.assertNotIn("VIVO_ALLOWED ", process.stdout)
        self.assertNotIn("VIVO_COMPLETE ", process.stdout)

    def test_unavailable_controls_close_without_claiming_allow(self):
        for scenario in ("missing", "disabled"):
            with self.subTest(scenario=scenario):
                process, records = self.invoke_native("apply", "smart", scenario)
                self.assertEqual(records[0]["status"], "unavailable")
                self.assertIsNone(records[0]["after"])
                self.assertTrue(records[0]["phone_free"])
                self.assertIn("CLICKS 0", process.stdout)
                self.assertIn("SCREEN 0", process.stdout)

    def test_lost_host_kills_helper_even_during_a_blocked_ui_call(self):
        process, records = self.invoke_native("apply", "smart", "host_lost")
        self.assertEqual(process.returncode, 99)
        self.assertEqual(records, [])
        self.assertIn('"reason":"host_lost"', process.stdout)
        self.assertIn('"phone_free":false', process.stdout)
        self.assertNotIn("VIVO_ALLOWED ", process.stdout)

    def test_inventory_records_ownership_before_metadata_and_emits_complete_results(self):
        pidfile = self.directory / "inventory-pid"
        process = subprocess.run(["java", "-cp", str(self.directory), "phonepolicy.InventoryHarness", str(pidfile), "false"],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(pidfile.read_text(), "12345")
        lines = process.stdout.splitlines()
        self.assertEqual(lines[0], "VIVO_PID 12345")
        self.assertEqual(json.loads(lines[1][9:]), {"package": "org.example.app", "label": "Example app", "uid": 10001,
                                                  "system": False, "version_code": 4, "version_name": "2.0",
                                                  "first_install_time": 100, "last_update_time": 200})
        self.assertEqual(lines[2], "VIVO_INVENTORY_COMPLETE 1")

    def test_inventory_keeps_pid_when_metadata_fails_without_claiming_completion(self):
        pidfile = self.directory / "failed-inventory-pid"
        process = subprocess.run(["java", "-cp", str(self.directory), "phonepolicy.InventoryHarness", str(pidfile), "true"],
                                 capture_output=True, text=True, timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(pidfile.read_text(), "12345")
        self.assertEqual(process.stdout.splitlines(), ["VIVO_PID 12345"])
        self.assertIn("metadata unavailable", process.stderr)
