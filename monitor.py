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
    have it, without needing to touch the systemd service at all. If a
    device stop is armed-but-not-yet-in-effect when pause is requested,
    the pause is deferred until the stop actually completes, so an armed
    stop can never get stranded behind a port that's already closed.
  * lets the web UI erase all session history and start numbering over

Session files are numbered sequentially (000123_NORMAL.log, ...), so
"the session before/after" a flagged one is just id-1 / id+1 -- no need to
duplicate content, the id is the pointer. Cumulative counts (self.stats)
are restored from index.jsonl on startup, not just zeroed and rebuilt from
scratch -- a restart shouldn't make hundreds of already-recorded sessions
disappear from the Live page's counters.

Read loop notes
----------------
Linux device-arrival notifications wake capture immediately when the serial
node appears. CaptureSerial preserves queued startup input that pyserial
normally flushes during open. A select + single OS read drains each burst
without an in_waiting ioctl or a multi-read that can throw away partial
results on disconnect. Lines are split in one pass, and an unterminated
final line is retained when the device sleeps. See serial_capture.py.

Reliability notes
------------------
run_forever() is wrapped so it can never die silently: any exception
(not just the expected serial.SerialException from a normal disconnect)
is logged with a full traceback and the loop just retries, rather than
the whole monitor thread quietly evaporating. seconds_since_alive() feeds
watchdog.py's health check, and _finish_session() checks free disk space
before writing a session's raw text so a full SD card degrades (skips the
raw log, keeps the index entry, logs a warning) instead of throwing.
None of this can fix a genuine kernel/USB-level lockup on its own -- see
the docs/GUIDE.md "if it stops responding" section for that.
"""

import collections
import glob
import json
import logging
import os
import shutil
import subprocess
import threading
import time

import serial
from serial_capture import CaptureSerial, read_chunk, wait_for_port

logger = logging.getLogger("guardian")


class SerialSessionMonitor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self._serial_write_lock = threading.Lock()

        self.connected = False
        self.boot_count = 0
        self.stop_count = 0
        self.serial_connections = 0
        self.serial_bytes_received = 0
        self.empty_sessions = 0
        self.last_serial_open_error = None
        self._terminal_active = False
        self._terminal_session = 0
        self._terminal_buffer = bytearray()
        self._terminal_end = 0
        self.live_tail = collections.deque(maxlen=cfg.LIVE_TAIL_LINES)
        self.live_times = collections.deque(maxlen=cfg.LIVE_TAIL_LINES)
        self.live_started = time.time()
        self.last_session_id = 0

        # Cross-session state for the manual stop/resume feature.
        self._ser = None               # the live Serial object, or None
        self._stop_armed = False       # "halt it the next time it's connected"
        self._currently_halted = False # true while it's sitting at the REPL because we put it there

        # Cross-session state for pausing monitoring entirely (mpremote, etc).
        self._monitoring_on = True
        self._pause_pending = False    # a pause is queued behind an armed-but-not-yet-halted stop

        # Watchdog heartbeat -- see seconds_since_alive() / watchdog.py.
        self._last_alive = time.time()

        self._normal_files = collections.deque()  # rolling retention window

        self.sessions_dir = os.path.join(cfg.DATA_DIR, "sessions")
        self.index_path = os.path.join(cfg.DATA_DIR, "index.jsonl")
        os.makedirs(self.sessions_dir, exist_ok=True)

        self._load_state_from_disk()
        self._reset_session_state()

    # ------------------------------------------------------------------ #
    # startup helpers
    # ------------------------------------------------------------------ #
    def _load_state_from_disk(self):
        """Restore numbering AND cumulative stats from a previous run's
        index.jsonl, so a restart doesn't zero out counts that reflect
        sessions already recorded -- read the whole file once, at
        startup only (not on any per-request path)."""
        self.next_id = 1
        self.stats = {"NORMAL": 0, "ANOMALY": 0, "EXCEPTION": 0, "STOPPED": 0}
        if not os.path.exists(self.index_path):
            self.last_session_id = 0
            return

        last_line = None
        with open(self.index_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                last_line = line
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                status = rec.get("status")
                if status in self.stats:
                    self.stats[status] += 1

        if last_line:
            try:
                self.next_id = json.loads(last_line)["id"] + 1
            except (json.JSONDecodeError, KeyError):
                pass
        self.last_session_id = self.next_id - 1

    def _reset_session_state(self):
        """Per-session state -- cleared at the start of every session.
        (Cross-session state like _stop_armed / _currently_halted lives on
        self directly and is NOT touched here.)"""
        self._lines = []
        self._start_ts = None
        self._is_exception = False
        self._exception_marker = None
        self._user_stopped = False     # this session's halt was OUR Ctrl-C, not a real bug
        self._pending_soft_reset_at = None
        self._awaiting_boot = False
        self._boot_deadline = None

    # ------------------------------------------------------------------ #
    # main loop -- call this from a background thread
    # ------------------------------------------------------------------ #
    def run_forever(self):
        """Never returns, and never dies quietly. _run_one_cycle() does the
        real work; anything it raises -- not just the expected
        serial.SerialException from a normal disconnect, but anything at
        all (a disk error, a bug, whatever) -- is caught here, logged with
        a full traceback, and the loop just tries again a second later."""
        logger.info("monitor thread starting (port=%s)", self.cfg.SERIAL_PORT)
        while True:
            self._last_alive = time.time()
            try:
                self._run_one_cycle()
            except Exception:
                logger.exception("monitor loop hit an unexpected error -- recovering")
                with self.lock:
                    self.connected = False
                    self._ser = None
                    self._currently_halted = False
                try:
                    self._finish_session()
                except Exception:
                    logger.exception("also failed to finish the in-flight session while recovering")
                time.sleep(1.0)

    def seconds_since_alive(self):
        """How long since the loop last ticked -- used by watchdog.py to
        decide whether to keep telling systemd we're alive."""
        return time.time() - self._last_alive

    def _run_one_cycle(self):
        cfg = self.cfg
        if not self._monitoring_enabled():
            # Paused: don't even try to open the port, so something
            # else (mpremote, a terminal) can grab it freely.
            with self.lock:
                self.connected = False
                self._ser = None
                self._currently_halted = False
            time.sleep(cfg.MONITORING_PAUSE_POLL_INTERVAL)
            return

        if not wait_for_port(cfg.SERIAL_PORT):
            return
        buf = b""
        ser = None
        try:
            with CaptureSerial(cfg.SERIAL_PORT, cfg.BAUD,
                                timeout=cfg.SERIAL_READ_TIMEOUT, write_timeout=1.0) as ser:
                with self.lock:
                    self.connected = True
                    self._ser = ser
                    self.serial_connections += 1
                    self.last_serial_open_error = None
                self._begin_session()
                self._send_armed_stop_if_any(ser)

                # Exits (closing the port via the `with` above) either
                # on a real disconnect (caught below) or because
                # monitoring got paused mid-session.
                while self._monitoring_enabled():
                    self._last_alive = time.time()
                    chunk = read_chunk(ser, min(cfg.SERIAL_READ_TIMEOUT, 0.1))
                    if chunk:
                        self._record_serial_bytes(chunk)
                        parts = (buf + chunk).split(b"\n")
                        buf = parts.pop()
                        for raw in parts:
                            self._handle_line(ser, raw.decode(errors="replace") + "\n")
                    self._check_boot_timeout(ser)
        except serial.SerialException as exc:
            if ser is None:
                with self.lock:
                    self.last_serial_open_error = str(exc)
            # A read failure after a successful open usually means sleep/USB removal.
        finally:
            # Devices may sleep without a final newline. Preserve that text,
            # including a clean-shutdown marker at the end of a short burst.
            if buf:
                self._handle_line(ser, buf.decode(errors="replace"))

        with self.lock:
            self.connected = False
            self._ser = None
            self._currently_halted = False   # can't stay "halted" through a disconnect
            if self._terminal_active and self._monitoring_on:
                self._stop_armed = True
        self._finish_session()
        # Briefly back off if udev has not removed the old node yet. Device
        # creation after removal is handled by inotify on the next cycle.
        if os.path.exists(cfg.SERIAL_PORT):
            time.sleep(0.02)

    # ------------------------------------------------------------------ #
    # session lifecycle
    # ------------------------------------------------------------------ #
    def _begin_session(self):
        self._reset_session_state()
        self._start_ts = time.time()

    def _send_armed_stop_if_any(self, ser):
        """If a stop was armed while disconnected, deliver it the instant
        we have a live connection -- before any of the board's own output,
        and with no artificial delay. There used to be a settle delay here
        (STOP_SEND_DELAY); it's gone because some of this board's wake
        cycles complete in well under half a second, shorter than that
        delay -- the stop would miss its window entirely, stay armed, and
        just keep re-rolling the dice on the next (also possibly too-short)
        cycle, sometimes for a long time before a slow-enough wake let it
        land. Sending first-thing removes the race altogether."""
        with self.lock:
            armed = self._stop_armed
        if armed:
            self._send_stop_keys(ser)

    def _handle_line(self, ser, line):
        now = time.time()

        if self.cfg.BOOT_BANNER_MARKER in line:
            with self.lock:
                self.boot_count += 1
                # Startup output can already be queued when we interrupt
                # on open. It must not undo the deliberate REPL state.
                if not self._user_stopped:
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
            self.live_times.append(now)

        if not self._is_exception and not self._user_stopped:
            for marker in self.cfg.EXCEPTION_MARKERS:
                if marker in line:
                    self._is_exception = True
                    self._exception_marker = marker
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
            if not self._lines:
                self.empty_sessions += 1

        fname = f"{sid:06d}_{status}.log"
        path = os.path.join(self.sessions_dir, fname)
        if self._has_disk_space():
            with open(path, "w") as f:
                f.write(text)
        else:
            logger.warning("low disk space -- skipped writing raw log for session #%d (%s)", sid, status)

        record = {
            "id": sid,
            "status": status,
            "start": self._start_ts,
            "duration": round(duration, 3),
            "lines": len(self._lines),
            "file": fname,
            "exception_marker": self._exception_marker,
        }
        with open(self.index_path, "a") as f:
            f.write(json.dumps(record) + "\n")

        if status == "NORMAL" and self.cfg.NORMAL_LOG_RETENTION:
            self._prune_normal(path)

        if status != "NORMAL":
            logger.info("session #%d: %s (%.1fs, %d lines)", sid, status, duration, len(self._lines))

        self._reset_session_state()

    def _has_disk_space(self):
        """Guard against a full SD card turning a write failure into an
        unhandled OSError -- checked before every raw session-log write,
        not just once, since free space can change while running."""
        try:
            free_mb = shutil.disk_usage(self.cfg.DATA_DIR).free / (1024 * 1024)
            return free_mb >= self.cfg.MIN_FREE_DISK_MB
        except Exception:
            return True  # if we can't even check, don't block writes over it

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
            with self._serial_write_lock:
                ser.write(self.cfg.SOFT_RESET_BYTES)
                ser.flush()
        except Exception:
            pass
        self._pending_soft_reset_at = None
        self._awaiting_boot = True
        self._boot_deadline = time.time() + self.cfg.BOOT_TIMEOUT

    def _check_boot_timeout(self, ser=None):
        # Recovery must run even if an exception is the board's final line.
        # The read loop wakes at least every 100 ms, independently of a long
        # user-configured read timeout.
        if ser is not None and self._pending_soft_reset_at and time.time() >= self._pending_soft_reset_at:
            self._trigger_soft_reset(ser)
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
    def request_repl(self):
        """Catch the next connection and interrupt into the REPL.

        Unlike the Stop toggle, repeated requests keep the interrupt armed.
        Resume monitoring and cancel a queued pause so the port stays open
        to catch the board and display its prompt.
        """
        with self.lock:
            if not self._terminal_active:
                self._terminal_session += 1
            self._terminal_active = True
            self._monitoring_on = True
            self._pause_pending = False
            if self._currently_halted or self._stop_armed:
                return
            self._stop_armed = True
            ser, live = self._ser, self.connected
        logger.info("device REPL requested -- interrupt on next connection")
        if live and ser is not None:
            self._send_stop_keys(ser)

    def close_repl(self):
        """Leave browser input mode without rebooting or resuming the board."""
        with self.lock:
            self._terminal_active = False
            if self._stop_armed and not self._currently_halted:
                self._stop_armed = False
                self._pause_pending = False

    def _record_serial_bytes(self, data):
        # Keep raw prompts, echoes and ANSI sequences independently of the
        # newline-based session logger. Memory stays bounded at 64 KiB.
        with self.lock:
            self.serial_bytes_received += len(data)
            self._terminal_end += len(data)
            self._terminal_buffer.extend(data)
            del self._terminal_buffer[:-65536]

    def get_terminal_output(self, after=None):
        with self.lock:
            end = self._terminal_end
            start = end - len(self._terminal_buffer)
            clipped = after is not None and not start <= after <= end
            offset = max(start, min(end, after)) if after is not None else start
            if clipped:
                offset = start
            return bytes(self._terminal_buffer[offset-start:]), end, clipped

    def write_repl(self, data, connection, session):
        """Write browser keystrokes through the monitor's existing port.

        Never buffer input for a later connection: stale commands must not
        execute on a subsequent wake after a disconnect or deployment.
        """
        with self._serial_write_lock:
            with self.lock:
                if (not self._terminal_active or not self._monitoring_on
                        or not self.connected or self._ser is None or self._stop_armed
                        or connection != self.serial_connections or session != self._terminal_session):
                    raise ValueError("Terminal is not connected. Input was not sent.")
                ser = self._ser
            if ser.write(data) != len(data):
                raise OSError("Incomplete serial write. Input was not retried.")
            # Ctrl-D may run the program again without closing USB. Don't
            # leave the transport showing a deliberate halt after that.
            interrupt, restart = data.rfind(b"\x03"), data.rfind(b"\x04")
            if max(interrupt, restart) >= 0:
                with self.lock:
                    if self._ser is ser and self.connected:
                        self._currently_halted = interrupt > restart

    def request_stop(self):
        """Arm a halt for the next time the board is connected, sending
        it immediately if it's connected right now. Calling this again
        while a stop is already armed but hasn't taken effect yet
        cancels it instead -- acts as a toggle in that specific state --
        and drops any pause that was queued behind it too, since there's
        nothing left for it to wait on."""
        with self.lock:
            if self._stop_armed and not self._currently_halted:
                self._terminal_active = False
                self._stop_armed = False
                had_pending_pause = self._pause_pending
                self._pause_pending = False
                cancelled = True
            else:
                cancelled = False
                self._stop_armed = True
                ser, live = self._ser, self.connected

        if cancelled:
            logger.info(
                "pending device stop cancelled%s",
                " (queued pause also cancelled)" if had_pending_pause else "",
            )
            return

        logger.info("device stop requested")
        if live and ser is not None:
            self._send_stop_keys(ser)

    def resume(self):
        """Send Ctrl-D right now, if it's currently sitting halted (i.e.
        this is specifically the counterpart to request_stop() -- for an
        unconditional "reboot it now regardless of state", see
        reset_device() below)."""
        logger.info("device resume requested")
        with self.lock:
            self._terminal_active = False
            was_armed_not_halted = self._stop_armed and not self._currently_halted
            self._stop_armed = False
            ser, live, was_halted = self._ser, self.connected, self._currently_halted
            self._currently_halted = False
            if was_armed_not_halted and self._pause_pending:
                self._pause_pending = False
                logger.info("pending pause cancelled -- the stop it was waiting for was cancelled first")
        if live and ser is not None and was_halted:
            try:
                with self._serial_write_lock:
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
        logger.info("manual device reset requested")
        with self.lock:
            self._terminal_active = False
            ser, live = self._ser, self.connected
        if live and ser is not None:
            try:
                with self._serial_write_lock:
                    ser.write(self.cfg.SOFT_RESET_BYTES)
                    ser.flush()
            except Exception:
                pass
            self._awaiting_boot = True
            self._boot_deadline = time.time() + self.cfg.BOOT_TIMEOUT
            with self.lock:
                self._currently_halted = False
                self._stop_armed = False
                if self._pause_pending:
                    self._pause_pending = False
                    logger.info("pending pause cancelled -- device reset abandoned the stop it was waiting on")
        else:
            self._hard_reset()

    def _send_stop_keys(self, ser):
        try:
            with self._serial_write_lock:
                ser.write(self.cfg.STOP_BYTES)
                ser.flush()
        except Exception:
            logger.warning("could not send device stop keys", exc_info=True)
            return
        # This session's exception marker(s), if any (MicroPython prints a
        # KeyboardInterrupt traceback for our own Ctrl-C), are expected --
        # don't treat it as a bug and don't let the auto-reset logic
        # override a deliberate stop.
        self._user_stopped = True
        self._pending_soft_reset_at = None
        self._awaiting_boot = False
        self._boot_deadline = None
        apply_deferred_pause = False
        with self.lock:
            self._stop_armed = False
            self._currently_halted = True
            self.stop_count += 1
            if self._pause_pending:
                self._pause_pending = False
                self._monitoring_on = False
                apply_deferred_pause = True
        if apply_deferred_pause:
            logger.info("device stop completed -- applying the pause that was waiting on it")

    # ------------------------------------------------------------------ #
    # pause / resume monitoring outright (frees the port for mpremote etc)
    # ------------------------------------------------------------------ #
    def _monitoring_enabled(self):
        with self.lock:
            return self._monitoring_on

    def pause_monitoring(self):
        """Stop touching the serial port at all -- the run_forever loop
        notices within one read timeout and closes it, so an external
        tool can open it right after.

        If a device stop is currently armed but hasn't taken effect yet
        (Stop was pressed, then Pause before it actually connected and
        halted), this defers the actual pause until the stop completes,
        rather than closing the port out from under it -- otherwise an
        armed-but-not-yet-connected stop could never be delivered once
        monitoring stops trying to open the port at all. Calling this
        again while a pause is already queued that way cancels the
        queued pause instead -- acts as a toggle in that specific state
        -- leaving the stop itself untouched."""
        with self.lock:
            self._terminal_active = False
            if self._pause_pending:
                self._pause_pending = False
                action = "cancel"
            elif self._stop_armed and not self._currently_halted:
                self._pause_pending = True
                action = "queue"
            else:
                self._monitoring_on = False
                action = "pause"

        if action == "cancel":
            logger.info("pending pause cancelled")
        elif action == "queue":
            logger.info("pause monitoring requested -- deferring until the pending device stop completes")
        else:
            logger.info("monitoring paused")

    def resume_monitoring(self):
        logger.info("monitoring resumed")
        with self.lock:
            self._monitoring_on = True
            self._pause_pending = False

    # ------------------------------------------------------------------ #
    # read-only helpers for the web UI
    # ------------------------------------------------------------------ #
    def get_status(self):
        with self.lock:
            return {
                "connected": self.connected,
                "serial_open": self._ser is not None,
                "serial_port": self.cfg.SERIAL_PORT,
                "boot_count": self.boot_count,
                "stop_count": self.stop_count,
                "serial_connections": self.serial_connections,
                "serial_bytes_received": self.serial_bytes_received,
                "empty_sessions": self.empty_sessions,
                "last_serial_open_error": self.last_serial_open_error,
                "monitoring": self._monitoring_on,
                "pause_pending": self._pause_pending,
                "stats": dict(self.stats),
                "last_session_id": self.last_session_id,
                "stop_armed": self._stop_armed,
                "halted": self._currently_halted,
                "terminal_active": self._terminal_active,
                "terminal_session": self._terminal_session,
            }

    def get_tail(self):
        with self.lock:
            return list(self.live_tail)

    def get_timed_tail(self):
        """A bounded timestamped snapshot for interleaving timeline labels."""
        with self.lock:
            return [{"type": "log", "ts": ts, "text": text}
                    for ts, text in zip(self.live_times, self.live_tail)]

    def resize_live_tail(self):
        """Rebuild the live-tail buffer to the current cfg.LIVE_TAIL_LINES
        (a plain deque's maxlen can't be changed after creation). Cheap
        and safe to call any time -- e.g. after a Settings-page save,
        whether or not that particular value actually changed."""
        with self.lock:
            self.live_tail = collections.deque(self.live_tail, maxlen=self.cfg.LIVE_TAIL_LINES)
            self.live_times = collections.deque(self.live_times, maxlen=self.cfg.LIVE_TAIL_LINES)

    def recent_sessions(self, n=100, status=None):
        return self.recent_sessions_window(n, status)[0]

    def recent_sessions_window(self, n=100, status=None):
        """Like recent_sessions(), but also returns `floor`: the start time
        of the oldest index line that was scanned (before any status
        filtering), or 0.0 if the scan reached the start of the file.
        The Sessions page uses it to decide which labels belong in the
        view -- anything newer than `floor` -- so labels show up the same
        way whichever status filter is active."""
        if not os.path.exists(self.index_path):
            return [], 0.0
        count = n * 4 if status else n
        try:
            out = subprocess.run(
                ["tail", "-n", str(count), self.index_path],
                capture_output=True, text=True, timeout=5,
            )
            lines = out.stdout.splitlines()
        except Exception:
            with open(self.index_path) as f:
                lines = list(collections.deque(f, maxlen=count))

        records = [json.loads(l) for l in lines if l.strip()]
        floor = records[0]["start"] if records and len(lines) >= count else 0.0
        if status:
            records = [r for r in records if r["status"] == status]
        return list(reversed(records[-n:])), floor

    def iter_records(self):
        """Every index record, oldest first, streamed one line at a time
        (the index grows forever, so don't slurp it). Used by export."""
        if not os.path.exists(self.index_path):
            return
        with open(self.index_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

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

    def read_session_text(self, sid, max_chars=None):
        # Four direct paths instead of globbing every file in sessions/ for
        # every row/export entry. Log retention may leave many exception logs.
        for status in ("NORMAL", "ANOMALY", "EXCEPTION", "STOPPED"):
            path = os.path.join(self.sessions_dir, f"{sid:06d}_{status}.log")
            try:
                with open(path) as f:
                    return f.read(max_chars) if max_chars is not None else f.read()
            except FileNotFoundError:
                continue
        return None

    # ------------------------------------------------------------------ #
    # erase all history -- called from a Flask request thread
    # ------------------------------------------------------------------ #
    def erase_all_sessions(self):
        """Delete every session log and the index, and reset numbering
        back to #1. Doesn't touch the live connection or in-progress
        session -- if one finishes at the exact moment this runs, it may
        land back on disk right after (a harmless, rare race; not worth
        adding lock contention on the serial read path to close it)."""
        logger.warning("erasing all session history")
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
