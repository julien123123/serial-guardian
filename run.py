#!/usr/bin/env python3
"""
Entry point. Run this on the Pi (directly, or via the systemd service in
systemd/serial-guardian.service):

    python3 run.py

Then browse to http://<pi-hostname-or-ip>:8080/
"""
import threading

import config as cfg
import settings
from monitor import SerialSessionMonitor
from webapp import create_app


def main():
    settings.apply_overrides(cfg)   # pick up anything saved from the Settings page

    monitor = SerialSessionMonitor(cfg)

    t = threading.Thread(target=monitor.run_forever, daemon=True, name="serial-monitor")
    t.start()

    app = create_app(monitor, cfg)
    app.run(host=cfg.WEB_HOST, port=cfg.WEB_PORT, threaded=True)


if __name__ == "__main__":
    main()
