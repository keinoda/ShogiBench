#!/usr/bin/env python3
# ShogiBench を介さず、インスタンス上で単体実行する SPSA チューナー。
# 更新則は OpenBench/Fishtest と同一 (alpha/gamma/A_ratio, C_end/R_end)。
#
# 使い方:
#   1. このファイルを ~/shogibench-worker/Client/ に置く (Scripts/ からコピー)
#   2. 下の CONFIG を編集 (エンジン・評価関数パス・調整パラメータ)
#   3. nohup python3 spsa_standalone.py > spsa.out 2>&1 &
#
# 状態は spsa_state.json に毎イテレーション保存され、中断しても再開できます。
#   python3 spsa_standalone.py --status   で現在値を表示します。
#
# 前提: パラメータはエンジンの USI option として setoption 可能であること。
# ワーカーと同居させる場合はコアを取り合うので、concurrency を控えめに。

import json
import math
import os
import random
import re
import subprocess
import sys

CONFIG = {

    # 対局ランナーとエンジン (Client ディレクトリからの相対パス or 絶対パス)
    'shogitest'   : './shogitest-ob',
    'engine'      : 'Engines/YaneuraOu-nagisa-XXXXXXXX-XXXXXXXX-XXXXXXXX',
    'book'        : 'Books/taya36_shogi_sfen.epd',

    # 対局条件。固定ノードなら 'nodes=25000'
    'tc'          : 'tc=10+0.1 timemargin=250',
    'concurrency' : 8,
    'pairs_per'   : 8,     # 1イテレーションの対局ペア数 (合計 2*pairs_per 局)

    # 両側に共通で渡す USI オプション (パスは絶対推奨、値に空白不可)
    'base_options' : {
        'Threads' : 1,
        'Hash'    : 256,
        'EvalDir' : '/root/shogibench-worker/Client/Networks/XXXXXXXX-dir',
        # 'LS_BUCKET_MODE'    : 'progress8kpabs',
        # 'LS_PROGRESS_COEFF' : '/root/shogibench-worker/Client/Networks/XXXXXXXX-dir/progress.bin',
    },

    # SPSA ハイパーパラメータ (OpenBench/Fishtest と同じ意味・既定値)
    'iterations' : 1000,
    'alpha'      : 0.602,
    'gamma'      : 0.101,
    'A_ratio'    : 0.1,

    # 調整対象: (名前, 'int'|'float', 初期値, 最小, 最大, C_end, R_end)
    #   C_end: 最終イテレーションでの摂動幅 (可動域の 1/20〜1/10 が目安)
    #   R_end: 最終学習率 (Fishtest 標準 0.002)
    'parameters' : [
        ('SlowMover', 'int', 100, 50, 200, 10, 0.002),
    ],

    # tune.py の .params ファイルから読む場合はパスを指定 ('parameters' より優先)。
    # 「名前, 型, 現在値, min, max, C_end(step), R_end(delta)」形式。
    # 現在値が初期値になり、イテレーションごとに同じファイルへ書き戻されるので、
    # チューニング後はそのまま `python3 tune.py apply ...` で焼き戻せます
    'parameters_file' : '',

    'state_file' : 'spsa_state.json',
    'log_file'   : 'spsa_log.csv',
}

# .params ファイルのコメントと [[NOT USED]] 行はそのまま保存する
PARAM_COMMENTS   = {}
PARAM_UNUSED     = []

def load_parameters_file():

    path = CONFIG.get('parameters_file')
    if not path:
        return

    parameters = []
    for raw in open(path, encoding='utf-8'):
        raw = raw.rstrip('\n')
        if not raw.strip():
            continue
        if '[[NOT USED]]' in raw:
            PARAM_UNUSED.append(raw)
            continue
        body    = raw.split('//')[0]
        comment = raw[len(body):]
        f = [x.strip() for x in body.split(',')]
        parameters.append((f[0], f[1], float(f[2]), float(f[3]),
                           float(f[4]), float(f[5]), float(f[6])))
        if comment:
            PARAM_COMMENTS[f[0]] = comment

    CONFIG['parameters'] = parameters
    print ('%s から %d パラメータを読み込みました (+%d 未使用)'
           % (path, len(parameters), len(PARAM_UNUSED)))

def save_parameters_file(state):

    path = CONFIG.get('parameters_file')
    if not path:
        return

    with open(path, 'w', encoding='utf-8') as fout:
        for name, dtype, start, vmin, vmax, c_end, r_end in CONFIG['parameters']:
            fout.write('%s, %s, %s, %s, %s, %s, %s%s\n' % (
                name, dtype, state['values'][name], vmin, vmax,
                c_end, r_end, PARAM_COMMENTS.get(name, '')))
        for raw in PARAM_UNUSED:
            fout.write(raw + '\n')

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def load_state():
    if os.path.exists(CONFIG['state_file']):
        with open(CONFIG['state_file']) as fin:
            return json.load(fin)
    return {
        'iteration' : 0,
        'values'    : { name : float(start) for name, _, start, *_ in CONFIG['parameters'] },
    }

def save_state(state):
    with open(CONFIG['state_file'], 'w') as fout:
        json.dump(state, fout, indent=2)

def schedule(k):

    # Fishtest/OpenBench と同一: C_k = C_end*iters^g / k^g,
    # a_k = R_end*C_end^2*(A+iters)^a / (A+k)^a, r_k = a_k / c_k^2
    iters = CONFIG['iterations']
    A     = CONFIG['A_ratio'] * iters
    per   = {}

    for name, dtype, start, vmin, vmax, c_end, r_end in CONFIG['parameters']:
        c_k = c_end * iters ** CONFIG['gamma'] / k ** CONFIG['gamma']
        if dtype == 'int':
            c_k = max(c_k, 0.5)
        a_k = r_end * c_end ** 2 * (A + iters) ** CONFIG['alpha'] / (A + k) ** CONFIG['alpha']
        per[name] = { 'c' : c_k, 'r' : a_k / c_k ** 2 }

    return per

def perturb(state, per):

    # 各パラメータを ±c_k 振った dev/base の実対局値を作る
    played = {}
    for name, dtype, start, vmin, vmax, c_end, r_end in CONFIG['parameters']:
        flip = 1 if random.getrandbits(1) else -1
        dev  = state['values'][name] + flip * per[name]['c']
        base = state['values'][name] - flip * per[name]['c']

        if dtype == 'int':  # 確率的丸め (OpenBench と同じ)
            r    = random.uniform(0, 1)
            dev  = math.floor(dev  + r)
            base = math.floor(base + r)

        dev  = max(vmin, min(vmax, dev ))
        base = max(vmin, min(vmax, base))

        if dtype == 'int':
            dev, base = int(dev), int(base)

        played[name] = { 'dev' : dev, 'base' : base, 'flip' : flip }

    return played

def engine_spec(label, extra_options):
    directory, binary = os.path.split(CONFIG['engine'])
    options = dict(CONFIG['base_options']); options.update(extra_options)
    opts    = ' '.join('option.%s=%s' % (k, v) for k, v in options.items())
    return '-engine dir=%s/ cmd=./%s proto=usi %s %s name=%s' % (
        directory, binary, CONFIG['tc'], opts, label)

def run_iteration(k, played):

    dev_opts  = { name : p['dev' ] for name, p in played.items() }
    base_opts = { name : p['base'] for name, p in played.items() }

    command = ' '.join([
        CONFIG['shogitest'], '-repeat', '-recover', '-variant standard', '-testEnv',
        '-concurrency %d' % (CONFIG['concurrency']),
        '-games %d'       % (CONFIG['pairs_per']),
        '-resign movecount=3 score=2000',
        '-draw movenumber=40 movecount=8 score=10',
        engine_spec('dev',  dev_opts),
        engine_spec('base', base_opts),
        '-openings file=%s format=epd order=random start=1' % (CONFIG['book']),
        '-srand %d' % (k),
    ])

    output = subprocess.run(
        command, shell=True, capture_output=True, text=True, timeout=24*3600).stdout

    # フォーク版 shogitest の集計行 (先頭エンジン = dev 視点)
    match = re.search(r'Games:\s*(\d+),\s*Wins:\s*(\d+),\s*Draws:\s*(\d+),\s*Losses:\s*(\d+)', output)
    if not match:
        print (output)
        raise SystemExit('shogitest の集計行を読み取れませんでした (上に全出力)')

    games, wins, draws, losses = map(int, match.groups())
    return games, wins, draws, losses

def main():

    load_parameters_file()
    state = load_state()

    # .params 側に存在するのに state に無いパラメータを補完 (途中追加にも耐える)
    for name, dtype, start, *_ in CONFIG['parameters']:
        state['values'].setdefault(name, float(start))

    if '--status' in sys.argv:
        print (json.dumps(state, indent=2)); return

    if not os.path.exists(CONFIG['log_file']):
        with open(CONFIG['log_file'], 'w') as fout:
            fout.write('iteration,games,wins,losses,draws,%s\n'
                       % (','.join(name for name, *_ in CONFIG['parameters'])))

    while state['iteration'] < CONFIG['iterations']:

        k      = state['iteration'] + 1
        per    = schedule(k)
        played = perturb(state, per)

        games, wins, draws, losses = run_iteration(k, played)
        result = wins - losses   # dev 視点のトリノミアル差 (OpenBench と同一)

        # theta += r_k * c_k * (W-L) * flip, [min,max] に丸め
        for name, dtype, start, vmin, vmax, c_end, r_end in CONFIG['parameters']:
            delta = per[name]['r'] * per[name]['c'] * result * played[name]['flip']
            state['values'][name] = max(vmin, min(vmax, state['values'][name] + delta))

        state['iteration'] = k
        save_state(state)
        save_parameters_file(state)

        summary = '  '.join('%s=%.3f' % (n, v) for n, v in state['values'].items())
        print ('[%4d/%d] W:%d L:%d D:%d  ->  %s' % (
            k, CONFIG['iterations'], wins, losses, draws, summary), flush=True)

        with open(CONFIG['log_file'], 'a') as fout:
            fout.write('%d,%d,%d,%d,%d,%s\n' % (k, games, wins, losses, draws,
                       ','.join('%.4f' % (state['values'][n]) for n, *_ in CONFIG['parameters'])))

    print ('\n完了。最終値:')
    for name, dtype, start, vmin, vmax, c_end, r_end in CONFIG['parameters']:
        value = state['values'][name]
        print ('  %s = %s' % (name, round(value) if dtype == 'int' else '%.4f' % (value)))

if __name__ == '__main__':
    main()
