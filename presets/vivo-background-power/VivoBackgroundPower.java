package phonepolicy;

import com.android.uiautomator.testrunner.UiAutomatorTestCase;
import com.android.uiautomator.core.Configurator;
import com.android.uiautomator.core.UiObject;
import com.android.uiautomator.core.UiSelector;
import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.FileWriter;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;
import org.json.JSONObject;

public class VivoBackgroundPower extends UiAutomatorTestCase {
    private final SessionWatchdog watchdog = new SessionWatchdog();

    private class SessionWatchdog extends Thread {
        volatile boolean finished;
        volatile long deadline;
        volatile Process launch;
        File heartbeat;

        @Override
        public void run() {
            while (!finished) {
                String reason = null;
                if (android.os.SystemClock.elapsedRealtime() >= deadline) {
                    reason = "watchdog_timeout";
                } else if (heartbeat != null && System.currentTimeMillis() - heartbeat.lastModified() > 15000) {
                    reason = "host_lost";
                }
                if (reason != null && !finished) {
                    Process activeLaunch = launch;
                    if (activeLaunch != null) {
                        activeLaunch.destroyForcibly();
                    }
                    System.out.println("VIVO_END {\"reason\":\"" + reason + "\",\"phone_free\":false}");
                    System.out.flush();
                    android.os.Process.killProcess(android.os.Process.myPid());
                    return;
                }
                android.os.SystemClock.sleep(250);
            }
        }
    }

    public void testAllowBackgroundPowerUsage() throws Exception {
        try (FileWriter pidfile = new FileWriter(getParams().getString("pidfile"))) {
            pidfile.write(Integer.toString(android.os.Process.myPid()));
        }
        System.out.println("VIVO_PID " + android.os.Process.myPid());
        System.out.flush();
        watchdog.deadline = android.os.SystemClock.elapsedRealtime() + 120000;
        String heartbeatPath = getParams().getString("heartbeat");
        if (heartbeatPath != null) {
            watchdog.heartbeat = new File(heartbeatPath);
        }
        watchdog.setDaemon(true);
        watchdog.start();
        int completed = 0;
        boolean phoneFree = true;
        String ending = "complete";
        try {
            String mode = getParams().getString("mode", "apply");
            assertTrue("Native mode must be apply or verify", mode.equals("apply") || mode.equals("verify"));
            List<String> queue = new ArrayList<>();
            String singlePackage = getParams().getString("package");
            if (singlePackage != null) {
                queue.add(singlePackage);
            } else {
                try (BufferedReader packages = new BufferedReader(new FileReader(getParams().getString("packages")))) {
                    String packageName;
                    while ((packageName = packages.readLine()) != null) {
                        queue.add(packageName);
                    }
                }
            }
            Configurator.getInstance().setWaitForIdleTimeout(200).setWaitForSelectorTimeout(10000);
            UiObject powerControl = new UiObject(new UiSelector().resourceId("com.iqoo.powersaving:id/title").text("Background power control"));
            UiObject appName = new UiObject(new UiSelector().resourceId("com.iqoo.powersaving:id/app_name"));
            UiObject namedApp = new UiObject(new UiSelector().resourceId("com.iqoo.powersaving:id/app_name").textMatches(".+"));
            UiObject allow = new UiObject(new UiSelector().resourceId("com.iqoo.powersaving:id/all_opt"));
            UiObject smart = new UiObject(new UiSelector().resourceId("com.iqoo.powersaving:id/all_intel"));
            UiObject initialSelection = new UiObject(new UiSelector().resourceIdMatches("com.iqoo.powersaving:id/(all_intel|all_opt|all_vos_button_opt_restrict)").checked(true));
            for (String packageName : queue) {
                String controlPath = getParams().getString("control");
                if (controlPath != null) {
                    try (BufferedReader control = new BufferedReader(new FileReader(controlPath))) {
                        String instruction = control.readLine();
                        if ("pause".equals(instruction) || "stop".equals(instruction)) {
                            ending = "pause".equals(instruction) ? "paused" : "stopped";
                            break;
                        }
                    }
                }
                watchdog.deadline = android.os.SystemClock.elapsedRealtime() + 120000;
                String before = null;
                String after = null;
                String status = "unavailable";
                String reason = null;
                boolean policyPageOpen = false;
                boolean navigating = false;
                Throwable failure = null;
                System.out.println("VIVO_BEGIN " + packageName);
                System.out.flush();
                try {
                    navigating = true;
                    phoneFree = false;
                    Process launch = new ProcessBuilder("am", "start", "-W", "--user", "0", "-f", Integer.toString(android.content.Intent.FLAG_ACTIVITY_NEW_TASK | android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK), "-n", "com.iqoo.powersaving/.fuelgauge.PowerUsageSummaryActivity", "--es", "package_name", packageName).redirectErrorStream(true).start();
                    watchdog.launch = launch;
                    if (!launch.waitFor(15000, TimeUnit.MILLISECONDS)) {
                        launch.destroyForcibly();
                        fail("Native battery activity launch timed out for " + packageName);
                    }
                    assertEquals("Native battery activity launch for " + packageName, 0, launch.exitValue());
                    watchdog.launch = null;
                    if (!powerControl.waitForExists(5000)) {
                        assertEquals("Native UI remains in battery settings", "com.iqoo.powersaving", getUiDevice().getCurrentPackageName());
                        reason = "NO_NATIVE_CONTROL";
                    } else {
                        assertTrue("Nonempty native app label for " + packageName, namedApp.waitForExists(5000));
                        String label = appName.getText();
                        String expectedLabel = getParams().getString("expected_label");
                        if (expectedLabel != null) {
                            assertEquals("Native app label matches selected installation", expectedLabel, label);
                        }
                        UiObject expectedApp = new UiObject(new UiSelector().resourceId("com.iqoo.powersaving:id/app_name").text(label));
                        assertTrue("Open native power page for " + packageName, powerControl.clickAndWaitForNewWindow(5000));
                        policyPageOpen = true;
                        assertTrue("Allow radio exists for " + packageName, allow.waitForExists(5000));
                        if (!namedApp.waitForExists(5000)) {
                            reason = "NO_NATIVE_POLICY_ENTRY";
                        } else {
                            assertTrue("App label across native screens", expectedApp.waitForExists(5000));
                            assertTrue("Initial native radio state for " + packageName, initialSelection.waitForExists(5000));
                            before = allow.isChecked() ? "allow" : (smart.isChecked() ? "smart" : "restrict");
                            if (!allow.isEnabled()) {
                                reason = "CONTROL_DISABLED";
                            } else {
                                if (mode.equals("apply") && !allow.isChecked()) {
                                    assertTrue("Select native Allow for " + packageName, allow.click());
                                }
                                String selected = mode.equals("apply") ? "allow" : before;
                                assertTrue("Allow selection settled for " + packageName, new UiObject(new UiSelector().resourceId("com.iqoo.powersaving:id/all_opt").checked(selected.equals("allow"))).waitForExists(10000));
                                assertTrue("Smart selection settled for " + packageName, new UiObject(new UiSelector().resourceId("com.iqoo.powersaving:id/all_intel").checked(selected.equals("smart"))).waitForExists(10000));
                                assertTrue("Restrict selection settled for " + packageName, new UiObject(new UiSelector().resourceId("com.iqoo.powersaving:id/all_vos_button_opt_restrict").checked(selected.equals("restrict"))).waitForExists(10000));
                                getUiDevice().pressBack();
                                policyPageOpen = false;
                                assertTrue("Return to battery details", powerControl.waitForExists(5000));
                                assertTrue("Reopen native power page", powerControl.clickAndWaitForNewWindow(5000));
                                policyPageOpen = true;
                                assertTrue("App label after reopening", expectedApp.waitForExists(5000));
                                assertTrue("Native radio state after reopening", initialSelection.waitForExists(10000));
                                after = allow.isChecked() ? "allow" : (smart.isChecked() ? "smart" : "restrict");
                                assertTrue("Persisted Allow state for " + packageName, new UiObject(new UiSelector().resourceId("com.iqoo.powersaving:id/all_opt").checked(after.equals("allow"))).waitForExists(10000));
                                assertTrue("Persisted Smart state for " + packageName, new UiObject(new UiSelector().resourceId("com.iqoo.powersaving:id/all_intel").checked(after.equals("smart"))).waitForExists(10000));
                                assertTrue("Persisted Restrict state for " + packageName, new UiObject(new UiSelector().resourceId("com.iqoo.powersaving:id/all_vos_button_opt_restrict").checked(after.equals("restrict"))).waitForExists(10000));
                                if (mode.equals("apply")) {
                                    assertEquals("Persisted Allow for " + packageName, "allow", after);
                                    status = before.equals("allow") ? "already_configured" : "verified";
                                } else {
                                    status = after.equals("allow") ? "verified" : "not_configured";
                                }
                            }
                        }
                    }
                    if (policyPageOpen) {
                        getUiDevice().pressBack();
                        assertTrue("Close native power page", powerControl.waitForExists(5000));
                    }
                    getUiDevice().pressBack();
                    assertTrue("Close native battery details", appName.waitUntilGone(5000));
                    phoneFree = true;
                } catch (Exception | Error error) {
                    failure = error;
                    status = "failed";
                    reason = error.toString();
                    ending = "failed";
                    if (navigating) {
                        getUiDevice().pressHome();
                        phoneFree = appName.waitUntilGone(5000) && !"com.iqoo.powersaving".equals(getUiDevice().getCurrentPackageName());
                    }
                } finally {
                    if (watchdog.launch != null) {
                        watchdog.launch.destroyForcibly();
                        watchdog.launch = null;
                    }
                }
                JSONObject result = new JSONObject();
                result.put("package", packageName);
                result.put("before", before == null ? JSONObject.NULL : before);
                result.put("after", after == null ? JSONObject.NULL : after);
                result.put("status", status);
                result.put("reason", reason == null ? JSONObject.NULL : reason);
                result.put("verified_at", Instant.now().toString());
                result.put("phone_free", phoneFree);
                System.out.println("VIVO_RESULT " + result.toString());
                if (failure == null) {
                    if (status.equals("unavailable")) {
                        System.out.println("VIVO_SKIPPED " + packageName + " " + reason);
                    } else if (mode.equals("verify")) {
                        System.out.println("VIVO_OBSERVED " + packageName + " " + before + " " + after);
                    } else {
                        System.out.println("VIVO_ALLOWED " + packageName + " " + before);
                    }
                    completed++;
                }
                System.out.flush();
                if (failure != null) {
                    if (failure instanceof Exception) {
                        throw (Exception) failure;
                    }
                    throw (Error) failure;
                }
            }
        } catch (Exception | Error failure) {
            ending = "failed";
            throw failure;
        } finally {
            watchdog.finished = true;
            watchdog.join(1000);
            if (ending.equals("complete")) {
                System.out.println("VIVO_COMPLETE " + completed);
            }
            JSONObject result = new JSONObject();
            result.put("reason", ending);
            result.put("completed", completed);
            result.put("phone_free", phoneFree);
            System.out.println("VIVO_END " + result.toString());
            System.out.flush();
        }
    }
}
