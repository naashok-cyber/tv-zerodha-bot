"""Daemonize monitor_ng_m2m.py --loop with output to /tmp/ng_monitor.log."""
import os
import sys

LOG = "/tmp/ng_monitor.log"
SCRIPT = "/app/scripts/monitor_ng_m2m.py"

# Kill any existing instance
import glob, signal
for cmdline_path in glob.glob("/proc/*/cmdline"):
    try:
        with open(cmdline_path, "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode(errors="ignore")
        if "monitor_ng_m2m" in cmd and "daemon" not in cmd:
            pid = int(cmdline_path.split("/")[2])
            if pid != os.getpid():
                os.kill(pid, signal.SIGTERM)
    except Exception:
        pass

# Double-fork to fully detach
pid = os.fork()
if pid > 0:
    print(f"NG monitor daemon started (child pid={pid})")
    sys.exit(0)

os.setsid()

pid2 = os.fork()
if pid2 > 0:
    sys.exit(0)

# Grandchild — redirect I/O then exec
with open(LOG, "a") as log:
    os.dup2(log.fileno(), sys.stdout.fileno())
    os.dup2(log.fileno(), sys.stderr.fileno())

with open("/dev/null") as devnull:
    os.dup2(devnull.fileno(), sys.stdin.fileno())

os.execv(sys.executable, [sys.executable, "-u", SCRIPT, "--loop"])
