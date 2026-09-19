# Badge -> F450 drone controller

Firmware that turns the Hack the North hacker badge into a Wi-Fi remote for an
F450 quadcopter. The D-pad and the A/B buttons fly it. Shaking the badge calls
the drone back to a spot you marked earlier.

## What the badge actually has

Everything below was read out of `badge.kicad_pcb`, not assumed. The
ESP32-C3-MINI-1 module pad to GPIO translation is confirmed by five separate
anchors in the netlist (`ESP32_EN` on the EN pad, `ESP32_BOOT` on the IO9 strap,
and three nets literally named `Net-(U9-IO8)`, `Net-(U9-IO18)`, `Net-(U9-IO19)`).

| Function | GPIO | Notes |
|---|---|---|
| Shift register data out | 7 | from `U8.9` (74HC165 QH) |
| Shift register shift/load | 20 | to `U8.1`, active low |
| Shift register clock | 21 | to `U8.2` |
| I2C SDA | 5 | 4k7 pull-up, shared with the NFC reader |
| I2C SCL | 6 | 4k7 pull-up |
| WS2812B data | 3 | through a level shifter, 6 pixels |
| Display CS / DC / SCLK / MOSI / RST | 2 / 0 / 1 / 10 / 4 | not driven by this firmware |
| Boot button | 9 | wired straight to the pin, not the register |

The eight game buttons are not on GPIOs. They hang off a 74HC165 shift
register, each pulled to 3V3 by a 10k and shorted to ground when pressed, so
every one of them reads active low. The register presents D7 first, so reading
eight bits MSB-first gives this order:

| Bit | Net | Switch | Role |
|---|---|---|---|
| 7 | `SW_HPM` | SW6 | A |
| 6 | `BTN_7` | SW5 | B |
| 5 | `BTN_6` | SW7 | SELECT |
| 4 | `BTN_5` | SW8 | down |
| 3 | `BTN_1` | SW11 | slide switch |
| 2 | `BTN_2` | SW2 | up |
| 1 | `BTN_3` | SW3 | right |
| 0 | `BTN_4` | SW4 | left |

The accelerometer is an SC7A20 at I2C address 0x19. Its CS pin is pulled high,
which selects I2C, and its SDO pin is pulled high, which picks 0x19 over 0x18.
Both were read off the board.

### One thing you should verify on real hardware

This board has no silkscreen naming the buttons, so the D-pad and A/B roles come
from where the switches physically sit. SW2, SW4, SW3 and SW8 form a clean cross,
which is unambiguous. A and B are the diagonal pair to its right, and A is taken
to be the outer one.

Hold the BOOT button while powering up to enter diagnostics. Nothing is
transmitted in that mode, so it is safe with a battery in the drone. Press each
button and read the names off the serial console. If A and B are swapped on your
badge, swap `BIT_A` and `BIT_B` at the top of `src/buttons.cpp`.

## What the drone needs

An F450 is a frame, not a flight stack. This assumes the common build: a
Pixhawk-class flight controller running ArduCopter, with something on a TELEM
port bridging MAVLink onto Wi-Fi.

**The flight controller has to be running ArduPilot or PX4.** Betaflight and
INAV do not accept MAVLink control commands, so this firmware cannot fly them.
Check that before anything else.

For the bridge you have three reasonable options.

| Bridge | Weight | Notes |
|---|---|---|
| DroneBridge for ESP32 | under 8 g | runs on a Seeed XIAO ESP32C3, 150 m or more |
| MAVESP8266 | around 10 g | the older standard, documented by ArduPilot |
| Raspberry Pi 5 | around 100 g | heavier, but a whole Linux machine on board |

The two small ones are the better engineering answer if the badge link is all
you want. See [pi/README.md](pi/README.md) for the Pi route, including a setup
script, and for the power and weight trade you are making by taking it. The
badge firmware is identical either way.

Whichever you pick:

1. Put the bridge's SSID, password and address in `include/config.h`. Wi-Fi
   names are case sensitive.
2. Set `SYSID_MYGCS` on the flight controller to `255`. ArduPilot ignores
   `MANUAL_CONTROL` from any other system id, and this is the single most
   common reason a setup like this does nothing at all.
3. Leave the flight mode at `LOITER`. It holds position when you release the
   buttons. `ALT_HOLD` only holds height and will drift with the wind.

Do not set `FLIGHT_MODE` to `STABILIZE`. There is no throttle stick on a badge,
only buttons, and `STABILIZE` would drop the aircraft the moment you let go.

## Flying it

| Input | Action |
|---|---|
| up / down | climb and descend |
| left / right | yaw, or strafe, depending on the SW11 slide switch |
| A | forward |
| B | backward |
| shake the badge | fly to the marked spot |

Hold SELECT and press a direction for commands. Each needs a deliberate hold,
so nothing fires by brushing a button.

| Combo | Action |
|---|---|
| SELECT + A | arm, 1.5 s, refused unless landed with a GPS fix |
| SELECT + B | disarm, 1.5 s, refused while airborne |
| SELECT + up | take off to `TAKEOFF_ALT_M` |
| SELECT + down | land here |
| SELECT + left | return to launch |
| SELECT + right | mark this spot as the beacon, 2 s, drone must be landed |

The sticks go quiet while SELECT is held, so reaching for a combo does not yaw
the aircraft.

The LEDs carry the state: blue while connecting, dim green disarmed, solid red
armed on the ground, green sweep flying, red blink if the link drops, amber
flash during the cancel window, magenta sweep while flying home.

## How "come to me" works, and what it cannot do

The badge has an accelerometer but no GPS. It cannot know where you are. So
"come to me" means "fly to a position you marked earlier", and marking is the
part you have to do on purpose.

Stand where you want the drone to end up, put the drone on the ground next to
you, and hold SELECT + right for two seconds. The badge reads the drone's own
GPS position and saves it to flash, where it survives a reboot. That spot is now
the beacon. If you already know your coordinates you can instead set
`BEACON_FIXED_LAT` and `BEACON_FIXED_LON` in the config.

Shaking then does this:

1. A shake is four threshold crossings of the gravity-removed acceleration
   inside 1.2 seconds. One knock will not do it, and neither will the badge
   swinging on a lanyard.
2. The LEDs flash amber for 1.5 seconds. **Any button press cancels.**
3. The aircraft goes to GUIDED and the badge streams a position target at 5 Hz.
4. It stops `COME_STANDOFF_M` short of the beacon, on the side it is already
   approaching from, so it parks near you rather than flying at you. Do not set
   that below about 3 metres.
5. It holds the altitude it already had, clamped between `COME_MIN_ALT_M` and
   `COME_MAX_ALT_M`. It will not climb or dive on the way over.
6. Any button press during the flight home stops it and returns to `LOITER`.

It refuses outright if the drone is not flying, if there is no beacon, if the
GPS fix is worse than 3D with 6 satellites, or if the link is down. The LEDs
double-flash red and the reason goes to the serial console.

The accuracy ceiling is your GPS, so expect it to arrive within a few metres,
not onto a dinner plate. That is also why the standoff exists.

## Safety

This drives real propellers, so the firmware is deliberately reluctant.

- Nothing is armed and no command is sent at boot.
- Sticks are only transmitted while the aircraft's heartbeat is fresh. Two
  seconds of silence and the badge stops commanding and shows red.
- Arming needs a GPS fix and a two-button hold.
- Held buttons ramp in over 350 ms rather than stepping to full deflection, and
  release about twice as fast as they engage.
- The default deflections are about a third of full stick. Raise them once you
  trust the setup, not before.

None of that replaces a real transmitter bound to the aircraft with a working
failsafe. Keep one in your hands, fly somewhere legal and open, and test every
combo with the propellers off first.

## Building

```
pio run                  # build
pio run -t upload        # flash over USB-C
pio device monitor       # serial console, 115200
```

## Tests

```
./tests/run.sh           # needs only g++ and python3, no badge, no drone
```

135 checks covering the four things that are painful to debug with a drone in
front of you.

- **MAVLink wire format.** Every frame the firmware encodes is decoded by a
  separate reference implementation that derives each message's `CRC_EXTRA`
  from the message definition instead of copying the firmware's table, so a
  wrong constant cannot agree with itself. The receive-side byte offsets are
  checked the same way.
- **Standoff geometry.** The distance calculation is compared against haversine,
  and for a spread of drone positions the target is confirmed to sit the right
  distance from the beacon, on the drone's side of it, and on the straight line
  between the two.
- **Shake detection.** The detector is driven with synthetic motion: a badge at
  rest, walking, jogging with it swinging, single and double knocks, a real
  shake, and a badge turned upside down. Only the shake fires.
- **Button map.** Re-derived from `badge.kicad_pcb` and compared against what
  `src/buttons.cpp` compiles in, including working the D-pad roles back out of
  the switch coordinates.

The geometry and shake tests pull their code verbatim out of the firmware
sources rather than reimplementing it, so they track edits to the real thing.

USB-C goes straight to the C3's native USB peripheral, so the console is USB CDC
and needs no adapter.

If the LEDs stay dark but the badge is clearly running, check slide switch SW1.
It gates the 5 V boost converter that powers the LED chain.

## Layout

| File | What is in it |
|---|---|
| `include/badge_pins.h` | pin map, with the netlist evidence for each pin |
| `include/config.h` | Wi-Fi, stick feel, failsafe limits, everything you edit |
| `src/mavlink_min.cpp` | MAVLink v2 codec, nine messages, no external library |
| `src/buttons.cpp` | 74HC165 driver, debounce, edge and hold detection |
| `src/imu.cpp` | SC7A20 driver and the shake detector |
| `src/leds.cpp` | status animations |
| `src/dronelink.cpp` | Wi-Fi, UDP, and the picture of the aircraft |
| `src/main.cpp` | state machine, control mapping, come-to-me |
| `tests/` | host-side tests, no hardware needed |
| `pi/` | Raspberry Pi bridge setup, if you go that route |

The MAVLink codec is hand-written rather than pulled from the official headers,
which are enormous and awkward to vendor. Two things make or break it: payload
fields go on the wire sorted by size rather than in declaration order, and the
checksum mixes in a per-message `CRC_EXTRA` byte. Both are handled, and both
were verified against an independent reference implementation that derives
`CRC_EXTRA` from the message definitions rather than copying the table.
