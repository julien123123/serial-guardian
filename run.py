#!/usr/bin/env python3
"""
Entry point. Run this on the Pi (directly, or via the systemd service in
systemd/serial-guardian.service):

    python3 run.py

Then browse to http://<pi-hostname-or-ip>:8080/

Serves with waitress (a production-grade WSGI server) if it's installed
-- `sudo apt install python3-waitress` -- falling back to Flask's own
development server otherwise, which works fine but isn't meant to be
left running unattended for long stretches. See docs/GUIDE.md's "if it stops
responding" section for why this matters.
"""
import logging
import threading

import applog
import config as cfg
import sdnotify
import settings
import watchdog as watchdog_mod
from monitor import SerialSessionMonitor
from webapp import create_app


def main():
    settings.apply_overrides(cfg)   # pick up anything saved from the Settings page
    applog.setup(cfg)
    logger = logging.getLogger("guardian")
    logger.info("serial-guardian starting up")

    monitor = SerialSessionMonitor(cfg)
    threading.Thread(target=monitor.run_forever, daemon=True, name="serial-monitor").start()
    threading.Thread(target=watchdog_mod.run, args=(monitor, cfg), daemon=True, name="watchdog").start()

    app = create_app(monitor, cfg)

    # Type=notify in the systemd unit waits for this before considering
    # startup complete; a harmless no-op if not running under systemd.
    sdnotify.ready()

    try:
        from waitress import serve
        logger.info("serving with waitress on %s:%s", cfg.WEB_HOST, cfg.WEB_PORT)
        serve(app, host=cfg.WEB_HOST, port=cfg.WEB_PORT, threads=6)
    except ImportError:
        logger.warning(
            "waitress not installed -- falling back to Flask's development server. "
            "Fine for testing; `sudo apt install python3-waitress` is recommended "
            "for anything left running unattended (see docs/GUIDE.md)."
        )
        app.run(host=cfg.WEB_HOST, port=cfg.WEB_PORT, threaded=True)


if __name__ == "__main__":
    main()
