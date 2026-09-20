// Parse detection packets with the badge's own parser, on a host.
//
// detparse.inc is pulled verbatim out of src/badgecam_main.cpp by extract.py,
// so this exercises the code that actually ships rather than a copy of it.
// Edit the firmware's parser and this notices.
//
// Reads packets.txt (name + hex, produced by oak_stream.py's real struct
// formats) and prints what it recovered. detpacket_gen.py wrote expect.txt
// from the values that went in, and run.sh diffs the two.

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "detparse.inc"

static int hexval(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

int main(int argc, char **argv) {
  if (argc < 2) { fprintf(stderr, "usage: detpackettest packets.txt\n"); return 2; }
  FILE *f = fopen(argv[1], "r");
  if (!f) { fprintf(stderr, "cannot open %s\n", argv[1]); return 2; }

  char line[65536];
  while (fgets(line, sizeof(line), f)) {
    std::string s(line);
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r')) s.pop_back();
    if (s.empty()) continue;

    const size_t sp = s.find(' ');
    const std::string name = s.substr(0, sp);
    const std::string hex = (sp == std::string::npos) ? "" : s.substr(sp + 1);

    std::vector<unsigned char> pkt;
    for (size_t i = 0; i + 1 < hex.size(); i += 2) {
      const int hi = hexval(hex[i]), lo = hexval(hex[i + 1]);
      if (hi < 0 || lo < 0) break;
      pkt.push_back((unsigned char)((hi << 4) | lo));
    }

    // 16 is kMaxBoxes in the firmware. Sized exactly, so a parser that wrote
    // one box too many would run off the end here too and be caught by the
    // sanitizer the test suite builds with.
    DetBox boxes[16];
    uint16_t w = 0, h = 0;
    const int n = parseDetections(pkt.empty() ? (const unsigned char *)"" : pkt.data(),
                                  (int)pkt.size(), boxes, 16, &w, &h);

    if (n < 0) {
      printf("%s -1\n", name.c_str());
      continue;
    }
    printf("%s %d %u %u", name.c_str(), n, (unsigned)w, (unsigned)h);
    for (int i = 0; i < n; i++) {
      printf(" %d,%d,%u,%u,%u,%u,%s", boxes[i].x, boxes[i].y,
             (unsigned)boxes[i].w, (unsigned)boxes[i].h,
             (unsigned)boxes[i].conf, (unsigned)boxes[i].id, boxes[i].label);
    }
    printf("\n");
  }
  fclose(f);
  return 0;
}
