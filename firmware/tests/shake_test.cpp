#include <cstdio>
#include <cstdint>
#include <cmath>
#include "config.h"

// Globals the detector owns, mirrored from imu.cpp.
static float gX, gY, gZ, gBaseline = 1.0f, gJolt = 0;
static bool gArmedAboveThreshold = false;
static uint8_t gCrossings = 0;
static uint32_t gFirstCrossingMs = 0, gLastFireMs = 0, gStartMs = 0;
static bool gHasFired = false;
static const uint32_t kStartupGraceMs = 1500;

#include "detector.inc"

static int fails = 0;
static void check(bool ok, const char *msg) {
  printf("  %s %s\n", ok ? "ok  " : "FAIL", msg);
  if (!ok) fails++;
}

// Drive the detector at 100 Hz with a vertical acceleration profile on top of
// gravity, and report how many shakes fired.
struct Sim {
  uint32_t t = 0;
  int fired = 0;
  void reset() { t = 0; fired = 0; detail::reset(0); }
  void run(double seconds, double amplitudeG, double hz, double noiseG = 0.0) {
    const int n = (int)(seconds * 100);
    for (int i = 0; i < n; i++) {
      const double phase = 2 * M_PI * hz * (t / 1000.0);
      const double a = amplitudeG * sin(phase);
      // Simple deterministic jitter, so runs are repeatable.
      const double noise = noiseG * sin(t * 1.7);
      if (detail::feedSample(0.02f, 0.02f, (float)(1.0 + a + noise), t)) fired++;
      t += 10;
    }
  }
  void settle(double seconds) { run(seconds, 0.0, 0.0, 0.002); }
};

int main() {
  Sim s;

  printf("badge sitting on a table (0.002 g noise, 5 s):\n");
  s.reset(); s.settle(5);
  check(s.fired == 0, "no shake from a badge at rest");

  printf("walking with the badge on a lanyard (0.35 g at 2 Hz, 10 s):\n");
  s.reset(); s.settle(1); s.run(10, 0.35, 2.0, 0.02);
  check(s.fired == 0, "walking does not call the drone");

  printf("jogging, badge swinging harder (0.7 g at 2.5 Hz, 10 s):\n");
  s.reset(); s.settle(1); s.run(10, 0.7, 2.5, 0.03);
  check(s.fired == 0, "even a hard swing stays below the threshold");

  printf("one sharp knock (single 2.5 g half-cycle):\n");
  s.reset(); s.settle(1);
  s.run(0.10, 2.5, 5.0);     // one half period at 5 Hz
  s.settle(3);
  check(s.fired == 0, "a single knock is not a shake");

  printf("two knocks a second apart:\n");
  s.reset(); s.settle(1);
  s.run(0.10, 2.5, 5.0); s.settle(1.0); s.run(0.10, 2.5, 5.0); s.settle(2);
  check(s.fired == 0, "two knocks still below the crossing count");

  printf("deliberate shake (2.5 g at 5 Hz for 1.0 s):\n");
  s.reset(); s.settle(1); s.run(1.0, 2.5, 5.0); s.settle(2);
  check(s.fired == 1, "a real shake fires exactly once");

  printf("vigorous shake (4 g at 7 Hz for 1.5 s):\n");
  s.reset(); s.settle(1); s.run(1.5, 4.0, 7.0); s.settle(2);
  check(s.fired == 1, "a harder shake also fires exactly once, not repeatedly");

  printf("two shakes back to back (cooldown check):\n");
  s.reset(); s.settle(1);
  s.run(1.0, 2.5, 5.0);
  s.settle(0.5);
  s.run(1.0, 2.5, 5.0);
  s.settle(1);
  check(s.fired == 1, "second shake inside the cooldown is suppressed");

  printf("two shakes separated by more than the cooldown:\n");
  s.reset(); s.settle(1);
  s.run(1.0, 2.5, 5.0);
  s.settle(5);
  s.run(1.0, 2.5, 5.0);
  s.settle(1);
  check(s.fired == 2, "a later shake fires again once the cooldown expires");

  printf("badge turned upside down and held there:\n");
  s.reset(); s.settle(1);
  for (int i = 0; i < 300; i++) { detail::feedSample(0, 0, -1.0f, s.t); s.t += 10; }
  check(s.fired == 0, "a steady orientation change is absorbed by the baseline");
  printf("  baseline settled to %.3f g\n", detail::baseline());

  printf("\nFAILURES: %d\n", fails);
  return fails ? 1 : 0;
}
