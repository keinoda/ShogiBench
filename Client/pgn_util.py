# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                           #
#   OpenBench is a chess engine testing framework by Andrew Grant.          #
#   <https://github.com/AndyGrant/OpenBench>  <andrew@grantnet.us>          #
#                                                                           #
#   OpenBench is free software: you can redistribute it and/or modify       #
#   it under the terms of the GNU General Public License as published by    #
#   the Free Software Foundation, either version 3 of the License, or       #
#   (at your option) any later version.                                     #
#                                                                           #
#   OpenBench is distributed in the hope that it will be useful,            #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#   GNU General Public License for more details.                            #
#                                                                           #
#   You should have received a copy of the GNU General Public License       #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.   #
#                                                                           #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

import bz2
import os
import re
import sys

## Local imports must only use "import x", never "from x import ..."

# For use externally
REGEX_COMMENT_VERBOSE  = r'(book|[+-]?M?\d+(?:\.\d+)?/\d+ [\d.]+s, n=\d+, sd=\d+)'
REGEX_COMMENT_COMPACT  = r'(book|[+-]?M?\d+(?:\.\d+)?)/\d+'
REGEX_MOVE_AND_COMMENT = r'\s*(?:\d+\. )?([a-zA-Z0-9+=#*-]+) (?:\s*\{\s*([^}]*)\s*\})?'
REGEX_GAME_RESULT      = r'\s*(1-0|0-1|1/2-1/2|\*)'

def pgn_iterator(fname):
    with open(fname) as pgn:
        while True:
            headers   = pgn_header_list(iter(lambda: pgn.readline().rstrip(), ''))
            move_list = ' '.join(iter(lambda: pgn.readline().rstrip(), ''))
            if not headers or not move_list:
                break
            yield (headers, move_list)


def pgn_iterator_from_offset(fname, offset=0, final=False):
    """追記中のPGNから、完全に書き終わった対局だけを位置付きで返す。"""

    with open(fname) as pgn:
        pgn.seek(offset)

        while True:
            header_lines = []
            while True:
                line = pgn.readline()
                if line == '':
                    return
                line = line.rstrip()
                if not line:
                    break
                header_lines.append(line)

            # 対局間の余分な空行は読み飛ばす。
            if not header_lines:
                continue

            move_lines = []
            reached_eof = False
            while True:
                line = pgn.readline()
                if line == '':
                    reached_eof = True
                    break
                line = line.rstrip()
                if not line:
                    break
                move_lines.append(line)

            if not move_lines:
                return

            move_list = ' '.join(move_lines)
            complete = re.search(r'(?:1-0|0-1|1/2-1/2|\*)\s*$', move_list)

            # 実行中のEOFは、match runnerがまだ続きを書く可能性がある。
            if not complete or (reached_eof and not final):
                return

            yield (pgn_header_list(header_lines), move_list, pgn.tell())

            if reached_eof:
                return

def pgn_header_list(lines):
    # PGN Format: [<Header> "<Value>"]
    return { f.split()[0][1:] : re.search(r'"([^"]*)"', f).group(1) for f in lines }

def pgn_strip_headers(headers, compact):

    # 7-Tag Roster that is required to be a legal PGN
    desired = [
        'Event',  'Site',
        'Date',   'Round',
        'White',  'Black',
        'Result',
    ]

    desired += [
        'FEN',         # Required due to .epd openings
        'TimeControl', # Useful to extract statistics
        'Variant',     # Useful to account for FRC/DFRC
        'ScaleFactor', # Useful to extract statistics
    ]

    if not compact: # Useful to reconstruct time events
        desired += ['GameEndTime']

    # PGN Format: [<Header> "<Value>"]
    return '\n'.join('[%s "%s"]' % (f, headers[f]) for f in desired if f in headers)

def pgn_strip_movelist(move_text, compact):

    # May parse book, otherwise Score for Compact, Score Depth/SelDepth Time Nodes for Verbose
    comment_regex = re.compile(REGEX_COMMENT_COMPACT if compact else REGEX_COMMENT_VERBOSE)

    # Parses the move number, the SAN, and an optional comment
    one_ply_regex = re.compile(r'\s*(?:\d+\. )?([a-zA-Z0-9+=#*-]+) (?:\s*\{\s*([^}]*)\s*\})?')

    # Captures the trailing game result
    result_regex  = re.compile(r'\s*(1-0|0-1|1/2-1/2|\*)')

    stripped = '' # Add each: <Move> {<Comment>}
    for move, comment in re.compile(REGEX_MOVE_AND_COMMENT).findall(move_text):
        match = re.search(comment_regex, comment)
        stripped += '%s {%s} ' % (move, match.group() if match else 'unknown')

    # PGNs expect trailing game result text
    return stripped + re.compile(REGEX_GAME_RESULT).search(move_text).group(1)

def strip_entire_pgn(file_name, scale_factor, compact):

    stripped = ''
    for header_dict, move_text in pgn_iterator(file_name):
        header_dict['ScaleFactor'] = str(scale_factor)
        stripped += pgn_strip_headers(header_dict, compact) + '\n\n'
        stripped += pgn_strip_movelist(move_text, compact) + '\n\n'

    return stripped


def strip_new_pgns(file_name, offset, scale_factor, compact, final=False):

    if not os.path.isfile(file_name):
        return '', offset

    stripped   = ''
    next_offset = offset
    for header_dict, move_text, complete_offset in pgn_iterator_from_offset(
            file_name, offset=offset, final=final):
        header_dict['ScaleFactor'] = str(scale_factor)
        stripped += pgn_strip_headers(header_dict, compact) + '\n\n'
        stripped += pgn_strip_movelist(move_text, compact) + '\n\n'
        next_offset = complete_offset

    return stripped, next_offset


def compress_new_pgns(file_names, offsets, scale_factor, compact, final=False):
    """前回位置以降の完全な対局だけを圧縮し、成功後に使う次位置を返す。"""

    text         = ''
    next_offsets = dict(offsets)

    for fname in file_names:
        chunk, next_offset = strip_new_pgns(
            fname, offsets.get(fname, 0), scale_factor, compact, final=final)
        text += chunk
        next_offsets[fname] = next_offset

    return (bz2.compress(text.encode()) if text else None), next_offsets

def compress_list_of_pgns(file_names, scale_factor, compact):

    text = ''
    for fname in file_names:
        print ('Compressing %s...' % (fname))
        text += strip_entire_pgn(fname, scale_factor, compact)

    return bz2.compress(text.encode())
