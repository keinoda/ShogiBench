# SPSA (rshogi ラッパー) の .params テキスト処理。
#
# 受け付ける形式は rshogi / YaneuraOu tune.py / fishtest 系で共通の 7 カラム CSV:
#
#   名前, 型, 現在値, 最小, 最大, C_end(step), R_end(delta) [[NOT USED]] // コメント
#
# - '#' で始まる行は行コメント、'//' 以降は行内コメント
# - '[[NOT USED]]' の付いた行は「死にパラメータ」としてそのまま保持される
#   (rshogi 側でチューニング対象から外れるが、ファイル整合性のため残す)
#
# このモジュールは Django に依存しない。ワーカーはサーバから params テキストを
# 「原文のまま」受け取って rshogi の --init-from に渡すので、ここでのパースは
# 妥当性検証と GUI 表示用の構造化だけを担う。rshogi 本体のパーサは
# crates/tools/src/spsa_param_mapping.rs の parse_param_line と同じ規則。

import re

NOT_USED_MARKER = '[[NOT USED]]'

# USI オプション名として妥当な文字だけを許す (カンマ・空白・引用符は不可)
VALID_NAME = re.compile(r'^[A-Za-z0-9_.+\-]+$')


def parse_params_text(text):

    ## 7 カラム CSV テキストを [ { name, kind, value, min, max, c_end, r_end,
    ## not_used, comment } ] と [エラー文字列] に分解する。エラーがあっても
    ## パースできた行は返す (呼び出し側はエラーが空のときだけ採用する)

    rows, errors = [], []
    seen = set()

    for line_no, raw in enumerate(text.split('\n'), start=1):

        line = raw.strip()
        if not line or line.startswith('#'):
            continue

        # コメントを先に切り離してから [[NOT USED]] を判定する
        # (コメント内のマーカーを誤検出しないため。rshogi と同じ順序)
        body, _, comment = line.partition('//')
        not_used = NOT_USED_MARKER in body
        body     = body.replace(NOT_USED_MARKER, '')

        fields = [x.strip() for x in body.split(',')]
        if len(fields) < 7:
            errors.append('%d行目: 7カラム必要ですが %d カラムです' % (line_no, len(fields)))
            continue

        if len(fields) > 7:
            errors.append('%d行目: カラムが多すぎます (%d)。名前にカンマは使えません' % (line_no, len(fields)))
            continue

        name, kind = fields[0], fields[1]

        if not VALID_NAME.match(name):
            errors.append('%d行目: パラメータ名 "%s" に使えない文字があります' % (line_no, name))
            continue

        if name in seen:
            errors.append('%d行目: パラメータ名 "%s" が重複しています' % (line_no, name))
            continue
        seen.add(name)

        if kind not in ('int', 'float'):
            errors.append('%d行目: 型は int か float です (%s)' % (line_no, kind))
            continue

        try:
            value, vmin, vmax = float(fields[2]), float(fields[3]), float(fields[4])
            c_end, r_end      = float(fields[5]), float(fields[6])
        except ValueError:
            errors.append('%d行目: 数値を解釈できません (%s)' % (line_no, body.strip()))
            continue

        if vmin > vmax:
            errors.append('%d行目: %s の min が max を超えています' % (line_no, name))

        if not (vmin <= value <= vmax):
            errors.append('%d行目: %s の値が [min, max] の外です' % (line_no, name))

        if not not_used and c_end <= 0.0:
            errors.append('%d行目: %s の C_end は正の値が必要です' % (line_no, name))

        if not not_used and r_end <= 0.0:
            errors.append('%d行目: %s の R_end は正の値が必要です' % (line_no, name))

        rows.append({
            'name'     : name,
            'kind'     : kind,
            'value'    : value,
            'min'      : vmin,
            'max'      : vmax,
            'c_end'    : c_end,
            'r_end'    : r_end,
            'not_used' : not_used,
            'comment'  : comment.strip(),
        })

    if not any(not row['not_used'] for row in rows):
        errors.append('チューニング対象 (NOT USED でない) パラメータが1つもありません')

    return rows, errors


def rows_to_parameters(rows):

    ## Test.spsa['parameters'] に格納する GUI 表示用の構造。current 値は
    ## ワーカーからの報告 (state.params) で更新されていく

    parameters = {}
    for index, row in enumerate(rows):
        parameters[row['name']] = {
            'index'    : index,
            'float'    : row['kind'] == 'float',
            'start'    : row['value'],
            'value'    : row['value'],
            'min'      : row['min'],
            'max'      : row['max'],
            'c_end'    : row['c_end'],
            'r_end'    : row['r_end'],
            'not_used' : row['not_used'],
        }
    return parameters


def parse_state_params_text(text):

    ## ワーカーが報告してくる state.params / final.params (rshogi が書き出す
    ## 7 カラム形式) から { name : value } を取り出す。壊れた行は無視する

    values = {}
    rows, _ = parse_params_text(text)
    for row in rows:
        values[row['name']] = row['value']
    return values


def normalize_params_text(text):

    ## フォーム入力を保存用に整える: 改行コード統一 + 末尾空白除去。
    ## 内容には手を付けない (ワーカーはこのテキストをそのまま
    ## canonical.params としてディスクに書く)

    lines = [line.rstrip() for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    while lines and not lines[-1]:
        lines.pop()
    return '\n'.join(lines) + '\n' if lines else ''
