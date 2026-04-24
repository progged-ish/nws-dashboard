#!/usr/bin/env python3
"""Clean launcher that avoids bash job-control hang in Hermes."""
import subprocess, sys, os, time, json

PIDFILE = "/home/progged-ish/nws_dashboard/dashboard.pid"
LOGFILE = "/tmp/nws_dashboard_5000.log"
WORKDIR = "/home/progged-ish/nws_dashboard"
VENV_PYTHON = os.path.join(WORKDIR, "venv/bin/python")

def main():
    if os.path.exists(PIDFILE):
        try:
            with open(PIDFILE) as f:
                old = int(f.read().strip())
            os.kill(old, 0)
            os.kill(old, 15)
            time.sleep(1)
            try: os.kill(old, 0)
            except: pass
            else: os.kill(old, 9)
        except: pass
        os.remove(PIDFILE)

    if os.path.exists(LOGFILE):
        with open(LOGFILE, "w"):
            pass

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    log_fd = open(LOGFILE, "w")
    proc = subprocess.Popen(
        [VENV_PYTHON, "app.py"],
        cwd=WORKDIR,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )

    with open(PIDFILE, "w") as f:
        f.write(str(proc.pid))

    time.sleep(2)

    alive = False
    try:
        os.kill(proc.pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False

    print(json.dumps({
        "pid": proc.pid,
        "alive": alive,
        "pidfile": PIDFILE,
        "logfile": LOGFILE,
    }))
    return 0 if alive else 1

if __name__ == "__main__":
    sys.exit(main())
