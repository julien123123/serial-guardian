"""
A meta health-check that watches over BOTH halves of the app -- the
monitor thread and the web server -- since either one hanging on its own
would otherwise look "alive" from the other's perspective. Only pings
systemd's watchdog when the monitor loop's own heartbeat is recent AND a
real local HTTP request to the Live page actually gets a response.

Runs as its own daemon thread, started from run.py alongside the monitor
thread. Requires the systemd unit to use Type=notify + WatchdogSec= (see
systemd/serial-guardian.service) -- otherwise sdnotify's pings are just
silent no-ops and this loop is harmless but does nothing.
"""
import logging
import time
import urllib.request

import sdnotify

logger = logging.getLogger("guardian")

CHECK_INTERVAL = 15          # seconds between checks
MONITOR_STALE_AFTER = 30     # seconds since the monitor loop last ticked
HTTP_TIMEOUT = 5             # seconds for the local self-check request


def run(monitor, cfg):
    while True:
        time.sleep(CHECK_INTERVAL)
        check_once(monitor, cfg)


def check_once(monitor, cfg):
    """One health-check iteration, split out from run() so it can be
    exercised directly in tests without spinning up an infinite loop."""
    url = f"http://127.0.0.1:{cfg.WEB_PORT}/api/tail"
    try:
        stale_for = monitor.seconds_since_alive()
        if stale_for > MONITOR_STALE_AFTER:
            logger.warning("watchdog: monitor loop stale for %.0fs -- withholding heartbeat", stale_for)
            return
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning("watchdog: local self-check returned %s -- withholding heartbeat", resp.status)
                return
    except Exception as exc:
        logger.warning("watchdog: self-check failed (%s) -- withholding heartbeat", exc)
        return
    sdnotify.watchdog_ping()
