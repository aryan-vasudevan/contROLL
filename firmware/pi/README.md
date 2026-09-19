# Using a Raspberry Pi 5 as the bridge

Yes, this works, and the badge firmware does not change. The Pi replaces the
small ESP32 bridge and does the same job: carry MAVLink between the flight
controller's serial port and Wi-Fi.

Run [setup.sh](setup.sh) on the Pi and it installs mavlink-router, points it at
your serial port, and turns the onboard Wi-Fi into an access point the badge can
join. The badge still sends MANUAL_CONTROL over UDP to port 14550 and still has
no idea what is on the other end.

```
sudo AP_PASS='choose-something' ./setup.sh
```

## What it costs you

A Pi 5 is a much bigger thing to bolt to an F450 than a six dollar ESP32. It
works, but be clear-eyed about the trade.

| | Pi 5 | ESP32 bridge |
|---|---|---|
| Weight, installed | roughly 100 g | under 8 g |
| Current at 5 V | around 1.2 A for this workload | around 0.2 A |
| Ready after power-on | around 25 s | under 2 s |
| Survives a power yank | not without care | yes |

An F450 carries 100 g without complaint. You will lose some flight time and
some agility, and you gain a full Linux machine on the aircraft. If all you
want is the badge link, the ESP32 is the better engineering answer. If you want
a camera, vision tracking, logging, or anything else on board later, the Pi
earns its weight.

## Power, which is the part that bites

**Do not power the Pi from the flight controller's TELEM 5 V rail.** That rail
is sized for a radio drawing a fraction of what a Pi 5 does, and browning out
the Pi mid-flight takes the link with it.

Run a separate 5 V BEC off the main battery, rated 5 A. The workload here is
nowhere near that, but the Pi 5 draws hard in bursts and a marginal supply
shows up as random reboots that look like software bugs for a long time.

Yanking power from a running Pi can corrupt the SD card, and a drone loses
power abruptly by definition. Turn on the read-only overlay filesystem before
you fly: `sudo raspi-config`, Performance Options, Overlay File System. The
filesystem then lives in RAM and the card is never written, so a hard power cut
costs nothing. Turn it off when you want to change the configuration.

## Wiring

Four wires from the Pi's GPIO header to a TELEM port. Both sides are 3.3 V
logic, so no level shifter is needed.

| Pi 5 | Flight controller |
|---|---|
| GPIO 14, pin 8, TXD | TELEM RX |
| GPIO 15, pin 10, RXD | TELEM TX |
| any ground pin | TELEM GND |
| nothing | TELEM 5 V, leave disconnected |

Transmit goes to receive on each side. Getting this backwards is the most
common reason a new link is silent.

If you would rather skip the GPIO header entirely, a USB cable from the Pi to
the flight controller's USB port also carries MAVLink. Pass
`FC_DEVICE=/dev/ttyACM0` to the setup script. It is easier to get right, but it
occupies the port you use for configuration and can introduce a ground loop.

## Two Pi 5 specific traps

**The serial port moved.** On the Pi 5, `/dev/serial0` points at the separate
three-pin debug connector, not at GPIO 14 and 15. Any guide written for a Pi 4
will tell you to use `/dev/serial0` and it will not work. Use `/dev/ttyAMA0`.
The setup script defaults to it and enables `dtparam=uart0=on` for you.

**The access point must be 2.4 GHz.** The badge's ESP32-C3 has no 5 GHz radio.
A Pi 5 hotspot left on default settings may land on 5 GHz, where the badge
simply never sees it. The setup script pins the band to 2.4 GHz.

## Flight controller parameters

For a Pi on TELEM2:

```
SERIAL2_PROTOCOL = 2      # MAVLink2
SERIAL2_BAUD     = 921    # 921600, match FC_BAUD in setup.sh
BRD_SER2_RTSCTS  = 0      # the four-wire hookup has no flow control
SYSID_MYGCS      = 255    # or the badge is ignored entirely
```

`SYSID_MYGCS` is not optional. ArduPilot discards manual control from any
system id other than this one, and the badge sends 255. A setup that is
perfect except for this parameter looks exactly like a setup that is broken:
telemetry flows, the badge shows a healthy link, and the aircraft ignores
every button.

## Bringing it up

Work through these in order and stop at the first one that fails. Each step
proves one link in the chain, and skipping ahead means debugging two unknowns
at once.

1. **Prove the serial link.** With the Pi wired and the flight controller
   powered, run `python3 listen.py --serial /dev/ttyAMA0 --baud 921600`. You
   want a heartbeat, a mode name, and a satellite count. Nothing here means the
   problem is wiring or baud rate, and no amount of badge debugging will find it.
2. **Prove the router.** `sudo systemctl start mavlink-router`, then
   `python3 listen.py`, which reads the router's TCP server rather than the
   serial port. Same picture as step one.
3. **Prove the Wi-Fi.** Join a laptop to the access point and connect Mission
   Planner or QGroundControl over UDP on port 14550. If a laptop cannot see the
   aircraft, the badge never will, and the laptop tells you why in plain words.
4. **Prove the badge, with the propellers off.** Flash it, arm, and work
   through every button while watching the motor outputs in Mission Planner.
   Confirm the shake gesture does what you expect on the bench.
5. **Then fly**, outdoors, with a real RC transmitter in your other hand.

## Files

| File | What it is |
|---|---|
| `setup.sh` | installs and configures everything, asks before it changes anything |
| `main.conf.template` | mavlink-router config, serial in and UDP out |
| `listen.py` | decodes the link so you can see what the aircraft is saying |

`listen.py` is self-contained and has no dependencies beyond the standard
library, except for `--serial` which needs `python3-serial`. Copy it anywhere.
