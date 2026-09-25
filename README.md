<img width="1845" height="1137" alt="image" src="https://github.com/user-attachments/assets/afebb60f-500d-4739-8cae-a15a125ce637" />

# Serial Guardian

A web dashboard that watches a sleeping embedded device's USB serial output
from a low-power always-on SBC (built for a Raspberry Pi Zero W), so you
don't have to leave a laptop plugged in.

> **Vibecoded.** Every line of code and every doc in this repo — including
> this README — was written with Claude (Anthropic), GitHub Copilot, and
> Gemini, in conversation with a human who described the problem, tested it
> on real hardware, and iterated. No hand-written code. Read it before you
> trust it.

## The problem this solves

Some devices don't stay connected. A battery-powered MicroPython/
CircuitPython board that wakes up, does something for a second or two, and
goes back to deep sleep — dozens or hundreds of times a day — is a pain to
monitor: a laptop running `screen` or a Python script has to stay open
forever, and it misses everything that happens while it's not watching.

Serial Guardian runs headless on a Pi Zero W (or anything similar) sitting
next to the device, catches every wake cycle automatically, and gives you a
browser tab instead of a terminal you have to babysit.

## What it does

- Splits the serial stream into **sessions** (one connect→disconnect cycle)
  and tags each one `NORMAL`, `ANOMALY` (didn't shut down cleanly),
  `EXCEPTION` (crashed), or `STOPPED` (you halted it on purpose)
- Auto-recovers from crashes (soft reset, with an optional hardware GPIO
  fallback) and logs everything forever except routine `NORMAL` sessions,
  which get pruned so the SD card doesn't fill up
- Live web view + full session history, filterable by status
- One-click Stop / Resume / Reset on the device itself, and a Pause that
  frees the serial port for `mpremote` or a terminal without touching the
  service
- Pull custom columns out of your logs with your own regexes, and tune
  nearly everything live from a Settings page — no restart, no redeploy
- Survives its own crashes and a hung web server on its own (broad
  exception handling, a disk-space guard, a systemd watchdog)

## Quick start

```bash
sudo apt install -y python3-serial python3-flask python3-waitress
scp -r ./pi-serial-guardian/* pi@yourpi.local:~/serial-guardian/
ssh pi@yourpi.local
cd ~/serial-guardian
sudo cp systemd/serial-guardian.service /etc/systemd/system/
sudo systemctl enable --now serial-guardian
```

Open `http://yourpi.local:8080`.

Full install steps (headless Pi setup, wiring, systemd, optional GPIO
reset, sudoers for the Restart button) and the complete feature reference
are in **[docs/GUIDE.md](docs/GUIDE.md)**.

## Compile and deploy MicroPython files

`compile_deploy.sh` provides an interactive local workflow for compiling
`.py` files to `.mpy` files and deploying selected files to the Pi with
`scp` and `mpremote`.

Install its dependencies on the development computer:

```bash
sudo apt install -y whiptail openssh-client
chmod +x compile_deploy.sh
```

Run it from anywhere:

```bash
./compile_deploy.sh
```

The first run asks for the source folder, output folder, `mpy-cross`
executable, SSH target (for example `pi@guardian.local`), and the remote
`mpremote` command. These values are saved in
`~/.config/serial-guardian/compile_deploy.conf`, so later runs go directly
to the menu. If the script is moved to a different directory, it asks for
the paths and target again. Use **Settings** in the menu to change them
without moving the script. The deploy workflow supports selecting multiple
compiled files at once.

## Built for

- Single-board computers with limited RAM/CPU — a Pi Zero W is the
  reference target, not a Pi 4/5
- Devices that spend most of their time disconnected — deep-sleeping
  microcontrollers, not something with a stable always-on serial link
- MicroPython/CircuitPython boards, though anything talking plain text
  over USB serial works

## Not included

No authentication on the web UI — fine on a trusted home network, not
meant to be exposed to the internet.
