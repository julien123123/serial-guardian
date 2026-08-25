"""
Minimal sd_notify client -- no python3-systemd dependency needed. Lets
systemd's WatchdogSec= (paired with Type=notify in the unit file) detect
a genuinely hung process and restart it, on top of the broad exception
recovery already built into the monitor loop itself. The two catch
different failure modes: exception recovery handles anything that raises;
this handles a true hang (e.g. a blocking read that never returns).

A no-op when not running under systemd (e.g. `python3 run.py` directly
during development) -- NOTIFY_SOCKET just won't be set, so every call
here silently does nothing.
"""
import os
import socket


def _notify(state):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr[0] == "@":
        addr = "\0" + addr[1:]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(addr)
            sock.sendall(state.encode())
        finally:
            sock.close()
    except OSError:
        pass


def ready():
    """Tell systemd startup is complete (required once, for Type=notify)."""
    _notify("READY=1")


def watchdog_ping():
    """Tell systemd 'still alive' -- call more often than WatchdogSec."""
    _notify("WATCHDOG=1")
