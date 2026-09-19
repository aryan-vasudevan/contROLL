#!/usr/bin/env python3
"""Pull a named block out of a firmware source file so the host tests exercise
the shipped code rather than a retyped copy of it. If someone edits the
firmware and breaks an invariant, these tests notice."""
import io, sys

def extract(path, start_marker, end_marker, include_end=True):
    src = io.open(path, encoding='utf-8').read()
    a = src.index(start_marker)
    b = src.index(end_marker, a)
    return src[a:b + (len(end_marker) if include_end else 0)]

if __name__ == '__main__':
    path, start, end, out = sys.argv[1:5]
    body = extract(path, start, end)
    io.open(out, 'w', encoding='utf-8').write(body)
    print(f'{out}: {len(body)} chars from {path}')
