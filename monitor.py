"""
SerialSessionMonitor

Reads the MicroPython board's serial output the same way the original
check.py did (open port -> read lines until SerialException -> reopen),
but treats each connect/disconnect cycle as a "session" and:

  * writes every session to its own log file under DATA_DIR/sessions/
  * appends one line of metadata per session to DATA_DIR/index.jsonl
  * tags each session NORMAL / ANOMALY / EXCEPTION / STOPPED
  * on EXCEPTION, tries a soft reset (Ctrl-D over serial) and, if that
    doesn't produce a fresh boot within BOOT_TIMEOUT seconds, escalates to
    a hardware reset over GPIO (if you've wired one up)
  * lets the web UI arm a deliberate "stop on next update" (Ctrl-C, parks
    the board at the REPL), "resume" (Ctrl-D), and an unconditional
    "reset now" that works regardless of current state
  * lets the web UI pause/resume monitoring outright -- closing the serial
    port entirely so an external tool (mpremote, a terminal, whatever) can
    have it, without needing to touch the systemd service at all
  * lets the web UI erase all session history and start numbering over

Session files are numbered sequentially (000123_NORMAL.log, ...), so
"the session before/after" a flagged one is just id-1 / id+1 -- no need to
duplicate content, the id is the pointer.

Read loop notes
----------------
pyserial's Serial.readline() reads one byte per syscall (it's the default
io.RawIOBase behaviour). That's fine on a fast laptop but on a single-core
Pi Zero W it can't always keep up with bursty output (a traceback, or the
~50 NMEA lines a GPS sync dumps in under a second) -- Python falls behind,
the kernel's USB CDC buffer fills up, and lines get dropped. The loop below
blocks for the first byte (so it still sleeps properly when idle) and then
drains everything else currently buffered in one read() call, which turns
"one syscall per byte" into "about one syscall per burst".
"""

import collections
import glob
import json
import os
import subprocess
import threading
import time

import serial


class SerialSessionMonitor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()

        self.connected = False
        self.live_tail = collections.deque(maxlen=cfg.LIVE_TAIL_LINES)
        self.stats = {"NORMAL": 0, "ANOMALY": 0, "EXCEPTION": 0, "STOPPED": 0}
        self.last_session_id = 0

        # Cross-session state for the manual stop/resume feature.
        self._ser = None               # the live Serial object, or None
        self._stop_armed = False       # "halt it the next time it's connected"
        self._currently_halted = False # true while it's sitting at the REPL because we put it there

        # Cross-session state for pausing monitoring entirely (mpremote, etc).
        self._monitoring_on = True

        self._normal_files = collections.deque()  # rolling retention window

        self.sessions_dir = os.path.join(cfg.DATA_DIR, "sessions")
        self.index_path = os.path.join(cfg.DATA_DIR, "index.jsonl")
        os.makedirs(self.sessions_dir, exist_ok=True)

        self._load_next_id_from_disk()
        self._reset_session_state()

    # ------------------------------------------------------------------ #
    # startup helpers
    # ------------------------------------------------------------------ #
    def _load_next_id_from_disk(self):
        """Pick up numbering where a previous run left off."""
        self.next_id = 1
        if os.path.exists(self.index_path):
            last_line = None
            with open(self.index_path, "r") as f:
                for line in f:
                    if line.strip():
                        last_line = line
            if last_line:
                self.next_id = json.loads(last_line)["id"] + 1
        self.last_session_id = self.next_id - 1

    def _reset_session_state(self):
        """Per-session state -- cleared at the start of every session.
        (Cross-session state like _stop_armed / _currently_halted lives on
        self directly and is NOT touched here.)"""
        self._lines = []
        self._start_ts = None
        self._is_exception = False
        self._user_stopped = False     # this session's halt was OUR Ctrl-C, not a real bug
        self._pending_soft_reset_at = None
        self._stop_send_at = None
        self._awaiting_boot = False
        self._boot_deadline = None

    # ------------------------------------------------------------------ #
    # main loop -- call this from a background thread
    # ------------------------------------------------------------------ #
    def run_forever(self):
        cfg = self.cfg
        while True:
            if not self._monitoring_enabled():
                # Paused: don't even try to open the port, so something
                # else (mpremote, a terminal) can grab it freely.
                with self.lock:
                    self.connected = False
                    self._ser = None
                    self._currently_halted = False
                time.sleep(cfg.MONITORING_PAUSE_POLL_INTERVAL)
                continue

            try:
                with serial.Serial(cfg.SERIAL_PORT, cfg.BAUD,
                                    timeout=cfg.SERIAL_READ_TIMEOUT) as ser:
                    with self.lock:
                        self.connected = True
                        self._ser = ser
                    self._begin_session()
                    self._arm_pending_stop_if_requested()

                    buf = b""
                    # Exits (closing the port via the `with` above) either
                    # on a real disconnect (caught below) or because
                    # monitoring got paused mid-session.
                    while self._monitoring_enabled():
                        n = ser.in_waiting
                        chunk = ser.read(n if n else 1)   # blocks up to timeout when idle
                        if chunk:
                            buf += chunk
                            while b"\n" in buf:
                                raw, buf = buf.split(b"\n", 1)
                                self._handle_line(ser, raw.decode(errors="ignore") + "\n")
                        self._check_boot_timeout()
                        self._check_pending_stop(ser)
            except serial.SerialException:
                pass  # board went to sleep / disconnected -- handled below either way

            with self.lock:
                self.connected = False
                self._ser = None
                self._currently_halted = False   # can't stay "halted" through a disconnect
            self._finish_session()
            time.sleep(0.2)

    # ------------------------------------------------------------------ #
    # session lifecycle
    # ------------------------------------------------------------------ #
    def _begin_session(self):
        self._reset_session_state()
        self._start_ts = time.time()

    def _arm_pending_stop_if_requested(self):
        with self.lock:
            armed = self._stop_armed
        if armed:
            self._stop_send_at = time.time() + self.cfg.STOP_SEND_DELAY

    def _handle_line(self, ser, line):
        now = time.time()

        if self.cfg.BOOT_BANNER_MARKER in line:
            with self.lock:
                self._currently_halted = False
            if self._awaiting_boot:
                # Fresh boot right after our own soft reset -- split into a
                # new session so the exception log doesn't get merged with
                # the clean run that follows it.
                self._awaiting_boot = False
                self._finish_session()
                self._begin_session()

        with self.lock:
            self._lines.append(line)
            self.live_tail.append(line.rstrip("\n"))

        if not self._is_exception and not self._user_stopped:
            for marker in self.cfg.EXCEPTION_MARKERS:
                if marker in line:
                    self._is_exception = True
                    self._pending_soft_reset_at = now + self.cfg.SOFT_RESET_DELAY
                    break

        if self._pending_soft_reset_at and now >= self._pending_soft_reset_at:
            self._trigger_soft_reset(ser)

    def _finish_session(self):
        if self._start_ts is None:
            return
        duration = time.time() - self._start_ts
        text = "".join(self._lines)

        if self._user_stopped:
            status = "STOPPED"
        elif self._is_exception:
            status = "EXCEPTION"
        elif self.cfg.GOTOSLEEP_MARKER not in text:
            status = "ANOMALY"
        else:
            status = "NORMAL"

        with self.lock:
            sid = self.next_id
            self.next_id += 1
            self.last_session_id = sid
            self.stats[status] += 1

        fname = f"{sid:06d}_{status}.log"
        path = os.path.join(self.sessions_dir, fname)
        with open(path, "w") as f:
            f.write(text)

        record = {
            "id": sid,
            "status": status,
            "start": self._start_ts,
            "duration": round(duration, 3),
            "lines": len(self._lines),
            "file": fname,
        }
        with open(self.index_path, "a") as f:
            f.write(json.dumps(record) + "\n")

        if status == "NORMAL" and self.cfg.NORMAL_LOG_RETENTION:
            self._prune_normal(path)

        self._reset_session_state()

    def _prune_normal(self, path):
        self._normal_files.append(path)
        while len(self._normal_files) > self.cfg.NORMAL_LOG_RETENTION:
            old = self._normal_files.popleft()
            try:
                os.remove(old)
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------------ #
    # automatic recovery from an unplanned exception
    # ------------------------------------------------------------------ #
    def _trigger_soft_reset(self, ser):
        try:
            ser.write(self.cfg.SOFT_RESET_BYTES)
            ser.flush()
        except Exception:
            pass
        self._pending_soft_reset_at = None
        self._awaiting_boot = True
        self._boot_deadline = time.time() + self.cfg.BOOT_TIMEOUT

    def _check_boot_timeout(self):
        if self._awaiting_boot and time.time() > self._boot_deadline:
            self._awaiting_boot = False
            self._hard_reset()

    def _hard_reset(self):
        if not self.cfg.ENABLE_GPIO_RESET:
            return
        try:
            import RPi.GPIO as GPIO

            pin = self.cfg.GPIO_RESET_PIN
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.LOW)         # pull RESET low
            time.sleep(self.cfg.GPIO_RESET_PULSE)
            GPIO.setup(pin, GPIO.IN)           # release back to Hi-Z,
            GPIO.cleanup(pin)                  # let the board's own pull-up recover it
        except Exception:
            # Don't let a GPIO problem take the monitor thread down.
            pass

    # ------------------------------------------------------------------ #
    # deliberate stop / resume -- called from Flask request threads
    # ------------------------------------------------------------------ #
    def request_stop(self):
        """Arm a halt for the next time the board is connected. If it's
        connected right now, send it immediately too."""
        with self.lock:
            self._stop_armed = True
            ser, live = self._ser, self.connected
        if live and ser is not None:
            self._send_stop_keys(ser)

    def resume(self):
        """Send Ctrl-D right now, if it's currently sitting halted (i.e.
        this is specifically the counterpart to request_stop() -- for an
        unconditional "reboot it now regardless of state", see
        reset_device() below)."""
        with self.lock:
            self._stop_armed = False
            ser, live, was_halted = self._ser, self.connected, self._currently_halted
            self._currently_halted = False
        if live and ser is not None and was_halted:
            try:
                ser.write(b"\x04")
                ser.flush()
            except Exception:
                pass

    def reset_device(self):
        """Manual reset, available any time regardless of current state.
        If the board is connected right now, this sends the same
        soft-reset keystrokes used for automatic exception recovery, and
        the normal boot-timeout escalation to the hardware reset line (if
        configured) still applies if it doesn't come back. If it's not
        connected at all (asleep, or not enumerating), soft reset over
        serial isn't possible -- the hardware line, if wired, is fired
        immediately instead since it doesn't need the USB link."""
        with self.lock:
            ser, live = self._ser, self.connected
        if live and ser is not None:
            try:
                ser.write(self.cfg.SOFT_RESET_BYTES)
                ser.flush()
            except Exception:
                pass
            self._awaiting_boot = True
            self._boot_deadline = time.time() + self.cfg.BOOT_TIMEOUT
            with self.lock:
                self._currently_halted = False
                self._stop_armed = False
        else:
            self._hard_reset()

    def _check_pending_stop(self, ser):
        if self._stop_send_at and time.time() >= self._stop_send_at:
            self._send_stop_keys(ser)
            self._stop_send_at = None

    def _send_stop_keys(self, ser):
        try:
            ser.write(self.cfg.STOP_BYTES)
            ser.flush()
        except Exception:
            pass
        # This session's exception marker(s), if any (MicroPython prints a
        # KeyboardInterrupt traceback for our own Ctrl-C), are expected --
        # don't treat it as a bug and don't let the auto-reset logic
        # override a deliberate stop.
        self._user_stopped = True
        self._pending_soft_reset_at = None
        with self.lock:
            self._stop_armed = False
            self._currently_halted = True

    # ------------------------------------------------------------------ #
    # pause / resume monitoring outright (frees the port for mpremote etc)
    # ------------------------------------------------------------------ #
    def _monitoring_enabled(self):
        with self.lock:
            return self._monitoring_on

    def pause_monitoring(self):
        """Stop touching the serial port at all -- the run_forever loop
        notices within one read timeout and closes it, so an external
        tool can open it right after."""
        with self.lock:
            self._monitoring_on = False

    def resume_monitoring(self):
        with self.lock:
            self._monitoring_on = True

    # ------------------------------------------------------------------ #
    # read-only helpers for the web UI
    # ------------------------------------------------------------------ #
    def get_status(self):
        with self.lock:
            return {
                "connected": self.connected,
                "monitoring": self._monitoring_on,
                "stats": dict(self.stats),
                "last_session_id": self.last_session_id,
                "stop_armed": self._stop_armed,
                "halted": self._currently_halted,
            }

    def get_tail(self):
        with self.lock:
            return list(self.live_tail)

    def recent_sessions(self, n=100, status=None):
        if not os.path.exists(self.index_path):
            return []
        try:
            out = subprocess.run(
                ["tail", "-n", str(n * 4 if status else n), self.index_path],
                capture_output=True, text=True, timeout=5,
            )
            lines = out.stdout.splitlines()
        except Exception:
            with open(self.index_path) as f:
                lines = f.readlines()[-(n * 4 if status else n):]

        records = [json.loads(l) for l in lines if l.strip()]
        if status:
            records = [r for r in records if r["status"] == status]
        return list(reversed(records[-n:]))

    def find_record(self, sid):
        """Look up one session's index metadata by id (small linear grep)."""
        if not os.path.exists(self.index_path):
            return None
        needle = f'"id": {sid},'
        try:
            out = subprocess.run(
                ["grep", "-F", needle, self.index_path],
                capture_output=True, text=True, timeout=5,
            )
            for line in out.stdout.splitlines():
                rec = json.loads(line)
                if rec["id"] == sid:
                    return rec
        except Exception:
            pass
        return None

    def read_session_text(self, sid):
        matches = glob.glob(os.path.join(self.sessions_dir, f"{sid:06d}_*.log"))
        if not matches:
            return None
        with open(matches[0]) as f:
            return f.read()

    # ------------------------------------------------------------------ #
    # erase all history -- called from a Flask request thread
    # ------------------------------------------------------------------ #
    def erase_all_sessions(self):
        """Delete every session log and the index, and reset numbering
        back to #1. Doesn't touch the live connection or in-progress
        session -- if one finishes at the exact moment this runs, it may
        land back on disk right after (a harmless, rare race; not worth
        adding lock contention on the serial read path to close it)."""
        for f in glob.glob(os.path.join(self.sessions_dir, "*.log")):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
        try:
            os.remove(self.index_path)
        except FileNotFoundError:
            pass
        with self.lock:
            self.stats = {"NORMAL": 0, "ANOMALY": 0, "EXCEPTION": 0, "STOPPED": 0}
            self.next_id = 1
            self.last_session_id = 0
        self._normal_files.clear()
