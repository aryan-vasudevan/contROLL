#include <cstdio>
#include <cstdint>
#include <cmath>
#include <cstdlib>

#define DEG_TO_RAD 0.017453292519943295
#define RAD_TO_DEG 57.29577951308232
#define COME_STANDOFF_M 4.0f

struct Beacon { bool set; int32_t latE7, lonE7; };
static Beacon gBeacon;

#include "geom.inc"

static int fails = 0;
static void check(bool ok, const char *msg, double got, double want) {
  printf("  %s %-52s got=%.3f want=%.3f\n", ok ? "ok  " : "FAIL", msg, got, want);
  if (!ok) fails++;
}

// Independent haversine, for cross-checking the equirectangular approximation.
static double haversine(double lat1, double lon1, double lat2, double lon2) {
  const double R = 6371000.0;
  const double p1 = lat1 * DEG_TO_RAD, p2 = lat2 * DEG_TO_RAD;
  const double dp = p2 - p1, dl = (lon2 - lon1) * DEG_TO_RAD;
  const double a = sin(dp/2)*sin(dp/2) + cos(p1)*cos(p2)*sin(dl/2)*sin(dl/2);
  return 2 * R * asin(sqrt(a));
}

int main() {
  // Waterloo, Ontario, where an F450 at a hackathon would plausibly be.
  const double bLat = 43.4730, bLon = -80.5449;
  gBeacon = { true, (int32_t)llround(bLat*1e7), (int32_t)llround(bLon*1e7) };

  struct Case { const char *name; double dLatM, dLonM; };
  Case cases[] = {
    {"drone 50 m due north",      50,   0},
    {"drone 50 m due south",     -50,   0},
    {"drone 50 m due east",        0,  50},
    {"drone 50 m due west",        0, -50},
    {"drone 120 m northeast",     85,  85},
    {"drone 300 m southwest",   -212,-212},
    {"drone 6 m away",             4,   4},
  };

  const double mPerDegLat = 111320.0;
  const double mPerDegLon = 111320.0 * cos(bLat * DEG_TO_RAD);

  for (auto &c : cases) {
    const double dLat = bLat + c.dLatM / mPerDegLat;
    const double dLon = bLon + c.dLonM / mPerDegLon;
    const int32_t dLatE7 = (int32_t)llround(dLat*1e7), dLonE7 = (int32_t)llround(dLon*1e7);

    printf("%s:\n", c.name);

    const double dApprox = distanceM(dLatE7, dLonE7, gBeacon.latE7, gBeacon.lonE7);
    const double dTrue   = haversine(dLat, dLon, bLat, bLon);
    check(fabs(dApprox - dTrue) < 0.05, "distance matches haversine within 5 cm", dApprox, dTrue);

    int32_t tLat, tLon;
    standoffTarget(dLatE7, dLonE7, tLat, tLon);

    const double tToBeacon = haversine(tLat/1e7, tLon/1e7, bLat, bLon);
    check(fabs(tToBeacon - COME_STANDOFF_M) < 0.15,
          "target sits COME_STANDOFF_M from the beacon", tToBeacon, COME_STANDOFF_M);

    // The target must be nearer the drone than the beacon is, i.e. the drone
    // stops short rather than overflying the person.
    const double tToDrone = haversine(tLat/1e7, tLon/1e7, dLat, dLon);
    check(tToDrone < dTrue + 1e-6,
          "target is on the drone's side, not past the beacon", tToDrone, dTrue);

    // Target, beacon and drone must be collinear: the standoff point lies on
    // the straight line in, so the approach does not curve around the person.
    check(fabs(tToDrone + tToBeacon - dTrue) < 0.2,
          "target lies on the line between drone and beacon",
          tToDrone + tToBeacon, dTrue);
  }

  // Degenerate case: drone already directly over the beacon.
  printf("drone directly over beacon:\n");
  int32_t tLat, tLon;
  standoffTarget(gBeacon.latE7, gBeacon.lonE7, tLat, tLon);
  check(tLat == gBeacon.latE7 && tLon == gBeacon.lonE7,
        "holds position instead of picking a random direction", tLat, gBeacon.latE7);

  printf("\nFAILURES: %d\n", fails);
  return fails ? 1 : 0;
}
