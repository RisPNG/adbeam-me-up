package phonepolicy;

import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import java.io.FileWriter;
import org.json.JSONObject;

public class PackageInventory {
    public static void main(String[] args) throws Exception {
        if (args.length != 4 || !args[0].equals("--user") || !args[1].equals("0") || !args[2].equals("--pidfile")) {
            throw new IllegalArgumentException("Package inventory requires --user 0 --pidfile <path>");
        }
        try (FileWriter pidfile = new FileWriter(args[3])) {
            pidfile.write(Integer.toString(android.os.Process.myPid()));
        }
        System.out.println("VIVO_PID " + android.os.Process.myPid());
        System.out.flush();
        Thread deadline = new Thread("inventory-deadline") {
            @Override
            public void run() {
                try {
                    Thread.sleep(30000);
                } catch (InterruptedException finished) {
                    return;
                }
                android.os.Process.killProcess(android.os.Process.myPid());
            }
        };
        deadline.setDaemon(true);
        deadline.start();
        try {
            Class<?> activityThreadClass = Class.forName("android.app.ActivityThread");
            Object activityThread = activityThreadClass.getMethod("currentActivityThread").invoke(null);
            if (activityThread == null) {
                if (android.os.Looper.myLooper() == null) {
                    android.os.Looper.prepare();
                }
                activityThread = activityThreadClass.getMethod("systemMain").invoke(null);
            }
            Context context = (Context) activityThreadClass.getMethod("getSystemContext").invoke(activityThread);
            PackageManager packageManager = context.getPackageManager();
            int count = 0;
            for (PackageInfo info : packageManager.getInstalledPackages(0)) {
                JSONObject app = new JSONObject();
                app.put("package", info.packageName);
                app.put("label", info.applicationInfo.loadLabel(packageManager).toString());
                app.put("uid", info.applicationInfo.uid);
                app.put("system", (info.applicationInfo.flags & ApplicationInfo.FLAG_SYSTEM) != 0);
                app.put("version_code", info.getLongVersionCode());
                app.put("version_name", info.versionName == null ? JSONObject.NULL : info.versionName);
                app.put("first_install_time", info.firstInstallTime);
                app.put("last_update_time", info.lastUpdateTime);
                System.out.println("VIVO_APP " + app.toString());
                count++;
            }
            System.out.println("VIVO_INVENTORY_COMPLETE " + count);
            System.out.flush();
        } finally {
            deadline.interrupt();
        }
    }
}
