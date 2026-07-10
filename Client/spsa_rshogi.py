# SPSA (rshogi ラッパー)。
#
# ShogiBench の SPSA ワークロードは、1 台のワーカーが rshogi の `spsa` チューナー
# (https://github.com/keinoda/rshogi crates/tools/src/bin/spsa.rs) を丸ごと実行する
# 形で動く。ワーカーの役割は:
#
#   1. rshogi の spsa バイナリを用意する (サーバ指定の repo/ref から cargo ビルド)
#   2. サーバから受け取った .params 原文を canonical として run dir を組み立てる
#   3. spsa を起動し、run dir の meta.json / stats.csv / state.params を監視して
#      進捗をサーバへ報告する (サーバが stop を返したら安全に停止)
#   4. 完走したら final.params をサーバへ送る
#
# 状態は全て Client/SPSA/<test_id>/ 以下に残るので、ワーカーが再起動しても
# --resume --force-unlock で続きから再開できる。ワーカーの突然死で spsa が
# 生き残っていた場合は、同じ run dir のプロセスを見つけて監視だけ再開する。

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile

import requests

## Local imports must only use "import x", never "from x import ..."

import utils

BINARY_NAME     = 'spsa-ob'
MAPPING_NAME    = 'yo_rshogi_mapping.toml'
SPSA_DIR        = 'SPSA'
REPORT_INTERVAL = 30   # 進捗報告の間隔 (秒)
POLL_INTERVAL   = 5    # プロセス/停止指示の確認間隔 (秒)
STOP_GRACE      = 15   # SIGTERM から SIGKILL までの猶予 (秒)


## rshogi の spsa バイナリの用意

def ensure_spsa_binary(config):

    ## サーバが指定する repo/ref から spsa をビルドして Client 直下に
    ## spsa-ob として置く。ビルド済みの repo/ref が変わらない限り再利用する。
    ## 名前マッピング表 (tune/yo_rshogi_mapping.toml) も一緒にステージする

    target  = utils.url_join(config.server, 'clientMatchRunnerVersionRef')
    payload = { 'username' : config.username, 'password' : config.password }
    data    = requests.post(target, data=payload, timeout=30).json()

    if 'error' in data:
        raise utils.OpenBenchFatalWorkerException('Server error: %s' % (data['error']))

    repo_url = data['rshogi_repo_url']
    repo_ref = data['rshogi_repo_ref']

    binary  = os.path.abspath(BINARY_NAME)
    mapping = os.path.abspath(MAPPING_NAME)
    marker  = binary + '.source'
    wanted  = '%s %s' % (repo_url, repo_ref)

    if os.path.isfile(binary) and os.path.isfile(marker):
        with open(marker) as fin:
            if fin.read().strip() == wanted:
                return binary, mapping if os.path.isfile(mapping) else None

    print ('\nBuilding rshogi spsa from %s (%s)...' % (repo_url, repo_ref))
    print ('> This is a one-time Rust build, and may take a while')

    response = requests.get(utils.url_join(repo_url, 'archive', '%s.zip' % (repo_ref)), timeout=300)
    response.raise_for_status()

    with tempfile.TemporaryDirectory() as temp_dir:

        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            tmp_file.write(response.content)
            temp_zip_path = tmp_file.name

        with zipfile.ZipFile(temp_zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
        os.remove(temp_zip_path)

        repo_dir = os.path.join(temp_dir, os.listdir(temp_dir)[0])

        process = subprocess.Popen(
            ['cargo', 'build', '--release', '-p', 'tools', '--bin', 'spsa'],
            cwd=repo_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        for line in iter(process.stdout.readline, b''):
            text = line.decode('utf-8', 'replace').rstrip()
            if text:
                print ('> %s' % (text))
        process.wait()

        if process.returncode != 0:
            raise utils.OpenBenchBuildFailedException(
                'Failed to build rshogi spsa', 'cargo build exited with %d' % (process.returncode))

        built = os.path.join(repo_dir, 'target', 'release', 'spsa')
        shutil.copyfile(built, binary)
        os.chmod(binary, 0o755)

        # YO 名 ⇔ rshogi 名のマッピング表 (mapping=YO のワークロードで使う)
        table = os.path.join(repo_dir, 'tune', MAPPING_NAME)
        if os.path.isfile(table):
            shutil.copyfile(table, mapping)

    with open(marker, 'w') as fout:
        fout.write(wanted)

    print ('> Finished building rshogi spsa\n')
    return binary, mapping if os.path.isfile(mapping) else None


## コマンドライン組み立て (純関数: UnitTests から直接テストできる)

def time_control_flags(time_control, scale_factor):

    ## OpenBench 形式の持ち時間文字列を rshogi spsa のフラグへ変換する。
    ##   'N=25000' -> --nodes 25000        (スケールしない)
    ##   'MT=1000' -> --byoyomi 1000*f     (秒読み ms)
    ##   '2.0+0.02' -> --btime 2000*f --binc 20*f (フィッシャー)

    if (match := re.match(r'^N=(\d+)$', time_control)):
        return ['--nodes', match.group(1)]

    if (match := re.match(r'^MT=(\d+)$', time_control)):
        byoyomi = max(100, round(int(match.group(1)) * scale_factor))
        return ['--byoyomi', str(byoyomi)]

    if (match := re.match(r'^(\d+(?:\.\d+)?)\+(\d+(?:\.\d+)?)$', time_control)):
        btime = max(100, round(float(match.group(1)) * 1000 * scale_factor))
        binc  = max(0,   round(float(match.group(2)) * 1000 * scale_factor))
        return ['--btime', str(btime), '--binc', str(binc)]

    raise ValueError('SPSA では扱えない持ち時間形式です: %s' % (time_control))

def build_spsa_command(binary, run_dir, engine_path, spsa, tc_flags, usi_options,
                       threads, hash_mb, book_path, mode_flags, total_pairs, mapping_path):

    cmd = [
        binary,
        '--run-dir'    , run_dir,
        '--engine-path', engine_path,
        '--total-pairs', str(total_pairs),
        '--batch-pairs', str(spsa['batch_pairs']),
        '--concurrency', str(spsa['concurrency']),
        '--threads'    , str(threads),
        '--hash-mb'    , str(hash_mb),
        '--alpha'      , str(spsa['alpha']),
        '--gamma'      , str(spsa['gamma']),
        '--a-ratio'    , str(spsa['a_ratio']),
    ]

    cmd += tc_flags
    cmd += ['--startpos-file', book_path, '--require-startpos-file']

    if spsa.get('seed') is not None:
        cmd += ['--seed', str(spsa['seed'])]

    if spsa.get('active_regex'):
        cmd += ['--active-only-regex', spsa['active_regex']]

    if spsa.get('mapping') == 'YO':
        if not mapping_path:
            raise utils.OpenBenchFatalWorkerException(
                'yo_rshogi_mapping.toml is missing; rebuild the rshogi spsa binary')
        cmd += ['--engine-param-mapping', mapping_path]

    early = spsa.get('early_stop') or {}
    if early.get('patience'):
        cmd += ['--early-stop-patience'                 , str(early['patience'])]
        cmd += ['--early-stop-avg-abs-update-threshold' , str(early['avg_abs_update'])]
        cmd += ['--early-stop-result-variance-threshold', str(early['result_variance'])]

    for name, value in usi_options:
        cmd += ['--usi-option', '%s=%s' % (name, value)]

    cmd += mode_flags
    return cmd


## run dir の進捗読み取り (純関数)

def read_stats_totals(stats_csv):

    ## stats.csv (ヘッダ: iteration,batch_pairs,plus_wins,minus_wins,draws,...) から
    ## 累計の (バッチ数, W, L, D) を取り出す。plus 側視点

    batches = wins = losses = draws = 0

    if not os.path.isfile(stats_csv):
        return batches, wins, losses, draws

    with open(stats_csv) as fin:
        header = fin.readline().strip().split(',')
        try:
            iw = header.index('plus_wins')
            il = header.index('minus_wins')
            id_ = header.index('draws')
        except ValueError:
            return batches, wins, losses, draws

        for line in fin:
            fields = line.strip().split(',')
            if len(fields) <= max(iw, il, id_):
                continue
            try:
                wins    += int(fields[iw])
                losses  += int(fields[il])
                draws   += int(fields[id_])
                batches += 1
            except ValueError:
                continue

    return batches, wins, losses, draws

def read_run_progress(run_dir):

    ## meta.json / stats.csv / state.params から進捗のスナップショットを作る。
    ## まだ 1 バッチも完了していなければ meta は無い (ゼロ進捗を返す)

    progress = {
        'completed_pairs'     : 0,
        'completed_batches'   : 0,
        'total_games'         : 0,
        'wins'                : 0,
        'losses'              : 0,
        'draws'               : 0,
        'last_raw_result'     : 0.0,
        'last_avg_abs_update' : 0.0,
        'state_params'        : '',
        'final_params'        : '',
    }

    meta_path = os.path.join(run_dir, 'meta.json')
    if os.path.isfile(meta_path):
        try:
            with open(meta_path) as fin:
                meta = json.load(fin)
            progress['completed_pairs'    ] = int(meta.get('completed_pairs', 0))
            progress['completed_batches'  ] = int(meta.get('completed_iterations', 0))
            progress['total_games'        ] = int(meta.get('total_games', 0))
            progress['last_raw_result'    ] = float(meta.get('last_raw_result_mean', 0.0))
            progress['last_avg_abs_update'] = float(meta.get('last_avg_abs_update', 0.0))
        except (ValueError, OSError):
            pass # 書き込み途中の meta はスキップし、次の報告で拾う

    _, wins, losses, draws = read_stats_totals(os.path.join(run_dir, 'stats.csv'))
    progress['wins'], progress['losses'], progress['draws'] = wins, losses, draws

    state_path = os.path.join(run_dir, 'state.params')
    if os.path.isfile(state_path):
        with open(state_path) as fin:
            progress['state_params'] = fin.read()

    final_path = os.path.join(run_dir, 'final.params')
    if os.path.isfile(final_path):
        with open(final_path) as fin:
            progress['final_params'] = fin.read()

    return progress


## プロセス管理

def find_running_spsa(run_dir):

    ## この run dir を使っている spsa プロセスの PID (無ければ None)。
    ## ワーカーの突然死で残った detached プロセスへの再接続に使う

    try:
        # '--' が無いと pgrep がパターン先頭の '--run-dir' をオプションと誤認する
        output = subprocess.run(
            ['pgrep', '-f', '--', '--run-dir %s' % (run_dir)],
            capture_output=True, text=True).stdout.strip()
        return int(output.split('\n')[0]) if output else None
    except (ValueError, OSError):
        return None

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def stop_spsa_process(pid, engine_binary):

    ## spsa と、そのプロセスグループ (= 起動中のエンジン) を止める。
    ## SIGTERM では rshogi は .lock を残すので、再開時は --force-unlock を使う

    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None

    for sig in [signal.SIGTERM, signal.SIGKILL]:

        try:
            if pgid is not None and pgid != os.getpgid(0):
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)
        except OSError:
            break # 既に終了している

        for x in range(STOP_GRACE * 2):
            if not pid_alive(pid):
                break
            time.sleep(0.5)

        if not pid_alive(pid):
            break

    # 取り残されたエンジンを名前で掃除 (eval のファイルロックを塞がないように)
    if engine_binary:
        utils.kill_process_by_name(engine_binary)


## 起動モードの決定

def load_offsets(base_dir):

    path = os.path.join(base_dir, 'offsets.json')
    if os.path.isfile(path):
        try:
            with open(path) as fin:
                return json.load(fin)
        except (ValueError, OSError):
            pass
    return { 'pairs' : 0, 'batches' : 0, 'games' : 0, 'wins' : 0, 'losses' : 0, 'draws' : 0 }

def save_offsets(base_dir, offsets):
    with open(os.path.join(base_dir, 'offsets.json'), 'w') as fout:
        json.dump(offsets, fout, indent=2)

def choose_launch_mode(base_dir, run_dir, canonical, spsa):

    ## 3 通りの起動モードを決める。返り値は (フラグ, total_pairs, offsets)
    ##
    ## 1. resume        : この run dir に meta がある -> 続きから (--resume)
    ## 2. takeover      : 初見だがサーバに進捗がある -> サーバの state.params を
    ##                    起点に、残りペア数で新しいスケジュールを回す
    ## 3. fresh         : 何もない -> canonical から新規開始

    meta_path  = os.path.join(run_dir, 'meta.json')
    state_path = os.path.join(run_dir, 'state.params')

    if os.path.isfile(meta_path) and os.path.isfile(state_path):

        # meta が total_pairs を持っている (スケジュール不一致で bail しないよう
        # 起動時の値をそのまま使う)
        with open(meta_path) as fin:
            meta = json.load(fin)
        total = int(meta.get('total_pairs', spsa['total_pairs']))

        # 電源断や SIGKILL の残骸 .lock は --force-unlock で除去する。
        # 呼び出し側でプロセスの不在は確認済み
        return ['--resume', '--force-unlock'], total, load_offsets(base_dir)

    carry = spsa.get('carry') or {}
    if carry.get('pairs', 0) > 0 and spsa.get('state_params'):

        # 別マシンからの引き継ぎ: rshogi の meta が無いのでスケジュール位置 k は
        # 引き継げない。サーバの最新 state.params を起点に、残りペア数を新しい
        # 地平線として回す (SPSA としては「現在値からの再スタート」)
        os.makedirs(run_dir, exist_ok=True)
        with open(state_path, 'w') as fout:
            fout.write(spsa['state_params'])

        offsets = {
            'pairs'   : carry.get('pairs' , 0),
            'batches' : carry.get('pairs' , 0) // max(1, spsa['batch_pairs']),
            'games'   : carry.get('games' , 0),
            'wins'    : carry.get('wins'  , 0),
            'losses'  : carry.get('losses', 0),
            'draws'   : carry.get('draws' , 0),
        }
        save_offsets(base_dir, offsets)

        remaining = max(1, spsa['total_pairs'] - carry['pairs'])
        return ['--use-existing-state-as-init'], remaining, offsets

    offsets = { 'pairs' : 0, 'batches' : 0, 'games' : 0, 'wins' : 0, 'losses' : 0, 'draws' : 0 }
    save_offsets(base_dir, offsets)
    return ['--init-from', canonical], spsa['total_pairs'], offsets


## メインの実行ループ

def run_workload(config, reporter, engine_path, usi_options, book_path,
                 threads, hash_mb, scale_factor):

    ## worker.py の complete_workload() から SPSA ワークロード受領時に呼ばれる。
    ## reporter は worker.ServerReporter (循環 import を避けるため引数で受ける)

    if sys.platform.startswith('win'):
        raise utils.OpenBenchFatalWorkerException('SPSA (rshogi) は Linux ワーカーのみ対応です')

    test_id = int(config.workload['test']['id'])
    spsa    = config.workload['spsa']

    base_dir  = os.path.abspath(os.path.join(SPSA_DIR, str(test_id)))
    run_dir   = os.path.join(base_dir, 'run')
    canonical = os.path.join(base_dir, 'canonical.params')
    os.makedirs(run_dir, exist_ok=True)

    binary, mapping_path = ensure_spsa_binary(config)

    # canonical .params (サーバ保持の原文) を配置する。resume 時に別内容へ
    # 変わっていても、rshogi は meta の hash 検証で気付ける
    if not os.path.isfile(canonical) or open(canonical).read() != spsa['params_text']:
        with open(canonical, 'w') as fout:
            fout.write(spsa['params_text'])

    engine_binary = os.path.basename(engine_path)

    # ワーカー突然死の生き残りがいれば、殺さずに監視だけ引き継ぐ
    attached_pid = find_running_spsa(run_dir)
    proc         = None

    if attached_pid is not None:
        print ('Re-attaching to running rshogi spsa (pid=%d)' % (attached_pid))
        offsets = load_offsets(base_dir)

    else:
        mode_flags, total_pairs, offsets = choose_launch_mode(base_dir, run_dir, canonical, spsa)

        tc_flags = time_control_flags(config.workload['test']['dev']['time_control'], scale_factor)

        command = build_spsa_command(
            binary, run_dir, engine_path, spsa, tc_flags, usi_options,
            threads, hash_mb, book_path, mode_flags, total_pairs, mapping_path)

        print ('\nLaunching rshogi spsa...\n%s\n' % (' '.join(command)))

        # stdout/stderr は run.log へ追記。プロセスグループを分けておき、
        # 停止時に spsa とエンジンをまとめて止められるようにする
        with open(os.path.join(run_dir, 'run.log'), 'a') as log:
            proc = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)

    monitor_spsa(config, reporter, base_dir, run_dir, proc, attached_pid, offsets, engine_binary)

def spsa_is_running(proc, attached_pid):
    if proc is not None:
        return proc.poll() is None
    return pid_alive(attached_pid)

def spsa_pid(proc, attached_pid):
    return proc.pid if proc is not None else attached_pid

def monitor_spsa(config, reporter, base_dir, run_dir, proc, attached_pid, offsets, engine_binary):

    last_report = 0

    while True:

        running = spsa_is_running(proc, attached_pid)

        # openbench.exit でワーカーごと止める (状態は resume 可能な形で残る)
        if os.path.isfile('openbench.exit'):
            stop_spsa_process(spsa_pid(proc, attached_pid), engine_binary)
            report_progress(config, reporter, run_dir, offsets, finished=False)
            return

        if running and time.time() - last_report < REPORT_INTERVAL:
            time.sleep(POLL_INTERVAL)
            continue

        if running:

            last_report = time.time()

            # 一時的な通信失敗は握りつぶして続行する。それ以外
            # (Bad Client Version / サーバ設定変更など) は上へ投げる。
            # spsa 本体は殺さない: ワーカーが再起動しても再接続できる
            try:
                response = report_progress(config, reporter, run_dir, offsets, finished=False)
            except (requests.exceptions.RequestException, ValueError) as error:
                print ('[Note] Failed to report SPSA progress (%s)' % (error))
                continue

            # サーバからの停止指示 (GUI の停止/削除、Worker Key 失効など)。
            # 状態は残るので、再開されれば続きから走る
            if response.get('stop'):
                print ('Server requested SPSA stop')
                stop_spsa_process(spsa_pid(proc, attached_pid), engine_binary)
                try: report_progress(config, reporter, run_dir, offsets, finished=False)
                except Exception: pass
                return

            continue

        # プロセス終了。final.params があれば完走、なければ異常終了
        if os.path.isfile(os.path.join(run_dir, 'final.params')):
            print ('rshogi spsa finished; uploading final.params')
            for attempt in range(5):
                try:
                    report_progress(config, reporter, run_dir, offsets, finished=True)
                    return
                except (requests.exceptions.RequestException, ValueError) as error:
                    print ('[Note] Failed to upload final.params (%s), retrying' % (error))
                    time.sleep(10)
            # 届けられなくても run dir に final.params は残っている。次の割り当てで
            # resume すると rshogi は即終了し、この報告をやり直せる
            raise utils.OpenBenchFatalWorkerException('Unable to upload SPSA final.params')

        tail = read_log_tail(os.path.join(run_dir, 'run.log'))
        print ('rshogi spsa exited without final.params\n%s' % (tail))
        reporter.report_engine_error(config, 'rshogi spsa exited unexpectedly', tail)

        # クラッシュループを避けるため、このセッション中は再割り当てを受けない
        # (状態は残っているので、ワーカー再起動か別マシンで resume できる)
        config.blacklist.append(config.workload['test']['id'])
        return

def read_log_tail(log_path, lines=40, max_bytes=16384):

    if not os.path.isfile(log_path):
        return '(no run.log)'

    with open(log_path, 'rb') as fin:
        fin.seek(0, os.SEEK_END)
        size = fin.tell()
        fin.seek(max(0, size - max_bytes))
        text = fin.read().decode('utf-8', 'replace')

    return '\n'.join(text.split('\n')[-lines:])

def report_progress(config, reporter, run_dir, offsets, finished):

    progress = read_run_progress(run_dir)

    payload = {
        'test_id'             : config.workload['test'  ]['id'],
        'result_id'           : config.workload['result']['id'],

        'completed_pairs'     : offsets['pairs'  ] + progress['completed_pairs'],
        'completed_batches'   : offsets['batches'] + progress['completed_batches'],
        'total_games'         : offsets['games'  ] + progress['total_games'],
        'wins'                : offsets['wins'   ] + progress['wins'],
        'losses'              : offsets['losses' ] + progress['losses'],
        'draws'               : offsets['draws'  ] + progress['draws'],

        'last_raw_result'     : progress['last_raw_result'],
        'last_avg_abs_update' : progress['last_avg_abs_update'],
        'state_params'        : progress['state_params'],

        'finished'            : '1' if finished else '0',
        'final_params'        : progress['final_params'] if finished else '',
    }

    return reporter.report(config, 'clientSubmitSpsa', payload).json()


## 古い SPSA 状態の掃除 (worker.py の cleanup_client から呼ばれる)

def cleanup_stale_runs(max_age_seconds):

    if not os.path.isdir(SPSA_DIR):
        return

    for name in os.listdir(SPSA_DIR):

        base_dir = os.path.join(SPSA_DIR, name)
        run_dir  = os.path.abspath(os.path.join(base_dir, 'run'))

        if not os.path.isdir(base_dir):
            continue

        # 実行中のランは絶対に消さない
        if find_running_spsa(run_dir) is not None:
            continue

        newest = 0
        for root, dirs, files in os.walk(base_dir):
            for file in files:
                try: newest = max(newest, os.path.getmtime(os.path.join(root, file)))
                except OSError: pass

        if newest and time.time() - newest > max_age_seconds:
            shutil.rmtree(base_dir, ignore_errors=True)
            print ('Cleaned up stale SPSA state: %s' % (base_dir))
