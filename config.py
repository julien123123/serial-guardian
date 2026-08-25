"""
Serial Guardian configuration.

Edit these to match your hardware. Nothing in here needs to change unless
noted otherwise.
"""

# --- Serial link to the MicroPython board -----------------------------------
SERIAL_PORT = "/dev/ttyACM0"
BAUD = 115200
# How long ser.read() blocks waiting for at least one byte when the board
# is idle. Kept short so the boot-timeout / stop-request checks (which run
# once per loop iteration) stay responsive.
SERIAL_READ_TIMEOUT = 0.3

# --- Where everything gets stored on the Pi's SD card ------------------------
DATA_DIR = "/home/pi/serial-guardian/data"
LIVE_TAIL_LINES = 400          # lines kept in memory for the live dashboard view

# --- How a "clean" session is recognised -------------------------------------
# A session that reaches this line before it disconnects is considered normal
# (it comes from gotosleep()/ep.__exit__ completing cleanly).
GOTOSLEEP_MARKER = "ep.__exit__ completed"

# Any of these appearing in a session's output flags it as an EXCEPTION --
# unless that session was a deliberate STOP (see below), in which case the
# KeyboardInterrupt traceback our own Ctrl-C causes is expected and ignored.
EXCEPTION_MARKERS = [
    "Traceback (most recent call last):",
    "MemoryError",
    "Exception handler: caught exception",
]

# Printed by MicroPython when it drops into the interactive REPL after a
# fresh boot/soft-reset. Used to detect that a reset actually took effect.
BOOT_BANNER_MARKER = "Main try: starting"

# --- Automatic recovery from an unplanned exception -------------------------
# As soon as an exception marker is seen, wait this long (to let the
# traceback finish printing) then try a soft reset over serial.
SOFT_RESET_DELAY = 1.5
# CR, Ctrl-C x2 (stop whatever's running / bail out of raw-paste mode),
# Ctrl-D (MicroPython soft reboot). Same thing you'd type by hand in a
# serial terminal sitting at the ">>>" prompt.
SOFT_RESET_BYTES = b"\r\x03\x03\x04"
# If we don't see a fresh boot banner within this many seconds of sending
# the soft reset, escalate to a hardware reset (if enabled below).
BOOT_TIMEOUT = 12

# Hardware reset line, wired from a Pi GPIO pin to the board's RESET pin.
# See README.md for wiring. Leave this False if you haven't wired it up --
# the soft reset alone handles the vast majority of MicroPython exceptions.
# Only ever used for the automatic exception-recovery path, never for the
# manual stop/resume buttons below.
ENABLE_GPIO_RESET = False
GPIO_RESET_PIN = 17            # BCM numbering
GPIO_RESET_PULSE = 0.25        # seconds to hold RESET low

# --- Deliberate "stop device on next update" button --------------------
# Sends Ctrl-C (not Ctrl-D) so the board lands at the REPL and just sits
# there -- it won't call gotosleep(), so it stays awake and connected until
# you hit Resume (which sends Ctrl-D) or reset it some other way.
STOP_BYTES = b"\x03\x03"
STOP_SEND_DELAY = 0.4          # settle time after a fresh connect before sending it

# --- Pause monitoring outright (e.g. to hand the port to mpremote) ---------
# How often the paused run_forever loop checks whether it's been resumed.
MONITORING_PAUSE_POLL_INTERVAL = 0.3

# --- Disk usage ------------------------------------------------------------
# Full raw logs are always kept for EXCEPTION, ANOMALY and STOPPED sessions
# (rare, and exactly what you want to go back and read). Plain NORMAL
# session logs are kept as a rolling window so the SD card doesn't fill up
# over months of unattended running. Set to 0 to disable pruning entirely.
NORMAL_LOG_RETENTION = 500

# --- Disk safety ------------------------------------------------------
# If free space on the DATA_DIR filesystem drops below this, a session's
# raw log text is skipped (the index entry is still recorded) rather than
# risking a write failure -- turns "SD card fills up" into a graceful
# degradation instead of a crash loop.
MIN_FREE_DISK_MB = 100

# --- Web UI ------------------------------------------------------------
WEB_HOST = "0.0.0.0"
WEB_PORT = 8080

# --- systemd service control from the web UI --------------------------
# Must match the unit name installed at /etc/systemd/system/<name>.service
# (systemd/serial-guardian.service in this repo). Requires the narrowly
# scoped sudoers rule in systemd/serial-guardian.sudoers -- see README.
SERVICE_NAME = "serial-guardian"
