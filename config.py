"""
Serial Guardian configuration.

Edit these to match your hardware. Nothing in here needs to change unless
noted otherwise.
"""

# --- Serial link to the MicroPython board -----------------------------------
SERIAL_PORT = "/dev/ttyACM0"
BAUD = 115200

# --- Where everything gets stored on the Pi's SD card ------------------------
DATA_DIR = "/home/pi/serial-guardian/data"
LIVE_TAIL_LINES = 400          # lines kept in memory for the live dashboard view

# --- How a "clean" session is recognised -------------------------------------
# A session that reaches this line before it disconnects is considered normal
# (it comes from gotosleep()/ep.__exit__ completing cleanly).
GOTOSLEEP_MARKER = "gotosleep(): committed FRAM"

# Any of these appearing in a session's output flags it as an EXCEPTION.
EXCEPTION_MARKERS = [
    "Traceback (most recent call last):",
    "MemoryError",
    "Exception handler: caught exception",
]

# Printed by MicroPython when it drops into the interactive REPL after a
# fresh boot/soft-reset. Used to detect that a reset actually took effect.
BOOT_BANNER_MARKER = "Main try: starting"

# --- Reset behaviour -----------------------------------------------------
# As soon as an exception marker is seen, wait this long (to let the
# traceback finish printing) then try a soft reset over serial.
SOFT_RESET_DELAY = 1.5
# CR, Ctrl-C x2 (stop whatever's running / bail out of raw-paste mode),
# Ctrl-D (MicroPython soft reboot). This is the same thing you'd type by
# hand in a serial terminal sitting at the ">>>" prompt.
SOFT_RESET_BYTES = b"\r\x03\x03\x04"

# If we don't see a fresh boot banner within this many seconds of sending
# the soft reset, escalate to a hardware reset (if enabled below).
BOOT_TIMEOUT = 12

# Hardware reset line, wired from a Pi GPIO pin to the board's RESET pin.
# See README.md for wiring. Leave this False if you haven't wired it up --
# the soft reset alone handles the vast majority of MicroPython exceptions.
ENABLE_GPIO_RESET = False
GPIO_RESET_PIN = 17            # BCM numbering
GPIO_RESET_PULSE = 0.25        # seconds to hold RESET low

# --- Disk usage ------------------------------------------------------------
# Full raw logs are always kept for EXCEPTION and ANOMALY sessions (they're
# rare and are exactly what you want to go back and read). Plain NORMAL
# session logs are kept as a rolling window so the SD card doesn't fill up
# over months of unattended running. Set to 0 to disable pruning entirely.
NORMAL_LOG_RETENTION = 500

# --- Web UI ------------------------------------------------------------
WEB_HOST = "0.0.0.0"
WEB_PORT = 8080
