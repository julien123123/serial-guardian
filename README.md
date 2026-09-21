<img width="1845" height="1137" alt="image" src="https://github.com/user-attachments/assets/afebb60f-500d-4739-8cae-a15a125ce637" />

# Serial Guardian

Moves your `check.py` monitoring loop off the laptop and onto a Raspberry Pi
Zero W (v1), reachable over SSH and a small web dashboard. It also:

- splits the stream into **sessions** (one connect → disconnect cycle of the
  board's USB serial),
- tags each session `NORMAL` / `ANOMALY` (never reached `gotosleep()`) /
  `EXCEPTION` (traceback / MemoryError / etc.) / `STOPPED` (you halted it),
- on `EXCEPTION`, tries to recover the board automatically,
- lets you halt the board's normal wake/sleep cycle from the web UI and
  resume it later,
- lets you pause monitoring outright to free the serial port for
  `mpremote` or a terminal, without taking the web UI down,
- lets you reset the device unconditionally, and erase all session
  history with one click,
- lets you pull extra columns (like `REFRESH` or `reset cause`) out of
  each session's raw log with your own regexes, and tune almost every
  other setting too, all from a Settings page, live, no restart needed,
- survives its own crashes and a hung web server without your intervention
  (broad exception recovery, a disk-space guard, and a systemd watchdog
  that checks both halves of the app independently),
- keeps every `EXCEPTION` / `ANOMALY` / `STOPPED` log forever, and prunes
  old `NORMAL` logs so the SD card doesn't fill up.

Sessions are saved as `data/sessions/000123_STATUS.log`, numbered in order,
so "the session before/after #123" is just `#122` / `#124` — the id *is*
the pointer, no need to duplicate content around every flagged session.

---

## 1. Flash and provision the Pi headlessly

The Zero W only has 2.4 GHz Wi-Fi, which is fine here.

1. Raspberry Pi Imager → choose **Raspberry Pi OS Lite (32-bit)** (no
   desktop needed, much lighter on a single-core Zero).
2. Click the gear icon (or "Edit Settings") before writing:
   - set hostname, e.g. `guardian`
   - enable SSH, set a password or paste your public key
   - configure your Wi-Fi SSID/password + country
3. Write the image, boot the Pi.
4. From your laptop: `ssh pi@guardian.local` (or use its IP from your
   router's client list if `.local` mDNS doesn't resolve).

## 2. Wire up the MicroPython board

The Zero W has **two** micro-USB ports — only the one labelled **USB**
(not **PWR**) can talk to a device. You'll likely need a micro-USB-to-USB-A
OTG adapter/cable between that port and whatever cable the board uses.

Power the Pi from the **PWR** port with a proper 5V/2.5A supply — the Zero W
brownout-resets under a flaky supply, which is exactly when you don't want
to lose monitoring.

Confirm it enumerates (note: the board only shows up while it's actually
connected — it sleeps most of the time, so `/dev/ttyACM0` will flicker in
and out roughly once a minute; watch for a bit rather than checking once):
```
watch -n 1 'ls /dev/ttyACM* 2>&1'
sudo usermod -aG dialout pi   # then log out/in (or reboot) for it to take effect
```

### Optional: hardware reset line

The soft reset (Ctrl-D over serial) handles ordinary MicroPython exceptions
fine, since the traceback in your log always leaves the board sitting at a
live `>>>` REPL. Wire a hardware reset only if you want a fallback for the
rare case where the board hangs *without* reaching the REPL:

- Pi GPIO17 (physical pin 11) → 1kΩ resistor → board's **RESET** pin
- Common ground (already shared via USB, but double-check)

Then in the Settings page (or `config.py`) set `ENABLE_GPIO_RESET = True`
and `sudo apt install -y python3-rpi.gpio`. The code drives the pin as an
open-drain style pull (`OUT LOW` → `IN`/Hi-Z) so it never fights the
board's own pull-up. This line is only ever used for the automatic
exception-recovery path, never for the manual Stop/Resume buttons.

## 3. Install

```
sudo apt update
sudo apt install -y python3-serial python3-flask python3-waitress git
mkdir -p /home/pi/serial-guardian
# copy config.py, monitor.py, webapp.py, fields.py, settings.py, applog.py,
# sdnotify.py, watchdog.py, run.py, systemd/ here
#   e.g. scp -r ./pi-serial-guardian/* pi@guardian.local:~/serial-guardian/
```

`python3-waitress` isn't strictly required — `run.py` falls back to Flask's
own development server if it's missing — but it's a real production WSGI
server built for exactly this (long-running, unattended), where Flask's is
explicitly documented as not meant for that. See "If it stops responding"
below for why this is worth the one extra apt package.

Quick manual test:
```
cd /home/pi/serial-guardian
python3 run.py
```
Browse to `http://guardian.local:8080/` (or the Pi's IP) — you should see
the live tail scrolling and a green "connected" dot.

## 4. Run it as a service

```
sudo cp systemd/serial-guardian.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now serial-guardian
sudo systemctl status serial-guardian
```
It'll now survive reboots and restart itself if it ever crashes.

To pick up a code update later: `scp` the changed file(s) over, then
`sudo systemctl restart serial-guardian`.

---

## If it stops responding after running a while

If logging just stops, the Live page stops updating, the toolbar stops
responding, and even restarting the service doesn't bring it back — only
a full `sudo reboot` does — you've hit a real, known issue, not something
wrong with how you set this up. Two separate things can cause this, and
there are defenses in place for both, plus ways to actually tell them
apart next time.

**What's in place:**

- The monitor loop catches *every* exception (not just a normal
  disconnect), logs it with a full traceback to `data/guardian.log`, and
  keeps going instead of the background thread silently dying with zero
  trace.
- `run.py` prefers `waitress` (a real production WSGI server) automatically
  if it's installed, since Flask's built-in development server is
  explicitly documented as not meant to be left running for extended
  periods — under sustained polling (your browser hits `/api/tail` every
  1.5s) over hours/days on a resource-constrained Pi Zero W, that matters.
- The systemd unit uses `Type=notify` with `WatchdogSec=60`, and a
  background thread (`watchdog.py`) pings systemd every ~15s *only* when
  both the monitor loop's own heartbeat is recent **and** a real local
  HTTP request to the Live page gets a response — so a hung Flask server
  is caught independently of the monitor thread, and vice versa. If either
  stops responding, systemd kills and restarts the process automatically.
- `_finish_session()` checks free disk space before writing a session's
  raw text (`MIN_FREE_DISK_MB` in Settings, default 100MB) and skips the
  write with a logged warning instead of throwing if it's critically low.

**What this can't fix on its own:** there's a well-documented issue where
the Pi Zero/Pi 1's USB controller (`dwc_otg`) can lock up after repeated
USB connect/disconnect cycles — which is exactly what this project does to
your board every single wake/sleep cycle, dozens of times an hour. When it
happens, `/dev/ttyACM0` stops appearing at all, and reports online say
only a reboot recovers it (SSH and the rest of the system keep working
fine in that case). If the fixes above don't fully solve it for you, this
is the leading remaining suspect, and no amount of Python-level retry
logic can fix a wedged kernel USB driver — but there are two things worth
trying before a full reboot:

```
# find the USB bus:port for the board (look for "MicroPython" or
# "idVendor=239a" -- Adafruit's vendor ID -- in the output)
lsusb -v 2>/dev/null | grep -B5 -i micropython

# then force the kernel to re-enumerate it without a full reboot
# (replace 1-1 with whatever you found, e.g. from `dmesg | tail`)
echo '1-1' | sudo tee /sys/bus/usb/drivers/usb/unbind
sleep 1
echo '1-1' | sudo tee /sys/bus/usb/drivers/usb/bind
```

Some people also report better long-term stability switching from the
older `dwc_otg` driver to the newer `dwc2` one (`dtoverlay=dwc2` in
`/boot/firmware/config.txt`, then reboot) — worth a search for your
specific Pi model/OS version before changing it, since kernel parameter
advice shifts between Raspberry Pi OS releases.

**Next time it happens, before rebooting** — this preserves the evidence
we'd need to actually pin down which of these it is:
```
ssh pi@guardian.local                              # does SSH even work?
sudo systemctl status serial-guardian               # what state is it in?
ps aux | grep run.py                                # check the STAT column --
                                                      #   a 'D' means stuck in an
                                                      #   uninterruptible kernel wait
                                                      #   (confirms the USB-wedge theory)
dmesg -T | tail -50                                  # look for usb/dwc_otg errors
ls /dev/ttyACM*                                      # does the device node exist?
curl http://127.0.0.1:8080/api/tail                  # run ON the Pi -- if this hangs
                                                      #   too, it's not just WiFi/network
sudo systemctl restart serial-guardian               # does a REAL restart (via SSH,
                                                      #   not the dead UI button) work?
tail -50 /home/pi/serial-guardian/data/guardian.log  # anything logged right before it broke?
journalctl -u serial-guardian -n 100                 # systemd's own view of the same
```

---

## What "session" and the statuses mean

Each session is one open→close cycle of `/dev/ttyACM0`, i.e. one
wake/sleep cycle of the board (same boundary your original script printed
`~~~Connected.~~~` / `~~[DISCONNECTED]~~` around).

- **NORMAL** — the session's text contains
  `"ep.__exit__ completed"` (the line `gotosleep()` prints right before the
  board powers its USB down cleanly).
- **ANOMALY** — the session disconnected without that line, and without any
  exception marker. In your original sample log this also caught the
  button-press GPS-sync session (66s, ends in `committed and resetting`
  instead of `gotosleep()`) — a legitimate different code path, not a bug,
  but flagged for you to eyeball rather than silently assumed.
- **EXCEPTION** — a traceback / `MemoryError` / exception-handler line
  showed up unprompted. This is the one that triggers an automatic reset.
- **STOPPED** — you hit "Stop" in the web UI. The board's own `Traceback` /
  `KeyboardInterrupt` from our Ctrl-C is expected here and does **not**
  count as an EXCEPTION or trigger auto-recovery.

The big count cards on the Live page reflect **everything in `index.jsonl`**,
restored from disk at startup — not just what's happened since the process
last started. A restart doesn't zero out counts that reflect hundreds of
already-recorded sessions; only Erase (see below) does that, deliberately.

## How the automatic reset works (EXCEPTION only)

1. The moment an exception marker is seen, Guardian waits
   `SOFT_RESET_DELAY` (1.5s, to let the traceback finish printing) then
   writes `Ctrl-C, Ctrl-C, Ctrl-D` to the serial port — the same thing
   you'd type by hand at the `>>>` prompt to soft-reboot MicroPython.
2. If a fresh `"Main try: starting"` boot banner shows up within
   `BOOT_TIMEOUT` (12s), Guardian treats that banner as the start of a
   *new* session — so the exception session file ends right where the
   traceback did, and you get a clean before/after pair (`sid` = the crash,
   `sid+1` = the recovered boot) instead of one giant merged file.
3. If no boot banner shows up in time (board well and truly stuck) and
   you've wired the optional GPIO line, it pulses RESET.

## The control panel (Live page)

Status is a single line — a colored lamp (bigger now, more like a panel
indicator than a UI dot) plus a short label and the last session number.
It never grows a second line: anything extra (why it's armed, what a
pending action is waiting on) lives in that line's hover tooltip instead,
so the console strip's height never jumps around between states.

The toolbar is fused directly onto the terminal view — one bordered unit,
control strip on top, the scrolling log immediately below it with no gap.
Buttons are icons, grouped under one-word labels (Device / Monitoring /
Service); hover any of them for the full explanation.

The icon and state choices lean on old transport-deck conventions (Nagra
reel-to-reel, the kind of gear in the background of *Blow Out*) — a filled
square for Stop, a triangle for Resume, two bars for Pause. States show up
as lamp behavior on the keys themselves:

- **Stop** (device) *flashes amber* while armed but not yet in effect —
  stops the moment it actually halts. Click it again while flashing to
  **cancel** the armed stop instead of re-arming it.
- **Reset** (device) glows green **only while a serial session is
  currently open** — a live indicator of whether there's a connection for
  the soft-reset keystrokes to land on.
- **Resume** (device) glows cyan, **Resume** (monitoring) glows amber,
  matching the connection-status lamp's own colors for those states.
- **Pause** (monitoring) also flashes amber while a pause is *queued*
  behind a pending Stop — see below. Click it again while flashing to
  **cancel** just the queued pause, leaving the stop itself untouched.
- Stop and Pause share one blink clock, so when both happen to be
  flashing at once they always do it in lockstep rather than drifting out
  of phase with each other.

**Device** — **Stop** halts the board at the REPL on its next update
(Ctrl-C) so it stays awake instead of sleeping; **Resume** brings it back
(Ctrl-D). Sent the instant a connection opens, with no settle delay —
some of this board's wake cycles complete in well under a second, and an
earlier version's delay could outlast the whole session, leaving an armed
stop stranded for many cycles before a slow-enough wake finally let it
land. **Reset** reboots the board right now regardless of whether it's
halted, running normally, or asleep — if connected, the same soft-reset
keystrokes used for automatic exception recovery (and the same escalation
to the hardware line if it doesn't come back); if asleep and not connected
at all, only the hardware line (if wired) can do anything.

**Monitoring** — *this is the one you want for mpremote.* **Pause** tells
Guardian to stop touching the serial port entirely, so `mpremote` (or a
plain serial terminal) can grab it with nothing fighting over the port.
Nothing gets logged while paused. **Resume** reopens the port.

If you press **Stop** and then, before it's actually taken effect, press
**Pause** — e.g. Stop is merely *armed*, waiting for the board to next wake
and connect — Guardian defers the pause rather than closing the port out
from under the pending stop. An armed-but-undelivered stop needs a live
connection to ever get sent; closing the port first would strand it
indefinitely. The Pause key flashes amber while queued this way, and the
pause applies automatically the instant the stop actually completes. Both
Stop and Pause can be cancelled independently by clicking them again while
flashing; cancelling the stop also drops any pause queued behind it (there's
nothing left for it to wait on), but cancelling the pause on its own leaves
the stop armed and running.

The **trash icon** next to Pause/Resume permanently deletes every session
log and the index, restarting numbering — and resetting the count cards —
back to zero. It lives here rather than in its own group since it's really
a "reset the record, not the device" action. Confirm dialog, since it
can't be undone.

**Service** — just **Restart**, via a narrowly scoped sudoers rule rather
than broad sudo access:

```
sudo visudo -cf systemd/serial-guardian.sudoers   # validate syntax first
sudo cp systemd/serial-guardian.sudoers /etc/sudoers.d/serial-guardian
sudo chmod 440 /etc/sudoers.d/serial-guardian
```

Grants the `pi` user passwordless rights to `systemctl restart
serial-guardian`, nothing broader. Without this file the button still
appears but silently does nothing. Confirm dialog on this one too.

The full **Stop the whole service** button is gone from the UI — pausing
monitoring covers the mpremote use case without taking the dashboard down
with it. The endpoint itself is still there for scripting:
```
curl -X POST http://guardian.local:8080/api/service/stop
```
(same sudoers rule, same "no restart on a deliberate stop" caveat — bring
it back with `sudo systemctl start serial-guardian` over SSH.)

Confirm dialogs only show up on **Erase** and **Restart** — the two
actions that are either irreversible or disruptive enough to warrant one.
Everything else fires immediately on click.

**Worth knowing:** none of this — including Erase and Restart — has any
authentication. Anyone who can reach the Pi on your network can hit these.
Fine on a trusted home LAN; if that's not your situation, say so and I can
add a single shared-password gate in front of the whole app.

## Settings page

**Session field extraction** — add/edit/remove columns pulled out of each
session's raw log with a regex, shown on the Sessions page. Out of the box:

| column | pulled from | pattern |
|---|---|---|
| `REFRESH` | `REFRESH: 8` (the SESSION dump block) | `REFRESH:\s*(\d+)` |
| `reset_cause` | `State: reset cause = 16, wake up pins [39]` | `reset cause = (\d+)` |
| `wake_pins` | same line | `wake up pins \[([^\]]*)\]` |

Each pattern needs **exactly one capture group**. Config lives in
`data/field_config.json`, not `config.py`, so you can change it from the
browser mid-debug with no restart. It's evaluated at *view* time against
each session's raw log, not baked in when the session finishes — so a new
column applies retroactively to sessions already on disk.

**Configuration** is nearly every tunable in `config.py` — serial timing,
detection markers, reset delays, GPIO settings, log retention, the
live-tail buffer, `MIN_FREE_DISK_MB` — as a form. Saves apply immediately,
no restart: writes to `data/settings.json` and mutates the running
process's config in place, which everything already reads fresh at the
point of use. A handful of things aren't exposed — `DATA_DIR`,
`WEB_HOST`/`WEB_PORT`, `SERVICE_NAME`, `SOFT_RESET_BYTES`, `STOP_BYTES` —
each shown read-only with the specific reason (mostly: process-bootstrap
values that can't retroactively rebind a socket or relocate an open file).

## Tuning

Nearly everything is editable from the **Settings** page and takes effect
immediately. `config.py` still holds the defaults, and is the only place
for the handful of things Settings deliberately doesn't expose.

## A note on scale

`recent_sessions()` and `find_record()` shell out to `tail`/`grep` on
`index.jsonl` rather than loading the whole file into memory, so browsing
sessions stays comfortable on a Zero W's 512MB even after months of
logging. The cumulative stats on the Live page (`self.stats`) *are* built
by reading the whole file once, at startup only — a one-time cost, not a
per-request one, and still fast even at tens of thousands of lines. If you
ever want proper querying (e.g. "all EXCEPTIONs in the last 30 days"),
swapping `index.jsonl` for a small SQLite table is the natural next step —
not done here to keep the dependency list to `pyserial` + `Flask`
(+ `waitress`).
