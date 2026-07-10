# .tune ファイル (YaneuraOu の探索パラメータを TUNE マクロ化するパッチ定義) の
# 解析・照合・追随ロジック。
#
# .tune の形式 (Scripts/tune/tune.py と同じ):
#   #set file <path>              対象ソース (以後のブロックに引き継がれる)
#   #set declaration <marker>     宣言の挿入位置マーカー
#   #set options <marker>         TUNE() の挿入位置マーカー
#   #context <prefix> [// comment]
#   <C++断片。チューニング対象の数値に 123@ / 123@suffix マーカーを付ける>
#   #add [<marker>]
#   <marker 行の直後に挿入する断片 (無名なら context の置換内容)>
#
# パラメータ名は {prefix}_{suffix} (suffix 省略時はブロック内の連番 1,2,...)。
#
# ここには「ソースへ実際にパッチを当てる」処理は無い (それは tune.py の仕事)。
# このモジュールが担うのは、バージョン (ブランチ) が進んだときの:
#   check_contexts : 各 context を現行ソースと照合し EXACT / NUMDRIFT / MISSING に分類
#   retune         : NUMDRIFT の context 本文を現行ソースの実テキストへ自動追随
#   params_from_tune : .tune から .params の行を生成・同期
# で、keinoda/fuuppi-spsa の check_contexts.py / retune.py を
# ファイル単位対応・純関数化して移植したもの。サーバ (GUI) とワーカーで共用する。

import re

NUM = r'\d+(?:\.\d+)?'

EXACT    = 'EXACT'
NUMDRIFT = 'NUMDRIFT'
MISSING  = 'MISSING'


## .tune の走査

def iter_context_blocks(tune_text):

    ## (name, file, body, header_line_index, body_line_range) の列。
    ## body は #context 行の次から、次の #... 行の手前まで

    lines    = tune_text.split('\n')
    cur_file = None
    i, n     = 0, len(lines)

    while i < n:
        line = lines[i]

        if line.startswith('#set'):
            parts = line.split('//', 1)[0].split()
            if len(parts) >= 3 and parts[1] == 'file':
                cur_file = parts[2].replace('\\', '/')
            i += 1
            continue

        if not line.startswith('#context'):
            i += 1
            continue

        name  = line[len('#context'):].split('//', 1)[0].strip()
        start = i + 1
        i += 1
        while i < n and not lines[i].startswith(('#context', '#add', '#set')):
            i += 1

        yield {
            'name'  : name,
            'file'  : cur_file,
            'body'  : '\n'.join(lines[start:i]),
            'range' : (start, i), # 行番号 [start, i) が本文
        }

def tune_files(tune_text):
    ## .tune が参照するソースファイル (相対パス、'/' 区切り) の一覧
    files = []
    for block in iter_context_blocks(tune_text):
        if block['file'] and block['file'] not in files:
            files.append(block['file'])
    return files

def iter_markers(tune_text):

    ## .tune が挿入位置として要求するマーカー (%%TUNE_DECLARATION%% 等) と、
    ## それが存在すべきファイルの組を列挙する。
    ## 由来: '#set declaration <M>' / '#set options <M>' / '#add <M>'

    seen     = set()
    cur_file = None

    for line in tune_text.split('\n'):

        if not line.startswith('#'):
            continue

        parts = line.split('//', 1)[0].split()
        if not parts:
            continue

        marker = None
        if parts[0] == '#set' and len(parts) >= 3:
            if parts[1] == 'file':
                cur_file = parts[2].replace('\\', '/')
            elif parts[1] in ('declaration', 'options'):
                marker = parts[2]
        elif parts[0] == '#add' and len(parts) >= 2:
            marker = parts[1]

        if marker and (marker, cur_file) not in seen:
            seen.add((marker, cur_file))
            yield { 'marker' : marker, 'file' : cur_file }

def check_markers(tune_text, sources):

    ## マーカーが対象ソースに存在するかの確認。TUNE マクロ用マーカーの無い
    ## ブランチ (例: 上流の素の YaneuraOu) を照合時に検出する

    results = []
    for entry in iter_markers(tune_text):

        item = { 'name' : 'marker %s' % (entry['marker']), 'file' : entry['file'],
                 'status' : MISSING, 'detail' : '', 'body' : '' }
        results.append(item)

        if entry['file'] not in sources:
            item['detail'] = 'ソースファイルがありません: %s' % (entry['file'])
        elif entry['marker'] in sources[entry['file']]:
            item['status'] = EXACT
        else:
            item['detail'] = 'マーカーがソースにありません (TUNE対応ブランチか確認してください)'

    return results


## 照合パターンの構築 (tune.py の replace_context と同じ空白無視マッチ)

def strip_markers(body):
    ## '123@suffix' の '@suffix' を除去して、ソースと照合するテキストにする
    return re.sub(r'@[A-Za-z0-9]*', '', body)

def exact_pattern(body_no_markers):
    ## 空白を全て除去し、文字ごとに \s* を挟んだパターン (tune.py と同一の照合)
    flat = re.sub(r'\s+', '', body_no_markers)
    if not flat:
        return None
    return r'\s*'.join(map(re.escape, flat))

def numeric_wildcard_pattern(body_no_markers):
    ## 数値リテラルをワイルドカード化した number-agnostic パターン
    flat = re.sub(r'\s+', '', body_no_markers)
    if not flat:
        return None
    parts = []
    for token in re.split(r'(%s)' % (NUM), flat):
        if re.fullmatch(NUM, token or ''):
            parts.append(NUM)
        elif token:
            parts.append(r'\s*'.join(map(re.escape, token)))
    return r'\s*'.join(parts)


## 照合 (check_contexts.py 相当)

def check_contexts(tune_text, sources):

    ## sources: { '#set file のパス' : ソース全文 }。
    ## 各 context を EXACT / NUMDRIFT / MISSING に分類して返す。
    ## NUMDRIFT の detail は '旧値->新値' の一覧

    results = []
    for block in iter_context_blocks(tune_text):

        entry = { 'name' : block['name'], 'file' : block['file'],
                  'status' : MISSING, 'detail' : '', 'body' : block['body'] }
        results.append(entry)

        if block['file'] not in sources:
            entry['detail'] = 'ソースファイルがありません: %s' % (block['file'])
            continue

        text  = sources[block['file']]
        clean = strip_markers(block['body'])

        pattern = exact_pattern(clean)
        if pattern is None:
            entry['status'], entry['detail'] = EXACT, '(空のcontext)'
            continue

        if len(re.findall(pattern, text, flags=re.MULTILINE | re.DOTALL)) == 1:
            entry['status'] = EXACT
            continue

        pattern2 = numeric_wildcard_pattern(clean)
        hits     = re.findall(pattern2, text, flags=re.MULTILINE | re.DOTALL)

        if len(hits) == 1:
            flat  = re.sub(r'\s+', '', clean)
            want  = re.findall(NUM, flat)
            got   = re.findall(NUM, re.sub(r'\s+', '', hits[0]))
            diffs = ['%s->%s' % (w, g) for w, g in zip(want, got) if w != g]
            entry['status'] = NUMDRIFT
            entry['detail'] = ', '.join(diffs) or '(数値一致・空白差のみ)'
        elif len(hits) > 1:
            entry['detail'] = '構造一致が %d 箇所あり一意に特定できません' % (len(hits))
        else:
            entry['detail'] = '構造が変わっています (現ソースに見つかりません)'

    return results

def summarize_check(results):
    counts = { EXACT : 0, NUMDRIFT : 0, MISSING : 0 }
    for entry in results:
        counts[entry['status']] += 1
    return counts


## 自動追随 (retune.py 相当、ファイル単位対応)

def analyze_body(body):

    ## @ 付き本文から (マーカー除去テキスト, {数値リテラル序数: suffix}) を得る。
    ## suffix '' はブロック内連番 (tune.py の省略記法)

    marks = {}
    out   = []
    pos   = 0
    idx   = 0
    for match in re.finditer(r'(%s)(@[A-Za-z0-9]*)?' % (NUM), body):
        out.append(body[pos:match.start()])
        out.append(match.group(1))
        pos = match.end()
        if match.group(2):
            marks[idx] = match.group(2)[1:]
        idx += 1
    out.append(body[pos:])
    return ''.join(out), marks

def remark(raw, marks):
    ## 現ソースの生テキストの数値リテラル序数 idx に @suffix を付け直す
    idx = [-1]
    def sub(match):
        idx[0] += 1
        if idx[0] in marks:
            return match.group(0) + '@' + marks[idx[0]]
        return match.group(0)
    return re.sub(NUM, sub, raw)

def retune(tune_text, sources):

    ## NUMDRIFT の context 本文を現行ソースの実テキストで置き換え、@ マーカーを
    ## 同じ序数の数値に付け直す。特定できないブロックは無変更で manual に列挙。
    ## 返り値: (新しい tune_text, {'exact': [...], 'auto': [...], 'manual': [(name, 理由)]})

    lines  = tune_text.split('\n')
    report = { 'exact' : [], 'auto' : [], 'manual' : [] }

    # 後ろから置換して行番号のずれを避ける
    for block in reversed(list(iter_context_blocks(tune_text))):

        name = block['name']

        if block['file'] not in sources:
            report['manual'].append((name, 'ソースファイルがありません: %s' % (block['file'])))
            continue

        text         = sources[block['file']]
        clean, marks = analyze_body(block['body'])

        pattern = exact_pattern(clean)
        if pattern is None:
            report['exact'].append(name)
            continue

        if len(re.findall(pattern, text, flags=re.MULTILINE | re.DOTALL)) == 1:
            report['exact'].append(name)
            continue

        pattern2 = numeric_wildcard_pattern(clean)
        hits     = list(re.finditer(pattern2, text, flags=re.MULTILINE | re.DOTALL))

        if len(hits) != 1:
            report['manual'].append((name, '%d 箇所一致' % (len(hits))))
            continue

        start, end = block['range']
        lines[start:end] = [''] + remark(hits[0].group(0), marks).split('\n') + ['']
        report['auto'].append(name)

    for key in ('exact', 'auto'):
        report[key].reverse()
    report['manual'].reverse()

    return '\n'.join(lines), report


## .params の生成・同期 (tune.py の check_params 相当)

def tune_param_defaults(value):

    ## tune.py と同じ既定レンジ: 0..2x (負値は 2x..0、0 は 0..1)、
    ## step は可動域の 1/20 (最低 1)、delta は 0.002 固定

    if value > 0:
        vmin, vmax = 0.0, value * 2
    elif value == 0:
        vmin, vmax = 0.0, 1.0
    else:
        vmin, vmax = value * 2, 0.0

    step = max((vmax - vmin) / 20, 1)
    return vmin, vmax, step, 0.0020

def tune_param_names(tune_text):

    ## context ブロックに現れる順で (パラメータ名, 元の数値) を列挙する。
    ## 名前は {prefix}_{suffix} (suffix 省略時はブロック内連番)

    named = []
    for block in iter_context_blocks(tune_text):

        prefix = block['name'].split()[0] if block['name'] else ''
        if not prefix:
            continue # 無名 context は置換専用でパラメータを持たない

        ordinal = 0
        for match in re.finditer(r'(-?%s)@([A-Za-z0-9]*)' % (NUM), block['body']):
            ordinal += 1
            suffix = match.group(2) or str(ordinal)
            named.append(('%s_%s' % (prefix, suffix), float(match.group(1))))

    return named

def format_param_value(value, kind):
    if kind == 'int' and float(value) == int(float(value)):
        return str(int(float(value)))
    return ('%f' % (float(value))).rstrip('0').rstrip('.')

def parse_params_entries(params_text):

    ## .params テキストを行の順で辞書の列に読む (コメント・NOT USED 保持)

    entries = []
    for raw in (params_text or '').split('\n'):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        body, _, comment = line.partition('//')
        not_used = '[[NOT USED]]' in body
        body     = body.replace('[[NOT USED]]', '')
        fields   = [x.strip() for x in body.split(',')]
        if len(fields) < 7:
            continue
        try:
            entries.append({
                'name'     : fields[0],
                'type'     : fields[1],
                'value'    : float(fields[2]),
                'min'      : float(fields[3]),
                'max'      : float(fields[4]),
                'step'     : float(fields[5]),
                'delta'    : float(fields[6]),
                'comment'  : comment.strip(),
                'not_used' : not_used,
            })
        except ValueError:
            continue
    return entries

def write_params_entries(entries):

    lines = []
    for e in entries:
        line = '%s, %s, %s, %s, %s, %s, %s' % (
            e['name'], e['type'],
            format_param_value(e['value'], e['type']),
            format_param_value(e['min'],   e['type']),
            format_param_value(e['max'],   e['type']),
            format_param_value(e['step'],  'float'),
            format_param_value(e['delta'], 'float'))
        if e.get('not_used'):
            line += ' [[NOT USED]]'
        if e.get('comment'):
            line += ' // %s' % (e['comment'])
        lines.append(line)
    return '\n'.join(lines) + '\n' if lines else ''

def params_from_tune(tune_text, existing_params_text=''):

    ## .tune のパラメータ集合に .params を同期する。
    ##   - 既存行は 値/レンジ/step/delta/コメント を保持 (継続チューニングを壊さない)
    ##   - .tune から消えた行は [[NOT USED]] を付けて残す (rshogi 側と同じ流儀)
    ##   - 新しいパラメータは tune.py と同じ既定値で追加 (初期値は context の数値)
    ## 返り値: (params_text, {'added': [...], 'retired': [...], 'kept': N})

    existing = { e['name'] : e for e in parse_params_entries(existing_params_text) }
    names    = tune_param_names(tune_text)
    in_tune  = { name for name, value in names }

    report  = { 'added' : [], 'retired' : [], 'kept' : 0 }
    entries = []

    for name, value in names:

        if name in existing:
            entry = existing.pop(name)
            if entry['not_used']:
                report['added'].append(name) # 復活
                entry['not_used'] = False
            else:
                report['kept'] += 1
            entries.append(entry)
            continue

        vmin, vmax, step, delta = tune_param_defaults(value)
        kind = 'int' if value == int(value) else 'float'
        entries.append({
            'name' : name, 'type' : kind, 'value' : value,
            'min' : vmin, 'max' : vmax, 'step' : step, 'delta' : delta,
            'comment' : '', 'not_used' : False,
        })
        report['added'].append(name)

    # .tune に現れなくなったパラメータは NOT USED として残す
    for name, entry in existing.items():
        if not entry['not_used']:
            entry['not_used'] = True
            report['retired'].append(name)
        entries.append(entry)

    return write_params_entries(entries), report
