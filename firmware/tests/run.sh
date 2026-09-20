#!/usr/bin/env bash
# Host-side tests. These need only g++ and python3; no badge, no drone.
#
#   ./tests/run.sh
#
# They cover the three things that are expensive to debug on real hardware:
# the MAVLink wire format, the standoff geometry that keeps the drone off the
# person holding the badge, and the shake detector's ability to tell a shake
# from walking.
set -euo pipefail
cd "$(dirname "$0")/.."
BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT

CXX="${CXX:-g++}"
FLAGS="-std=c++17 -Wall -Wextra -O2"
fail=0

echo "=== MAVLink wire format ==="
cp src/mavlink_min.h src/mavlink_min.cpp \
   tests/mavlink_test.cpp tests/ref.py tests/verify.py "$BUILD/"
# Minimal Arduino.h so the codec compiles off-target.
printf '#pragma once\n#include <stdint.h>\n#include <stddef.h>\n#include <string.h>\n' > "$BUILD/Arduino.h"
( cd "$BUILD" && $CXX $FLAGS -Wno-unused-parameter -I. mavlink_min.cpp mavlink_test.cpp -o mavtest \
  && ./mavtest > frames.txt && python3 verify.py frames.txt ) || fail=1

echo
echo "=== standoff geometry ==="
python3 tests/extract.py src/main.cpp 'constexpr double kEarthR' '// --- stick shaping' "$BUILD/geom.inc"
cp tests/geometry_test.cpp "$BUILD/"
( cd "$BUILD" && $CXX $FLAGS geometry_test.cpp -o geotest && ./geotest ) || fail=1

echo
echo "=== shake detector ==="
python3 tests/extract.py src/imu.cpp 'namespace detail {' '}  // namespace detail' "$BUILD/detector.inc"
cp tests/shake_test.cpp include/config.h "$BUILD/"
( cd "$BUILD" && $CXX $FLAGS -Wno-unused-parameter -I. shake_test.cpp -o shaketest && ./shaketest ) || fail=1

echo
echo "=== button map vs the board file ==="
python3 tests/netlist_test.py || fail=1

echo
echo "=== detection packet: the badge's parser against the Pi's packer ==="
# The handoff's §7 complaint was that the overlay's "verified wire format" was
# Python checked against Python. This compiles the firmware's own parser and
# feeds it bytes the real packer produced, with the address sanitizer on so a
# read past the buffer fails here rather than rebooting a badge.
python3 tests/extract.py src/badgecam_main.cpp '// [detparse]' '// [/detparse]' "$BUILD/detparse.inc"
cp tests/detpacket_test.cpp "$BUILD/"
( cd "$BUILD" && $CXX $FLAGS -fsanitize=address,undefined -I. detpacket_test.cpp -o dettest ) || fail=1
python3 tests/detpacket_gen.py "$BUILD"
if ( cd "$BUILD" && ./dettest packets.txt > got.txt ); then
  if diff -u "$BUILD/expect.txt" "$BUILD/got.txt" > "$BUILD/det.diff"; then
    echo "  ok   $(wc -l < "$BUILD/expect.txt" | tr -d ' ') packets parsed back to the values that went in"
  else
    echo "  FAIL the C++ parser did not recover what the Python packer sent:"
    sed -n '1,40p' "$BUILD/det.diff"
    fail=1
  fi
else
  echo "  FAIL the parser crashed or the sanitizer tripped"
  fail=1
fi

echo
echo "=== detection tracker ==="
python3 tests/track_test.py || fail=1

echo
echo "=== chase controller ==="
python3 tests/chase_test.py || fail=1

echo
echo "=== autopilot end to end ==="
# Runs the real badgedrive.py and speaks to it over real sockets. Slower than
# the others and worth it: the unit tests cover the arithmetic, this covers
# the wiring, and the wiring is where a detection packet once looked like a
# dead link and cut the motors.
python3 tests/autopilot_test.py || fail=1

echo
if [ "$fail" -eq 0 ]; then echo "ALL TESTS PASSED"; else echo "SOME TESTS FAILED"; fi
exit $fail
