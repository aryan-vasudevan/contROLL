#!/usr/bin/env python3
"""Re-derive the button bit map from the KiCad board file and compare it against
what the firmware compiles in. This is the check that would catch a button
mapping that drifted away from the hardware."""
import re, sys, os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
CANDIDATES = [
    os.path.join(PROJECT, '..', 'hardware', 'badge.kicad_pcb'),
    os.path.join(PROJECT, 'hardware', 'badge.kicad_pcb'),
    os.path.join(PROJECT, '..', 'Archive 2', 'badge.kicad_pcb'),
]
pcb = next((p for p in CANDIDATES if os.path.exists(p)), None)
if not pcb:
    print('  skipped: badge.kicad_pcb not found next to the project')
    sys.exit(0)

data = open(pcb, encoding='utf-8', errors='replace').read()
net2pads = defaultdict(list); pos = {}
i = 0
while True:
    i = data.find('(footprint ', i)
    if i < 0: break
    d = 0; j = i
    while True:
        c = data[j]
        if c == '(': d += 1
        elif c == ')':
            d -= 1
            if d == 0: break
        j += 1
    blk = data[i:j+1]
    r = re.search(r'\(property "Reference" "([^"]*)"', blk)
    at = re.search(r'\(at ([-\d.]+) ([-\d.]+)', blk)
    ref = r.group(1) if r else '?'
    if at: pos[ref] = (float(at.group(1)), float(at.group(2)))
    for pm in re.finditer(r'\(pad "([^"]*)"(?:[^()]|\((?:[^()]|\([^()]*\))*\))*?\(net \d+ "([^"]*)"\)', blk):
        net2pads[pm.group(2)].append((ref, pm.group(1)))
    i = j + 1

# 74HC165 input pin -> parallel data bit. QH presents D7 straight after a load,
# so reading eight bits MSB-first walks D7 down to D0.
PIN_TO_D = {'14':0, '13':1, '12':2, '11':3, '3':4, '4':5, '5':6, '6':7}
bit_to_net = {PIN_TO_D[pad]: net
              for net, pads in net2pads.items()
              for ref, pad in pads if ref == 'U8' and pad in PIN_TO_D}
net_to_sw = {net: ref for net in bit_to_net.values()
             for ref, _ in net2pads[net] if ref.startswith('SW')}

# D-pad roles come from geometry: the four cross switches around a common centre.
dpad = {sw: pos[sw] for sw in ('SW2','SW3','SW4','SW8')}
cx = sum(p[0] for p in dpad.values()) / 4
cy = sum(p[1] for p in dpad.values()) / 4
role_of = {}
for sw, (x, y) in dpad.items():
    role_of[sw] = ('UP' if y < cy - 3 else 'DOWN' if y > cy + 3 else
                   'LEFT' if x < cx - 3 else 'RIGHT')
sw_of = {v: k for k, v in role_of.items()}

expect = {'UP': sw_of['UP'], 'DOWN': sw_of['DOWN'],
          'LEFT': sw_of['LEFT'], 'RIGHT': sw_of['RIGHT'],
          'A': 'SW6', 'B': 'SW5', 'SELECT': 'SW7', 'SLIDE': 'SW11'}

src = open(os.path.join(PROJECT, 'src', 'buttons.cpp'), encoding='utf-8').read()
fw = {m.group(1): int(m.group(2))
      for m in re.finditer(r'constexpr uint8_t BIT_(\w+)\s*=\s*(\d+);', src)}

fails = 0
for role, sw in expect.items():
    got = net_to_sw.get(bit_to_net[fw[role]])
    ok = got == sw
    fails += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {role:7} -> bit {fw[role]} -> "
          f"{bit_to_net[fw[role]]:9} -> {got} (board says {sw})")

ok = sorted(fw.values()) == list(range(8))
fails += not ok
print(f"  {'ok  ' if ok else 'FAIL'} the eight bits are a permutation of 0..7")
print(f'\n  {fails} failure(s)')
sys.exit(1 if fails else 0)
