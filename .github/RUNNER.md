# Testing the OAK-1 through CI on the Pi 5

GitHub's normal (cloud) runners have no OAK-1 attached, so cloud CI runs only
the hardware-free tests. To have CI set up the Pi and talk to the real camera,
register the Pi 5 itself as a **self-hosted runner**. Then the `camera-hardware`
job in `.github/workflows/ci.yml` runs on the Pi with the camera plugged in.

## One-time: register the Pi as a runner

On the Pi 5:

1. On GitHub: repo → **Settings → Actions → Runners → New self-hosted runner**,
   choose **Linux / ARM64**. GitHub shows a token'd `./config.sh` command.
2. Run the download + `./config.sh` it gives you. When it asks for **labels**,
   add `oak1` (the workflow targets `[self-hosted, linux, ARM64, oak1]`).
3. Install it as a service so it survives reboots:

   ```bash
   sudo ./svc.sh install
   sudo ./svc.sh start
   ```

4. Let the runner use sudo without a password, so `setup.sh` can add the udev
   rule unattended:

   ```bash
   echo "$(whoami) ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/gh-runner
   ```

5. Plug the OAK-1 into a **blue USB3** port, and power the Pi from a supply that
   can actually feed the camera (official 27 W, or a powered hub). Undervoltage
   is the #1 cause of flaky camera CI.

## Turn the hardware job on

In `.github/workflows/ci.yml`, change the `camera-hardware` job's `if: false`
to `if: true`, commit, and push. From then on every push to `main` or a
`test/*` branch runs, on the Pi:

- `setup.sh`  — venv, DepthAI, udev rule, power check
- `check.py`  — the full camera diagnostic against the real OAK-1
- `selftest.py` — the 22 logic checks

If `check.py` reports undervoltage or a USB link below SuperSpeed, the job
fails — which is the point: it stops a browned-out board from being called a
passing build.

## What runs where

| Job | Runner | Needs camera? |
|---|---|---|
| `camera-software` | GitHub cloud | no — 22-check self-test |
| `firmware-software` | GitHub cloud | no — 135 firmware checks |
| `camera-hardware` | your Pi 5 | yes — real OAK-1 |
