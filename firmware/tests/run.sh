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
if [ "$fail" -eq 0 ]; then echo "ALL TESTS PASSED"; else echo "SOME TESTS FAILED"; fi
exit $fail
