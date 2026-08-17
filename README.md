# Serial Guardian

Moves your `check.py` monitoring loop off the laptop and onto a Raspberry Pi
Zero W (v1), reachable over SSH and a small web dashboard. It also:

- splits the stream into **sessions** (one connect → disconnect cycle of the
  board's USB serial),
- tags each session `NORMAL` / `ANOMALY` (never reached `gotosleep()`) /
  `EXCEPTION` (traceback / MemoryError / etc.),
- on `EXCEPTION`, tries to recover the board automatically,
- keeps every `EXCEPTION` and `ANOMALY` log forever, and prunes old `NORMAL`
  logs so the SD card doesn't fill up.

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

Confirm it enumerates:
```
ls /dev/ttyACM*
dmesg | tail
sudo usermod -aG dialout pi   # then log out/in (or reboot)
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
board's own pull-up.

## 3. Install

```
sudo apt update
sudo apt install -y python3-serial python3-flask git
mkdir -p /home/pi/serial-guardian
# copy config.py, monitor.py, webapp.py, run.py, systemd/ here
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
  showed up. This is the one that triggers a reset (see below).

## How the automatic reset works

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

In your pasted log, crashes were eventually cleared by what looks like a
watchdog — but only after 173s and 472s of sitting idle at the REPL. With
Guardian running, recovery happens in a couple of seconds instead.

## Tuning

Everything above lives in `config.py`:
- add more `EXCEPTION_MARKERS` if your firmware grows new failure modes
- `NORMAL_LOG_RETENTION` — how many normal-session logs to keep on disk
  (index entries for pruned sessions stick around either way, just without
  the raw text)
- `SOFT_RESET_DELAY` / `BOOT_TIMEOUT` if your board needs more time

## A note on scale

`recent_sessions()` and `find_record()` shell out to `tail`/`grep` on
`index.jsonl` rather than loading the whole file into memory, so this
stays comfortable on a Zero W's 512MB even after months of logging. If you
ever want proper querying (e.g. "all EXCEPTIONs in the last 30 days"),
swapping `index.jsonl` for a small SQLite table is the natural next step —
not done here to keep the dependency list to `pyserial` + `Flask`.
