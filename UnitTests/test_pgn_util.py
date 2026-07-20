#!/bin/python3

import os
import sys
import re
import bz2
import tempfile
import unittest

# Needed to include from ../Client/*.py
PARENT = os.path.join(os.path.dirname(__file__), os.path.pardir)
sys.path.append(os.path.abspath(os.path.join(PARENT, 'Client')))

from pgn_util import compress_new_pgns, pgn_iterator, pgn_strip_movelist
from pgn_util import REGEX_COMMENT_COMPACT, REGEX_COMMENT_VERBOSE


def sample_game(event, result='1-0'):
    return (
        '[Event "%s"]\n'
        '[Site "Local"]\n'
        '[Date "2026.07.20"]\n'
        '[Round "1"]\n'
        '[White "dev"]\n'
        '[Black "base"]\n'
        '[Result "%s"]\n'
        '\n'
        '1. 7g7f {book} 3c3d {book} %s'
    ) % (event, result, result)

def verify_stripped_move_list(move_list, compact):

    special_comments = [ 'book', 'unknown' ]
    comment_regex = re.compile(REGEX_COMMENT_COMPACT if compact else REGEX_COMMENT_VERBOSE)

    for move, comment in re.findall(r'([a-zA-Z0-9+=#-]+)\s\{([^}]*)\}', move_list):
        assert comment in special_comments or comment_regex.match(comment)


class IncrementalPGNTests(unittest.TestCase):

    def test_only_new_complete_games_are_compressed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = os.path.join(tempdir, 'games.pgn')

            with open(path, 'w') as fout:
                fout.write(sample_game('first') + '\n\n')
                fout.write(sample_game('second'))

            first, offsets = compress_new_pgns([path], {}, 1.0, True)
            first_text = bz2.decompress(first).decode()
            self.assertIn('[Event "first"]', first_text)
            self.assertNotIn('[Event "second"]', first_text)

            # 追記中のEOFは、終局記号があっても区切りが来るまで回収しない。
            pending, unchanged = compress_new_pgns([path], offsets, 1.0, True)
            self.assertIsNone(pending)
            self.assertEqual(unchanged, offsets)

            with open(path, 'a') as fout:
                fout.write('\n\n')

            second, offsets = compress_new_pgns([path], offsets, 1.0, True)
            second_text = bz2.decompress(second).decode()
            self.assertNotIn('[Event "first"]', second_text)
            self.assertIn('[Event "second"]', second_text)

            empty, final_offsets = compress_new_pgns([path], offsets, 1.0, True)
            self.assertIsNone(empty)
            self.assertEqual(final_offsets, offsets)

    def test_final_checkpoint_accepts_complete_game_at_eof(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = os.path.join(tempdir, 'games.pgn')
            with open(path, 'w') as fout:
                fout.write(sample_game('final'))

            live, live_offsets = compress_new_pgns([path], {}, 1.0, True)
            self.assertIsNone(live)
            self.assertEqual(live_offsets[path], 0)

            final, final_offsets = compress_new_pgns(
                [path], {}, 1.0, True, final=True)
            self.assertIn('[Event "final"]', bz2.decompress(final).decode())
            self.assertGreater(final_offsets[path], 0)

if __name__ == '__main__':
    for example_pgn in [ 'example1.pgn', 'example2.pgn', 'example3.pgn', ]:
        for headers, move_list in pgn_iterator(example_pgn):
            for compact in [ True, False ]:
                verify_stripped_move_list(pgn_strip_movelist(move_list, compact), compact)
