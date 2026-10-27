"""Low-latency serial capture helpers for Linux SBCs, with portable fallbacks."""
import ctypes
import os
import select
import sys
import time

import serial


class CaptureSerial(serial.Serial):
    """Retain bytes received during open instead of pyserial's startup flush.

    On POSIX pyserial 3.x calls _reset_input_buffer from open(). Explicit
    reset_input_buffer calls after opening still work normally.
    """
    def open(self):
        self._capture_opening = True
        try:
            return super().open()
        finally:
            self._capture_opening = False

    def _reset_input_buffer(self):
        if not getattr(self, '_capture_opening', False):
            return super()._reset_input_buffer()


def read_chunk(ser, timeout):
    """One OS read preserves a received burst even if the next read disconnects.

    Avoid in_waiting: its ioctl can fail on hangup before queued bytes are
    drained. Avoid pyserial's multi-read accumulation losing partial results
    when a later read inside the same call raises SerialException.
    """
    if os.name != 'posix':
        return ser.read(max(1, min(ser.in_waiting, 65536)))
    try:
        if not select.select([ser.fileno()], [], [], timeout)[0]:
            return b''
        chunk = os.read(ser.fileno(), 65536)
        if not chunk:
            raise serial.SerialException('serial device disconnected')
        return chunk
    except (BlockingIOError, InterruptedError):
        return b''
    except (OSError, ValueError) as exc:
        raise serial.SerialException(str(exc)) from exc


# Linux device-arrival notifications avoid both a 200 ms blind spot and a
# busy open/retry loop. They require no daemon or third-party Python package.
try:
    _libc = ctypes.CDLL(None, use_errno=True) if sys.platform.startswith('linux') else None
    if _libc is not None:
        _libc.inotify_init1.argtypes = [ctypes.c_int]
        _libc.inotify_init1.restype = ctypes.c_int
        _libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        _libc.inotify_add_watch.restype = ctypes.c_int
except (AttributeError, OSError):
    _libc = None


def wait_for_port(path, timeout=0.1):
    """Wake on device creation/udev permission updates; otherwise retry in 20 ms.

    Install the watch before checking existence, closing the arrival race.
    The bounded wait also lets monitoring Pause/configuration changes take effect.
    """
    if os.path.exists(path):
        return True
    fd = -1
    try:
        if _libc is not None:
            fd = _libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd >= 0 and _libc.inotify_add_watch(fd, os.fsencode(os.path.dirname(path) or '.'),
                                                  0x100 | 0x80 | 0x4) < 0:
                os.close(fd); fd = -1  # IN_CREATE | IN_MOVED_TO | IN_ATTRIB
        if os.path.exists(path):
            return True
        if fd < 0:
            time.sleep(min(timeout, 0.02))
            return os.path.exists(path)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                return False
            try: os.read(fd, 65536)
            except BlockingIOError: pass
            if os.path.exists(path):
                return True
    finally:
        if fd >= 0:
            os.close(fd)
