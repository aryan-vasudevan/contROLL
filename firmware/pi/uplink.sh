#!/usr/bin/env bash
# Can this Pi reach the cloud detector while still being the badge's access
# point? Run it on the Pi.
#
#     ./uplink.sh
#
# The whole difficulty is in one sentence: the Pi has one Wi-Fi radio and it
# is the access point. A single radio can be an AP or a client, not both, so
# every route to the internet has to come in on a *second* interface.
#
#   usb0 / enx*   a phone USB-tethered to the Pi. Android is plug-and-play
#                 (RNDIS). This is the only option that keeps the whole rig
#                 self-contained and moving -- the phone rides on the car.
#   eth0          a cable. Fine on a bench, useless while driving.
#   (a laptop)    the Mac shares its connection and the Pi routes through it.
#                 Works, and means carrying the laptop.
#
# An iPhone needs usbmuxd, which is not on Pi OS Lite, and installing it needs
# the internet you are trying to get -- so sideload the .deb from a Mac, or
# borrow an Android.
#
# Nothing here changes any configuration. NetworkManager already prefers the
# interface that has a gateway, and the AP profile has none, so a tether
# usually just works. This says whether it did, and if not, which link broke.

set -uo pipefail

RF_HOST="${RF_HOST:-serverless.roboflow.com}"
ok=0; bad=0
say()  { printf '  %-42s %s\n' "$1" "$2"; }
good() { say "$1" "ok    $2"; ok=$((ok+1)); }
fail() { say "$1" "FAIL  $2"; bad=$((bad+1)); }

echo "=== interfaces ==="
ap_if=""
for i in $(ls /sys/class/net | grep -v '^lo$'); do
  state=$(cat "/sys/class/net/$i/operstate" 2>/dev/null || echo "?")
  addr=$(ip -4 -o addr show dev "$i" 2>/dev/null | awk '{print $4}' | head -1)
  mode=""
  if iw dev "$i" info >/dev/null 2>&1; then
    mode=$(iw dev "$i" info 2>/dev/null | awk '/type/{print $2}')
    [ "$mode" = "AP" ] && ap_if="$i"
  fi
  printf '  %-10s %-8s %-18s %s\n' "$i" "$state" "${addr:--}" "${mode:+wifi:$mode}"
done

echo
echo "=== the access point the badge joins ==="
if [ -n "$ap_if" ]; then
  good "$ap_if is in AP mode" "$(iw dev "$ap_if" info | awk '/ssid/{print $2}')"
else
  fail "no interface is in AP mode" "the badge has nothing to join"
fi

echo
echo "=== a second way out ==="
# The default route must NOT be the AP interface. If it is, there is no uplink
# and every cloud call will hang until it times out.
route_if=$(ip route show default 2>/dev/null | awk '{print $5}' | head -1)
gw=$(ip route show default 2>/dev/null | awk '{print $3}' | head -1)
if [ -z "$route_if" ]; then
  fail "no default route" "nothing to reach the internet through"
  echo "         plug an Android phone in by USB and turn on USB tethering,"
  echo "         or share the Mac's connection and add a route via it."
elif [ "$route_if" = "$ap_if" ]; then
  fail "default route is $route_if" "that is the AP; it has no uplink"
else
  good "default route via $route_if" "gw $gw"
fi

echo
echo "=== the path to the detector ==="
if ping -c1 -W2 1.1.1.1 >/dev/null 2>&1; then good "raw IP reachable" "1.1.1.1"
else fail "cannot reach 1.1.1.1" "no internet on any interface"; fi

if getent hosts "$RF_HOST" >/dev/null 2>&1; then good "DNS resolves $RF_HOST" ""
else fail "DNS cannot resolve $RF_HOST" "routed but no working resolver"; fi

code=$(curl -s -o /dev/null -w '%{http_code} %{time_total}s' --max-time 10 \
       "https://$RF_HOST" 2>/dev/null)
case "$code" in
  000*|"") fail "https to $RF_HOST" "no answer (captive portal? firewall?)" ;;
  *) good "https to $RF_HOST" "$code" ;;
esac

echo
echo "=== inference, for real ==="
if [ -z "${RF_API_KEY:-}" ]; then
  say "RF_API_KEY" "skip  not set; export it and run again"
elif [ ! -f "${1:-}" ]; then
  say "no sample frame given" "skip  ./uplink.sh frame.jpg to time a real call"
  echo "         grab one with:  oak_stream.py --save 1"
else
  python3 detect.py --bench "$1" || bad=$((bad+1))
fi

echo
if [ "$bad" -eq 0 ]; then
  echo "$ok checks passed. The car can run the cloud detector with no laptop."
else
  echo "$bad check(s) failed. Until they pass, --detect will hang and --nn"
  echo "(detection on the camera, no internet at all) is the path that works."
fi
exit "$bad"
