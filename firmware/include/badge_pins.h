#pragma once
// ---------------------------------------------------------------------------
// Pin map for the Hack the North hacker badge.
//
// Every number here was read out of badge.kicad_pcb by walking each footprint's
// pads and collecting the net each pad sits on. The ESP32-C3-MINI-1-N4 module
// pad -> GPIO translation is the stock module pinout, and it is confirmed by
// five independent anchors in the netlist itself:
//
//     module pad 8  -> ESP32_EN            (module EN pin)
//     module pad 22 -> Net-(U9-IO8)        (net name states IO8)
//     module pad 23 -> ESP32_BOOT          (IO9 is the C3 boot strap)
//     module pad 26 -> Net-(U9-IO18)       (net name states IO18)
//     module pad 27 -> Net-(U9-IO19)       (net name states IO19)
//
// Reference designators below match the schematic so you can jump straight to
// the part if something reads wrong on real hardware.
// ---------------------------------------------------------------------------

// --- Buttons: 74HC165 (U8) parallel-in / serial-out shift register ---------
// All eight switch nets are pulled to +3V3 by 10k (R23-R30) and shorted to GND
// by the switch, so every button reads ACTIVE LOW at the register inputs.
#define PIN_SR_QH     7   // module pad 21 <- U8.9  QH   serial data out
#define PIN_SR_SHLD   20  // module pad 30 -> U8.1  SH/LD parallel load, active low
#define PIN_SR_CLK    21  // module pad 31 -> U8.2  CLK  shift clock

// --- I2C bus ---------------------------------------------------------------
// Shared by the SC7A20 accelerometer (U2) and the MFRC522 NFC reader (U7).
// Pulled up by 4k7 R34/R36 to +3V3, so do not enable internal pull-ups.
#define PIN_I2C_SDA   5   // module pad 19
#define PIN_I2C_SCL   6   // module pad 20

// SC7A20 (U2) strapping, read off the board:
//   CS  (U2.10) pulled high through 10k R5  -> I2C interface selected
//   SDO (U2.1)  pulled high through 10k R4  -> 7-bit address is 0x19
#define ACCEL_I2C_ADDR 0x19

// --- Status LEDs -----------------------------------------------------------
// GPIO3 -> SN74LVC1T45 level shifter (U11) -> 100R R3 -> LED1.DI, then daisy
// chained LED1 -> LED2 -> LED3 -> LED4 -> LED5 -> LED6. LED6.DO is unused, so
// the chain is exactly six pixels. They run off +5V.
//
// +5V is OR-ed from two sources by U12 (LM66200 ideal-diode mux): USB VBUS on
// U12.3, and the MT3608 boost output on U12.6. The 3V3 LDO (U5) is fed from
// that same +5V node, so the whole badge, ESP32 included, hangs off it.
//
// Slide switch SW1, silkscreened OFF/ON, gates only the BATTERY path into the
// boost. On USB the badge and the LEDs run with SW1 in either position; on
// battery alone SW1 has to be ON or nothing comes up at all.
#define PIN_LED_DIN   3
#define LED_COUNT     6

// --- Display FPC (AFC07-S12ECA-00, FPC1) ----------------------------------
// Panel is an HS20HS072RX on a 4-wire serial bus. Pins are listed so the
// display is easy to bring up later; this firmware does not drive it, because
// the panel's controller was not identifiable from the schematic alone.
#define PIN_DISP_DC   0   // DISP_RS,   module pad 12
#define PIN_DISP_SCLK 1   // DISP_SCL,  module pad 13
#define PIN_DISP_CS   2   // DISP_CS,   module pad 5
#define PIN_DISP_RST  4   // DISP_RST,  module pad 18
#define PIN_DISP_MOSI 10  // DISP_SDA,  module pad 16

// --- Boot button (SW10) ----------------------------------------------------
// Wired straight to IO9, not through the shift register. Active low.
#define PIN_BOOT_BTN  9
