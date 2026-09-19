#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// Button input, read through the 74HC165 shift register (U8).
//
// How the bit order was derived, since it is not obvious from the netlist:
// the 74HC165 presents D7 on QH straight after a parallel load, then shifts
// D6, D5 ... D0 out on each clock. Reading eight bits MSB-first therefore
// walks D7 down to D0. Mapping the register's input pins to the nets found on
// the board gives:
//
//   U8.6  = D7 = SW_HPM  (SW6)   bit 7   A
//   U8.5  = D6 = BTN_7   (SW5)   bit 6   B
//   U8.4  = D5 = BTN_6   (SW7)   bit 5   HOME
//   U8.3  = D4 = BTN_5   (SW8)   bit 4   DOWN
//   U8.14 = D3 = BTN_4   (SW4)   bit 3   LEFT
//   U8.13 = D2 = BTN_3   (SW3)   bit 2   RIGHT
//   U8.12 = D1 = BTN_2   (SW2)   bit 1   UP
//   U8.11 = D0 = BTN_1   (SW11)  bit 0   slide
//
// Note the low nibble: pins 11/12/13/14 are D0/D1/D2/D3. Assuming the reverse
// mirrors the D-pad and is exactly the bug this was caught with on hardware.
//
// The physical roles come from where the switches actually sit on the PCB.
// SW2/SW4/SW3/SW8 form a clean cross around (78.4, 128.8) mm, so they are the
// D-pad. SW6 and SW5 sit on the up-right/down-left diagonal to its right,
// which is the usual A/B pair, A being the outer one. SW11 is a slide switch,
// not a pushbutton, so it reads as a level rather than a press.
//
// CONFIRMED against the board's own silkscreen. The labels are drawn as vector
// outlines rather than KiCad text objects, which is why a text search of the
// board file finds nothing and an earlier version of this comment claimed the
// buttons were unlabelled. Rendering the F.SilkS artwork shows UP / LEFT /
// RIGHT / DOWN with arrows on the cross, A on the outer button of the diagonal
// pair and B on the inner one, HOME on SW7 and START on SW10. Every role below
// matches. Nothing here needs swapping.
// ---------------------------------------------------------------------------

namespace btn {

enum Id : uint8_t {
  UP = 0,      // SW2,  BTN_2
  DOWN,        // SW8,  BTN_5
  LEFT,        // SW4,  BTN_4
  RIGHT,       // SW3,  BTN_3
  A,           // SW6,  SW_HPM
  B,           // SW5,  BTN_7
  SELECT,      // SW7,  BTN_6
  SLIDE,       // SW11, BTN_1 -- slide switch, a level not a press
  BOOT,        // SW10, wired straight to GPIO9
  COUNT
};

void begin();

// Samples the register and updates edge state. Call this every loop.
void poll();

bool down(Id b);        // currently held
bool pressed(Id b);     // went down since the last poll
bool released(Id b);    // came up since the last poll

// Milliseconds the button has been held, or 0 if it is up.
uint32_t heldMs(Id b);

// True if any of the eight game buttons is down. Used to cancel a pending
// flight-home, so the slide switch is deliberately excluded.
bool anyDown();

// Raw register byte, for the diagnostics mode in main.
uint8_t rawRegister();

const char *name(Id b);

}  // namespace btn
