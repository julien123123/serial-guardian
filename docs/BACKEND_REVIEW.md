# Capture reliability and backend review

Reviewed 2026-10-27. Changes are project-independent; no firmware project or
project-specific marker defaults were changed.

## Capture findings and fixes

- The old reconnect loop slept 200 ms after every failure/disconnect. Linux
  device-arrival/udev events now wake the opener immediately. A 20 ms retry
  fallback handles unsupported notification environments and nodes whose
  permissions are not ready. Idle waits are bounded to keep Pause responsive.
- On POSIX, pyserial flushes its receive queue during `open()`. That can erase
  the earliest messages of a brief connection. `CaptureSerial` skips only
  that automatic startup flush. Explicit input-buffer resets still work.
- Capture now uses readiness plus a single bounded OS read, avoiding a
  potentially failing `in_waiting` ioctl and pyserial losing an accumulated
  partial result if a subsequent internal read fails on disconnect.
- Lines are split in one pass per chunk. The last unterminated line is
  preserved on disconnect/Pause rather than silently discarded. UTF-8 byte
  fragments remain buffered until a line is complete.
- A 15-second configured read timeout does not delay bytes that are already
  available, but it previously delayed idle control/recovery checks. Each
  capture wait is now capped at 100 ms. Delayed soft resets are also checked
  during silence, not only when another line happens to arrive.
- New session records and JSON exports include the actual `exception_marker`
  that triggered an exception classification (or null). Historical records
  are not reclassified, and existing project-specific settings are preserved.
- `/api/status` includes process-lifetime `serial_connections`,
  `serial_bytes_received`, `empty_sessions`, and `last_serial_open_error` to
  distinguish successful opens with no text from open/permission failures.

Automated POSIX tests reproduce startup-buffer loss with stock pyserial,
verify the capture opener preserves those bytes, and verify explicit flushes.
A Linux pseudo-terminal test simulates 12 separate 100 ms connections with
30 UTF-8 lines plus an unterminated shutdown marker per connection. All 12
logs matched their input exactly. These tests do not emulate USB enumeration,
nRF52840 firmware, a Pi Zero CPU, or an SD card.

A separate 20-arrival filesystem notification check measured 0.929 ms median
and 16.051 ms maximum time from simulated node creation to wake-up on this
machine. Ten empty 100 ms waits took 1,013.9 ms wall time and 3.69 ms process
CPU time. These are Linux filesystem-event measurements, not USB timings.

## Backend findings

`read_session_text()` previously globbed the entire session directory for
every session. A 200-row page could therefore scan thousands of filenames
200 times. It now tries the four canonical status filenames directly and
handles concurrent pruning. Raw-log content and retention policy are unchanged.

Synthetic warm-cache benchmark: 5,000 stored sessions, 200 displayed rows,
default extraction fields, five measured requests after warm-up, Python 3.12
in the cloud development machine:

| Lookup method | Median page time | Median process CPU time |
| --- | ---: | ---: |
| Directory glob per row | 1,066.52 ms | 1,062.22 ms |
| Direct status paths | 32.03 ms | 25.20 ms |

This is about 33.3× faster for this fixture, not a Pi performance claim.
Reproduce with `python tools/benchmark_backend.py` in Guardian's environment;
its temporary fixture is removed afterward.
The fallback index reader now retains a bounded deque instead of calling
`readlines()` on the whole index.

Other reviewed paths:

- Live output has a bounded buffer, on-demand previews are capped, and export
  emits one log at a time; these behaviors are retained.
- Session-page field extraction still reads each displayed raw log. Large
  logs or expensive user regexes can still consume CPU. No log truncation or
  regex behavior was silently introduced for this optimization.
- Session persistence is still synchronous. Slow/full SD storage can delay
  capture between sessions; disk checks and error reporting remain in place.
  Moving writes into a separate queue would need explicit durability,
  backpressure, and Erase consistency work, so it was not bundled here.
- The NORMAL-log retention deque is not restored from historical index
  entries after a restart, so older normal logs can accumulate across service
  restarts. This review does not retroactively delete stored logs; restoring
  retention across restarts should be handled explicitly in a separate change.
- Label metadata is still loaded from its JSON file on live polls. This is
  not on the serial-reading path, but very large label histories could merit
  an invalidated cache after measurement on the target.

## Limits and checking a real device

Bytes printed before USB is usable, or left in a board's USB transmit queue
when it powers down, cannot be recovered by changing the host reader. The
read-only clock test-rig review found a direct SYSTEMOFF path without an
explicit USB-drain handshake. No changes were made to that repository.

Use the counters above and compare full session logs after updating Guardian
on the Pi. The browser's poll interval affects display delay, not serial
sampling. If a project uses custom boot/sleep messages, configure the markers
in Settings; do not assume another project's markers. The supplied clock
export contains EXCEPTION-tagged logs without the default exception strings,
and its current Settings screenshot also shows the default exception strings.
Those historical tags cannot be explained from the supplied text alone. The
new stored `exception_marker` will make future classifications traceable.
