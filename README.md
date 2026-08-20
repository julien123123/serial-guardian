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
- keeps every `EXCEPTION` / `ANOMALY` / `STOPPED` log forever, and prunes
  old `NORMAL` logs so the SD card doesn't fill up,
- lets you pull extra columns (like `REFRESH` or `reset cause`) out of
  each session's raw log with your own regexes, and tune almost every
  other setting too, all from a Settings page, live, no restart needed.

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

Then in `config.py` set `ENABLE_GPIO_RESET = True` and
`sudo apt install -y python3-rpi.gpio`. The code drives the pin as an
open-drain style pull (`OUT LOW` → `IN`/Hi-Z) so it never fights the
board's own pull-up. This line is only ever used for the automatic
exception-recovery path, never for the manual Stop/Resume buttons.

## 3. Install

```
sudo apt update
sudo apt install -y python3-serial python3-flask git
mkdir -p /home/pi/serial-guardian
# copy config.py, monitor.py, webapp.py, fields.py, settings.py, run.py, systemd/ here
#   e.g. scp -r ./pi-serial-guardian/* pi@guardian.local:~/serial-guardian/
```

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

To pick up a code update later: `scp` the changed file over, then
`sudo systemctl restart serial-guardian`.

---

## What "session" and the statuses mean

Each session is one open→close cycle of `/dev/ttyACM0`, i.e. one
wake/sleep cycle of the board (same boundary your original script printed
`~~~Connected.~~~` / `~~[DISCONNECTED]~~` around).

- **NORMAL** — the session's text contains
  `"ep.__exit__ completed"` (the line `gotosleep()` prints right before the
  board powers its USB down cleanly).
- **ANOMALY** — the session disconnected without that line, and without any
  exception marker. In your sample log this also caught the button-press
  GPS-sync session (66s, ends in `committed and resetting` instead of
  `gotosleep()`) — a legitimate different code path, not a bug, but you
  did ask to see anything that skips the normal shutdown, so it's flagged
  for you to eyeball rather than silently assumed.
- **EXCEPTION** — a traceback / `MemoryError` / exception-handler line
  showed up unprompted. This is the one that triggers an automatic reset.
- **STOPPED** — you hit "Stop device on next update" in the web UI. The
  board's own `Traceback` / `KeyboardInterrupt` from our Ctrl-C is expected
  here and does **not** count as an EXCEPTION or trigger auto-recovery.

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

In your original pasted log, crashes were eventually cleared by what looks
like a watchdog — but only after 173s and 472s of sitting idle at the REPL.
With Guardian running, recovery happens in a couple of seconds instead.

## Stop / Resume

The **"Stop device on next update"** button on the Live page arms a halt:
- if the board is connected right now, it sends Ctrl-C immediately;
- if it's asleep, the request is remembered and applied the next time it
  wakes and connects (no need to leave the browser tab open and wait).

Ctrl-C interrupts whatever's running in `main.py` and drops MicroPython
into the REPL, same as any other unhandled exception — except here it's
expected, so it's tagged `STOPPED` rather than `EXCEPTION`, and the
auto-reset logic leaves it alone. Because the board never reaches
`gotosleep()`, it stays awake and connected (and drawing power) until you
either hit **Resume** (sends Ctrl-D, same soft-reboot as the auto-recovery
path) or reset it some other way.

One caveat, since I don't have your firmware source: this assumes
`main.py` doesn't catch `KeyboardInterrupt` internally and just carry on.
If your loop is wrapped in a broad `except:` that swallows it, Ctrl-C won't
actually stop anything — worth testing once on a session you don't mind
losing, and if that's the case, let me know how `main.py` structures its
top-level loop and I can adjust the approach.

## Why lines used to go missing on the Pi but not your laptop

pyserial's `Serial.readline()` reads **one byte per syscall** (the default
`io.RawIOBase` behaviour) — fine on a fast, multi-core laptop, but a
single-core Pi Zero W can't always keep up with bursty output: a traceback
printing fast, or the ~50 GPS NMEA lines your log shows arriving in under a
second. Python falls behind, the kernel's USB-serial buffer fills up, and
bytes get silently dropped while the CPU is busy elsewhere (which is more
likely to happen during exactly the bursty, "anormal" sessions you noticed
this in).

The read loop now blocks for the first byte (so it still sleeps properly
between updates) and then drains **everything else currently buffered** in
a single `read()` call, turning "one syscall per byte" into "about one
syscall per burst." I tested this against a simulated 600-line burst
delivered all at once and confirmed every line makes it into the log —
but if you still see gaps on real hardware at very high line rates, the
next lever to pull is decoupling the web server from the serial reader
(two processes instead of one, so a slow browser request can never delay
a read) — not needed unless the above turns out to be insufficient.

## Settings page

Nav bar has **Live / Sessions / Settings** now (the old "Exceptions"
shortcut is gone from the header — use the "Exception" filter pill on the
Sessions page, or click the EXCEPTION count card on the Live page, either
of which link to the same `/sessions?status=EXCEPTION` view).

**Session field extraction** lives at the top of the Settings page —
add/edit/remove columns pulled out of each session's raw log with a
regex, shown on the Sessions page. Out of the box:

| column | pulled from | pattern |
|---|---|---|
| `REFRESH` | `REFRESH: 8` (the SESSION dump block) | `REFRESH:\s*(\d+)` |
| `reset_cause` | `State: reset cause = 16, wake up pins [39]` | `reset cause = (\d+)` |
| `wake_pins` | same line | `wake up pins \[([^\]]*)\]` |

Each pattern needs **exactly one capture group** — that's the value shown
in the column; the save button rejects anything else with an explanation
rather than silently misbehaving. Config lives in
`data/field_config.json`, not `config.py`, specifically so you can change
it from the browser while mid-debug with no restart. It's also intentionally
*not* baked into a session's record when the session finishes — values are
pulled from the raw log at the moment you view the Sessions page, so adding
a new column applies retroactively to sessions already sitting on disk, not
just ones from that point forward. (Sessions whose raw log has since been
pruned — see retention below — just show a blank for any column.)

**Configuration** is the rest of the page: nearly every tunable in
`config.py` — serial timing, the markers that decide NORMAL vs EXCEPTION,
soft-reset/boot-timeout delays, GPIO settings, log retention, the live-tail
buffer size — as a form, grouped the same way `config.py` is commented.
Save applies changes **immediately, no restart needed**: it writes to
`data/settings.json` and mutates the running process's config in place,
which every part of the codebase already reads fresh at the point of use
rather than caching at startup. A handful of things aren't exposed here —
`DATA_DIR`, `WEB_HOST`/`WEB_PORT`, `SERVICE_NAME`, and the two raw
control-byte sequences (`SOFT_RESET_BYTES`, `STOP_BYTES`) — each shown
read-only at the bottom of the page with the specific reason (mostly:
they're process-bootstrap values that can't retroactively rebind a
listening socket or relocate a settings file that's already open). Edit
`config.py` directly and restart the service for those.

## Actions toolbar (Live page)

A short, right-aligned toolbar, grouped by what each part affects. Buttons
are single words; hover any of them for the full explanation.

**Device** — **Stop** halts the board at the REPL on its next update
(Ctrl-C) so it stays awake instead of sleeping; **Resume** brings it back
(Ctrl-D). **Reset** is new and unconditional: reboots the board right now
regardless of whether it's halted, running normally, or asleep. If it's
connected, that's the same soft-reset keystrokes used for automatic
exception recovery (and the same escalation to the hardware line if it
doesn't come back); if it's asleep and not connected at all, soft reset
over serial isn't possible, so only the hardware line (if wired) can do
anything.

**Monitoring** — *this is the one you want for mpremote.* **Pause** tells
Guardian to stop touching the serial port entirely: the next time its read
loop notices (within a fraction of a second), it closes `/dev/ttyACM0` and
stops trying to reopen it, so `mpremote` (or a plain serial terminal) can
grab it with nothing fighting over the port. Nothing gets logged while
paused. **Resume** reopens the port and picks back up where it left off.
Both stay within the same process — the web UI keeps working the whole
time, unlike a full service stop.

**Service** — the heavier hammer: `systemctl restart|stop
serial-guardian`, via a narrowly scoped sudoers rule rather than broad
sudo access:

```
sudo visudo -cf systemd/serial-guardian.sudoers   # validate syntax first
sudo cp systemd/serial-guardian.sudoers /etc/sudoers.d/serial-guardian
sudo chmod 440 /etc/sudoers.d/serial-guardian
```

This grants the `pi` user passwordless rights to exactly those two
commands, nothing broader. Without this file the buttons still appear but
silently do nothing (`sudo -n` fails fast rather than hanging on a
password prompt). **Restart** briefly interrupts monitoring, then systemd
brings the service back per `Restart=always` — the page reconnects on its
own. **Stop** takes the whole tool down (it's the same process serving
this page), and being a deliberate `systemctl stop`, systemd will *not*
auto-restart it. There's no "Start" button, deliberately: nothing could
serve it once the process is down. Bring it back with:
```
ssh pi@guardian.local
sudo systemctl start serial-guardian
```
In practice, **pausing monitoring is the right tool for using mpremote**
— instant, reversible from the browser, doesn't take the dashboard down
with it. Reach for full Stop only if Guardian itself needs bouncing.

**Data** — **Erase** permanently deletes every session log and the index,
and restarts numbering at #1. There's a confirm dialog because it can't be
undone. (One honest caveat: if a session happens to finish at the exact
instant you click Erase, its file can land back on disk right after —
a rare, harmless race, not worth adding lock contention on the serial
read path to fully close.)

**Worth knowing:** none of this — including Stop and Erase — has any
authentication. Anyone who can reach the Pi on your network can hit these.
Fine on a trusted home LAN; if that's not your situation, say so and I can
add a single shared-password gate in front of the whole app.

## Tuning

Nearly everything is now editable from the **Settings** page (see above)
and takes effect immediately. `config.py` still holds the defaults, and is
the only place for the handful of things the Settings page deliberately
doesn't expose (`DATA_DIR`, `WEB_HOST`/`WEB_PORT`, `SERVICE_NAME`,
`SOFT_RESET_BYTES`, `STOP_BYTES` — see the "Not editable here" section on
the page itself for why each one).

## A note on scale

`recent_sessions()` and `find_record()` shell out to `tail`/`grep` on
`index.jsonl` rather than loading the whole file into memory, so this
stays comfortable on a Zero W's 512MB even after months of logging. If you
ever want proper querying (e.g. "all EXCEPTIONs in the last 30 days"),
swapping `index.jsonl` for a small SQLite table is the natural next step —
not done here to keep the dependency list to `pyserial` + `Flask`.
