"""
SerialSessionMonitor

Reads the MicroPython board's serial output the same way the original
check.py did (open port -> read lines until SerialException -> reopen),
but treats each connect/disconnect cycle as a "session" and:

  * writes every session to its own log file under DATA_DIR/sessions/
  * appends one line of metadata per session to DATA_DIR/index.jsonl
  * tags each session NORMAL / ANOMALY / EXCEPTION
  * on EXCEPTION, tries a soft reset (Ctrl-D over serial) and, if that
    doesn't produce a fresh boot within BOOT_TIMEOUT seconds, escalates to
    a hardware reset over GPIO (if you've wired one up)

Session files are numbered sequentially (000123_NORMAL.log, ...), so
"the session before/after" a flagged one is just id-1 / id+1 -- no need to
duplicate content, the id is the pointer.
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
        self.stats = {"NORMAL": 0, "ANOMALY": 0, "EXCEPTION": 0}
        self.last_session_id = 0

        self._normal_files = collections.deque()  # rolling retention window

        self.sessions_dir = os.path.join(cfg.DATA_DIR, "sessions")
        self.index_path = os.path.join(cfg.DATA_DIR, "index.jsonl")
        os.makedirs(self.sessions_dir, exist_ok=True)

        self._reset_next_id_from_disk()
        self._reset_session_state()

    # ------------------------------------------------------------------ #
    # startup helpers
    # ------------------------------------------------------------------ #
    def _reset_next_id_from_disk(self):
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
        self._lines = []
        self._start_ts = None
        self._is_exception = False
        self._pending_soft_reset_at = None
        self._awaiting_boot = False
        self._boot_deadline = None

    # ------------------------------------------------------------------ #
    # main loop -- call this from a background thread
    # ------------------------------------------------------------------ #
    def run_forever(self):
        cfg = self.cfg
        while True:
            try:
                with serial.Serial(cfg.SERIAL_PORT, cfg.BAUD, timeout=1) as ser:
                    with self.lock:
                        self.connected = True
                    self._begin_session()
                    while True:
                        raw = ser.readline()
                        if raw:
                            self._handle_line(ser, raw.decode(errors="ignore"))
                        self._check_boot_timeout()
            except serial.SerialException:
                with self.lock:
                    self.connected = False
                self._finish_session()
                time.sleep(0.2)

    # ------------------------------------------------------------------ #
    # session lifecycle
    # ------------------------------------------------------------------ #
    def _begin_session(self):
        self._reset_session_state()
        self._start_ts = time.time()

    def _handle_line(self, ser, line):
        now = time.time()

        # If this line is a fresh boot banner arriving right after we sent a
        # soft reset, split it into a new session instead of lumping the
        # post-reset run into the exception session.
        if self._awaiting_boot and self.cfg.BOOT_BANNER_MARKER in line:
            self._awaiting_boot = False
            self._finish_session()
            self._begin_session()

        with self.lock:
            self._lines.append(line)
            self.live_tail.append(line.rstrip("\n"))

        if not self._is_exception:
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

        if self._is_exception:
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
    # reset actions
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
    # read-only helpers for the web UI
    # ------------------------------------------------------------------ #
    def get_status(self):
        with self.lock:
            return {
                "connected": self.connected,
                "stats": dict(self.stats),
                "last_session_id": self.last_session_id,
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
