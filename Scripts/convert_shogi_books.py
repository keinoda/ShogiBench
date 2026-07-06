#!/usr/bin/env python3
# Convert shogi opening collections ("startpos moves ..." or "sfen ..." lines)
# into the bare 4-field SFEN format shogitest's OpeningBook parser expects.

import hashlib
import os
import sys
import zipfile

import shogi  # python-shogi


def to_sfen(line):
    line = line.strip()
    if not line:
        return None
    if line.startswith('startpos moves '):
        board = shogi.Board()
        for mv in line.split()[2:]:
            board.push_usi(mv)
    elif line.startswith('sfen '):
        board = shogi.Board(line[5:].strip())
    else:
        # Try it as a bare sfen
        board = shogi.Board(line)
    # shogitest refuses to start a game from an in-check position
    # (Game::new asserts !is_in_check), so such lines must be dropped
    if board.is_check():
        return 'IN_CHECK'
    out = board.sfen()
    assert len(out.split(' ')) == 4, out
    return out


def convert(src, book_name, outdir):
    lines, in_check = [], 0
    with open(src, encoding='utf-8') as fin:
        for lineno, line in enumerate(fin, 1):
            try:
                sfen = to_sfen(line)
            except Exception as e:
                raise SystemExit('%s:%d: %s (%s)' % (src, lineno, e, line.strip()[:60]))
            if sfen is None:
                continue
            if sfen == 'IN_CHECK':
                in_check += 1
                continue
            # Keep duplicate positions (transpositions): the upstream files
            # are de-facto standards and their multiplicity is part of them
            lines.append(sfen)

    content = ('\n'.join(lines) + '\n').encode('utf-8')
    sha256 = hashlib.sha256(content).hexdigest()

    epd_path = os.path.join(outdir, book_name)
    with open(epd_path, 'wb') as fout:
        fout.write(content)

    zip_path = epd_path + '.zip'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.write(epd_path, arcname=book_name)

    print('%-42s %6d positions (%d in-check dropped)  sha256=%s  zip=%dKB'
          % (book_name, len(lines), in_check, sha256, os.path.getsize(zip_path) // 1024))
    return sha256


if __name__ == '__main__':
    raw = os.path.join(os.path.dirname(__file__), 'raw')
    out = os.path.join(os.path.dirname(__file__), 'out')
    os.makedirs(out, exist_ok=True)
    for src, name in [
        ('taya36.sfen',           'taya36_shogi_sfen.epd'),
        ('gokaku.sfen',           'dlshogi_gokaku_sfen.epd'),
        ('floodgate32-80.sfen',   'dlshogi_floodgate32to80_sfen.epd'),
        ('start_sfens_ply24.txt', 'yaneuraou2025_ply24_shogi_sfen.epd'),
        ('start_sfens_ply32.txt', 'yaneuraou2025_ply32_shogi_sfen.epd'),
    ]:
        path = os.path.join(raw, src)
        if not os.path.exists(path):
            print('SKIP (missing): %s' % (src))
            continue
        convert(path, name, out)
