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

import argparse
import cpuinfo
import hashlib
import importlib
import json
import multiprocessing
import os
import platform
import psutil
import queue
import re
import requests
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile

from subprocess import PIPE, Popen, call, STDOUT
from itertools import combinations_with_replacement
from concurrent.futures import ThreadPoolExecutor, wait

## Local imports must only use "import x", never "from x import ..."
## Local imports must also be done in reload_local_imports()

import bench
import genfens
import pgn_util
import spsa_rshogi
import utils

## Local imports from client are an exception

from client import BadVersionException
from client import url_join
from client import try_forever

## Basic configuration of the Client. These timeouts can be changed at will

CLIENT_VERSION   = 65 # Client version to send to the Server
TIMEOUT_HTTP     = 30 # Timeout in seconds for HTTP requests
TIMEOUT_ERROR    = 10 # Timeout in seconds when any errors are thrown
TIMEOUT_WORKLOAD = 30 # Timeout in seconds between workload requests
REPORT_INTERVAL  = 30 # Seconds between reports to the Server

IS_WINDOWS = platform.system() == 'Windows' # Don't touch this
IS_LINUX   = platform.system() != 'Windows' # Don't touch this

# setup_worker.sh のループと取り決めた終了コード。これらで終了したときは
# ラッパーはクライアントを再起動しない
EXIT_DUPLICATE = 65 # 同じディレクトリで別のワーカーが稼働中
EXIT_SHUTDOWN  = 66 # ワーカーキー失効 / openbench.exit による恒久停止

# 単一インスタンスロックの保持用 (プロセス生存中は開きっぱなしにする)
WORKER_LOCK = None

def acquire_single_instance_lock():

    ## 同じ Client ディレクトリで複数のワーカーが同時に走ることを防ぐ。
    ## 接続のリトライで積み上がった古い起動ループが後から動き出しても、
    ## ロックを取れずに即座に (再起動なしで) 終了する

    global WORKER_LOCK

    if IS_WINDOWS:
        return

    import fcntl

    lock = open('.worker.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # 本当に別プロセスがロックを保持しているときだけ「多重起動」と判定する
        lock.close()
        print ('[Error] Another worker is already running from this directory')
        print ('[Error] Exiting to avoid duplicate workers (exit code %d)' % (EXIT_DUPLICATE))
        terminate_legacy_wrapper()
        sys.exit(EXIT_DUPLICATE)
    except OSError as error:
        # flock が使えない環境 (NFS 等)。誤検知の exit 65 でラッパーごと
        # 恒久停止するくらいなら、ロック無しで進める方が安全
        lock.close()
        print ('[Note] Instance lock unavailable (%s); continuing without it' % (error))
        return

    WORKER_LOCK = lock

def stop_detached_spsa():

    ## 恒久停止の前に、detached で走り続けている rshogi spsa を止める。
    ## ワーカー突然死 → 再起動 → 登録前に失効、の経路では monitor による
    ## 再接続まで到達しないため、ここで止めないと孤児が何日も回り続ける

    try:
        import spsa_rshogi
        spsa_rshogi.stop_all_running_spsa()
    except Exception as error:
        print ('[Note] Could not stop detached SPSA runs: %s' % (error))

def legacy_wrapper_pid(ppid, parent_cmdline, wrapper_ack):

    ## 「終了コード契約 (65/66) を知らない旧ラッパーの直下で走っているか」の
    ## 判定。新しい setup_worker.sh は SHOGIBENCH_WRAPPER_ACK=1 を export する
    ## ので対象外。親のコマンドラインが bootstrap のものでなければ (手動起動や
    ## 独自の supervisor)、勝手に殺さない

    if wrapper_ack:
        return None

    markers = ('setup_worker.sh', 'shogibench_setup.sh')
    if any(marker in (parent_cmdline or '') for marker in markers):
        return ppid

    return None

def terminate_legacy_wrapper():

    ## 恒久停止 (exit 65/66) するとき、契約を知らない旧ラッパーの下に
    ## いたらラッパーごと終わらせる。放置すると旧ループは終了コードを捨てて
    ## 15秒毎に再起動し続け、失効キーでサーバを叩き続けてしまう

    if IS_WINDOWS:
        return

    try:
        ppid = os.getppid()
        with open('/proc/%d/cmdline' % (ppid), 'rb') as fin:
            cmdline = fin.read().decode('utf-8', 'replace').replace('\0', ' ')

        target = legacy_wrapper_pid(
            ppid, cmdline, os.environ.get('SHOGIBENCH_WRAPPER_ACK'))

        if target is not None:
            print ('[Note] Legacy restart loop detected (pid %d); stopping it as well' % (target))
            os.kill(target, signal.SIGTERM)

    except Exception as error:
        print ('[Note] Could not check for a legacy wrapper: %s' % (error))

def shutdown_if_revoked(response):

    ## サーバが「ワーカーキーの無効化/削除 (恒久的)」を通知してきたときは、
    ## リトライせずワーカーごと終了する。終了コード 66 により setup_worker.sh
    ## の再起動ループも止まる。
    ##
    ## 判定は構造化フラグ ('shutdown') で行う。旧サーバ (フラグ未対応) 向けに
    ## SHUTDOWN_ERROR の文言だけは後方互換で見る。単なる 'Bad Credentials' では
    ## 終了しない: アカウントの一時無効化やパスワード変更などの可逆的な失敗で
    ## フリート全体が恒久停止しないように (サーバ側がキー失効を区別してフラグを立てる)

    shutdown = isinstance(response, dict) and bool(response.get('shutdown'))
    message  = response.get('error') if isinstance(response, dict) else response

    if shutdown or 'Worker key disabled or deleted' in str(message):
        print ('[Error] Server rejected our credentials: %s' % (message))
        print ('[Error] Worker key was likely disabled or deleted. Shutting down (exit code %d)' % (EXIT_SHUTDOWN))
        stop_detached_spsa()
        terminate_legacy_wrapper()
        sys.exit(EXIT_SHUTDOWN)

def load_machine_token():

    ## このインスタンスを一意に識別する永続トークン。サーバはこれを見て
    ## 「同じマシンの再登録」を既存の Machine 行の再利用にする (クラッシュ
    ## ループやバージョン更新ループでマシン一覧が無限に増えるのを防ぐ)。
    ## MAC アドレスは Docker コンテナ間で衝突しうるので使わない

    try:
        with open('.machine_token') as fin:
            token = fin.read().strip()
        if re.match(r'^[0-9a-f]{32}$', token):
            return token
    except OSError:
        pass

    token = uuid.uuid4().hex
    with open('.machine_token', 'w') as fout:
        fout.write(token)
    return token

def wait_for_server_version(config):

    ## サーバが期待する client_version と自分の CLIENT_VERSION を登録前に照合する。
    ##
    ## - サーバが古い (デプロイ待ち) 間は、登録せずにここで静かに待つ。
    ##   以前は「登録 → Bad Client Version → 再ダウンロード → 再登録」を数秒周期で
    ##   繰り返し、そのたびに新しい Machine 行を作ってマシン一覧が無限に増えていた
    ## - 10分待っても追いつかないときはロールバック (サーバを意図的に古い版へ
    ##   戻した) とみなし、クライアント側を再取得して合わせる
    ## - サーバが新しいときは BadVersionException でクライアント更新へ回す

    waited = 0

    while True:

        try:
            target   = utils.url_join(config.server, 'clientVersionRef')
            payload  = { 'username' : config.username, 'password' : config.password }
            response = requests.post(target, data=payload, timeout=TIMEOUT_HTTP).json()
        except Exception:
            return # 照合できないだけなら通常フローに任せる

        # プロキシやメンテページが dict 以外の JSON を 200 で返すことがある。
        # 照合できないだけなので、クラッシュせず通常フローに任せる
        if not isinstance(response, dict):
            return

        if 'error' in response:
            shutdown_if_revoked(response)
            return

        try:
            expected = int(response.get('client_version', CLIENT_VERSION))
        except (TypeError, ValueError):
            return # 予期しない応答形式も同様にフェイルオープン

        if expected == CLIENT_VERSION:
            return

        if expected > CLIENT_VERSION:
            print ('[Note] Server expects client v%d, we are v%d: updating' % (expected, CLIENT_VERSION))
            raise BadVersionException()

        # サーバの方が古い = サーバの再デプロイがまだ。スパムせず待つが、
        # 待ちすぎたらロールバックとみなしてクライアント側を合わせにいく
        print ('[Note] Server expects client v%d but we are v%d' % (expected, CLIENT_VERSION))

        if waited >= 600:
            print ('[Note] Server still behind after %ds: refreshing the Client to match' % (waited))
            raise BadVersionException()

        print ('[Note] Server deploy appears to be behind; waiting 60s before rechecking')
        time.sleep(60)
        waited += 60


class Configuration:

    ## Handles configuring the worker with the server. This means collecting
    ## information about the system, as well as holding any of the command line
    ## arguments provided. Lastly, a Configuration() object holds the Workload

    def __init__(self, args):

        # Basic init of every piece of System specific information
        self.compilers      = {}
        self.git_tokens     = {}
        self.cpu_flags      = []
        self.cpu_name       = ''
        self.os_name        = platform.system()
        self.os_ver         = platform.release()
        self.python_ver     = platform.python_version()
        self.mac_address    = hex(uuid.getnode()).upper()[2:]
        self.logical_cores  = psutil.cpu_count(logical=True)
        self.physical_cores = psutil.cpu_count(logical=False)
        self.hard_cpu_affinity = False
        if self.os_name == 'Linux':
            try:
                self.logical_cores = len(os.sched_getaffinity(0))
                self.physical_cores = len(linux_physical_cpu_ids())
                self.hard_cpu_affinity = True
            except (AttributeError, RuntimeError):
                pass
        self.ram_total_mb   = psutil.virtual_memory().total // (1024 ** 2)
        self.machine_name   = 'None'
        self.machine_id     = 'None'
        self.secret_token   = 'None'
        self.syzygy_max     = 2
        self.blacklist      = []

        self.process_args(args)   # Rest of the command line settings
        self.check_requirements() # Checks for Make, cargo, and g++ or clang++
        self.init_client()        # Create folder structure and verify Syzygy
        self.validate_setup()     # Check the threads and sockets values provided

    def process_args(self, args):

        # Extract all of the options
        self.username    = args.username
        self.password    = args.password
        self.server      = args.server
        self.threads     = int(args.threads) if args.threads != 'auto' else self.physical_cores
        self.sockets     = int(args.nsockets)
        self.identity    = args.identity if args.identity else 'None'
        self.syzygy_path = args.syzygy   if args.syzygy   else None
        self.fleet       = args.fleet    if args.fleet    else False
        self.noisy       = args.noisy    if args.noisy    else False
        self.focus       = args.focus    if args.focus    else []

    def check_requirements(self):

        # Verify that we have make installed
        print('\nLooking for Make... [v%s]' % locate_utility('make'))

        # Look for either g++ or clang++
        gcc_ver       = locate_utility('g++', force_exit=False, report_error=False)
        clang_ver     = locate_utility('clang++', force_exit=False, report_error=False)
        self.cxx_comp = 'g++' if gcc_ver else 'clang++' if clang_ver else None
        print('Looking for C++ Compiler... [%s v%s]' % (self.cxx_comp, locate_utility(self.cxx_comp)))

        # Cannot build fastchess nor observe CPU flags
        if not self.cxx_comp:
            print ('[Error] Unable to locate C++ Compiler (g++ or clang++)')
            sys.exit()

        # Verify that we have cargo installed (for building shogitest)
        print('\nLooking for Cargo... [v%s]' % locate_utility('cargo'))

    def init_client(self):

        # Use Client.py's path as the base pathway
        os.chdir(os.path.dirname(os.path.abspath(__file__)))

        # Ensure the folder structure for ease of coding
        for folder in ['PGNs', 'Engines', 'Networks', 'Books']:
            if not os.path.isdir(folder):
                os.mkdir(folder)

        # Check until we stop finding valid N-man tables
        if self.syzygy_path:
            while validate_syzygy_exists(self, self.syzygy_max+1):
                self.syzygy_max = self.syzygy_max + 1

        # 1-man and 2-man tables are not a thing
        if self.syzygy_max < 3:
            self.syzygy_max = 0

        # Report highest complete depth that we found
        print('Looking for Syzygy... [%d-Man]' % (self.syzygy_max))

    def validate_setup(self):

        assert self.threads >= self.sockets
        assert self.threads % self.sockets == 0
        assert min(self.threads, self.sockets) >= 1

    def scan_for_compilers(self, data):

        print ('\nScanning for Compilers...')

        # For each engine, attempt to find a valid compiler
        for engine, build_info in data.items():

            # Private engines don't need to be compiled
            if build_info['private']: continue

            # Try to find at least one working compiler
            for compiler in build_info['compilers']:

                # Compilers may require a specific version
                if '>=' in compiler:
                    compiler, version = compiler.split('>=')
                    version = tuple(map(int, version.split('.')))
                else: version = (0, 0, 0)

                # Try to confirm this compiler is present, and new enough
                try:
                    match = get_version(compiler)
                    if tuple(map(int, match.split('.'))) >= version:
                        print('%-16s | %-8s (%s)' % (engine, compiler, match))
                        self.compilers[engine] = (compiler, match)
                        break
                except: continue # Unable to execute compiler

            # Report missing engines in case the User is not expecting it
            if engine not in self.compilers:
                print('%-16s | Missing %s' % (engine, data[engine]['compilers']))

    def scan_for_private_tokens(self, data):

        print ('\nScanning for Private Tokens...')

        # For each engine, attempt to find a valid compiler
        for engine, build_info in data.items():

            # Public engines don't need access tokens
            if not build_info['private']: continue

            # Private engines expect a credentials.engine file for the main repo
            has_token = os.path.exists('credentials.%s' % (engine.replace(' ', '').lower()))
            print('%-16s | %s' % (engine, ['Missing', 'Found'][has_token]))
            if has_token: self.git_tokens[engine] = True

    def scan_for_cpu_flags(self, data):

        print('\nScanning for CPU Flags...')

        # Get all flags, and for sanity uppercase them
        info   = cpuinfo.get_cpu_info()
        actual = [x.replace("_", "").replace(".", "").upper() for x in info.get('flags', [])]

        # Set the CPU name which has to be done via global
        self.cpu_name = info.get('brand_raw', info.get('brand', 'Unknown'))

        # This should cover virtually all compiler flags that we would care about
        desired  = ['POPCNT', 'BMI2']
        desired += ['SSSE3', 'SSE41', 'SSE42', 'SSE4A', 'AVX', 'AVX2', 'FMA']
        desired += ['AVX512VNNI', 'AVX512BW', 'AVX512DQ', 'AVX512F']

        # Add any custom flags from the OpenBench configs, just in case we missed one
        requested = set(sum([info['cpuflags'] for engine, info in data.items()], []))
        for flag in [x for x in requested if x not in desired]: desired.append(flag)
        self.cpu_flags = [x for x in desired if x in actual]

        # Report the results of our search, including any "missing flags
        print ('Found   |', ' '.join(self.cpu_flags))
        print ('Missing |', ' '.join([x for x in desired if x not in actual]))

class ServerReporter:

    ## Handles reporting things to the server, which are not intended to send a great
    ## deal of information back. Reports to the server can hit various endpoints, with
    ## differing payloads. Payloads must always include the machine id, and secret token

    @staticmethod
    def report(config, endpoint, payload, files=None):

        payload['machine_id'] = config.machine_id
        payload['secret']     = config.secret_token

        target   = utils.url_join(config.server, endpoint)
        response = requests.post(target, data=payload, files=files, timeout=TIMEOUT_HTTP)

        # Check for a json repsone, to look for Client Version Errors
        try: as_json = response.json()
        except: return response

        # Throw all the way back to the client.py
        if 'Bad Client Version' in as_json.get('error', ''):
            raise BadVersionException()

        # キー失効は恒久的なのでワーカーごと終了する
        if 'error' in as_json:
            shutdown_if_revoked(as_json)

        # Some fatal error, forcing us out of the Workload
        if 'error' in as_json:
            raise utils.OpenBenchFatalWorkerException(as_json['error'])

        return response

    @staticmethod
    def report_nps(config, dev_nps, base_nps):

        payload = {
            'nps'      : (dev_nps + base_nps) // 2,
            'dev_nps'  : int(dev_nps),
            'base_nps' : int(base_nps),
        }

        return ServerReporter.report(config, 'clientSubmitNPS', payload)

    @staticmethod
    def report_missing_artifact(config, artifact_name, artifact_json):

        payload = {
            'test_id'    : config.workload['test']['id'],
            'error'      : 'Artifact %s missing' % (artifact_name),
            'logs'       : json.dumps(artifact_json, indent=2),
        }

        return ServerReporter.report(config, 'clientSubmitError', payload)

    @staticmethod
    def report_build_fail(config, branch, output):

        branch_name = config.workload['test'][branch]['name']
        engine_name = config.workload['test'][branch]['engine']
        final_name  = '[%s] %s' % (engine_name, branch_name)

        payload = {
            'test_id'    : config.workload['test']['id'],
            'error'      : '%s build failed' % (final_name),
            'logs'       : output,
        }

        return ServerReporter.report(config, 'clientSubmitError', payload)

    @staticmethod
    def report_engine_error(config, error, pgn=None):

        payload = {
            'test_id'    : config.workload['test']['id'],
            'error'      : error,
            'logs'       : pgn,
        }

        return ServerReporter.report(config, 'clientSubmitError', payload)

    @staticmethod
    def report_bad_bench(config, error):

        payload = {
            'test_id'    : config.workload['test']['id'],
            'error'      : error,
        }

        return ServerReporter.report(config, 'clientBenchError', payload)

    @staticmethod
    def report_results(config, batches):

        payload = {

            'test_id'      : config.workload['test']['id'],
            'result_id'    : config.workload['result']['id'],

            'trinomial'    : [0, 0, 0],       # LDW
            'pentanomial'  : [0, 0, 0, 0, 0], # LL DL DD DW WW

            'crashes'      : 0, # " disconnect" or "connection stalls"
            'timelosses'   : 0, # " loses on time "
            'illegals'     : 0, # " illegal move "
        }

        for batch in batches:

            payload['trinomial'  ] = [x+y for x,y in zip(payload['trinomial'  ], batch['trinomial'  ])]
            payload['pentanomial'] = [x+y for x,y in zip(payload['pentanomial'], batch['pentanomial'])]

            payload['crashes'   ] += batch['crashes'   ]
            payload['timelosses'] += batch['timelosses']
            payload['illegals'  ] += batch['illegals'  ]

            if config.workload['test']['type'] == 'SPSA':

                # Pairs can be added one at a time, or in bulk
                result = batch['trinomial'][2] - batch['trinomial'][0]

                # For each param compute the update step for the Server
                for name, param in config.workload['spsa'].items():
                    delta = param['r'] * param['c'] * result * param['flip'][batch['runner_idx']]
                    payload['spsa_%s' % (name)] = payload.get('spsa_%s' % (name), 0.0) + delta

        # Collapse into a JSON friendly format for Django
        payload['trinomial'  ] = ' '.join(map(str, payload['trinomial'  ]))
        payload['pentanomial'] = ' '.join(map(str, payload['pentanomial']))

        print (payload)

        return ServerReporter.report(config, 'clientSubmitResults', payload)

    @staticmethod
    def report_heartbeat(config):

        payload = {
            'test_id' : config.workload['test']['id']
        }

        return ServerReporter.report(config, 'clientHeartbeat', payload)

    @staticmethod
    def report_pgn(config, compressed_pgn_text, part=0):

        payload = {
            'test_id'      : config.workload['test']['id'],
            'result_id'    : config.workload['result']['id'],
            'book_index'   : config.workload['test']['book_index'],
            'part'         : part,
            'Content-Type' : 'application/octet-stream',
        }

        files = {
            'file' : ('games.pgn', compressed_pgn_text)
        }

        return ServerReporter.report(config, 'clientSubmitPGN', payload, files)

class MatchRunner:

    ## Handles building the very long string of arguments that need to be passed
    ## to match runner in order to launch a set of games. Operates on the Configuration,
    ## and a small number of secondary arguments that are not housed in the Configuration

    @staticmethod
    def is_shogi(config):
        book_name = config.workload['test']['book']['name'].upper()
        return 'SHOGI' in book_name

    @staticmethod
    def executable(config):
        if MatchRunner.is_shogi(config):
            return ['shogitest-ob.exe', './shogitest-ob'][IS_LINUX]
        else:
            return ['fastchess-ob.exe', './fastchess-ob'][IS_LINUX]

    @staticmethod
    def engine_option_tokens(config, options):

        tokens = re.findall(r'"[^"]*"|\S+', options)
        if not MatchRunner.is_shogi(config):
            return tokens

        # ShogiBench上の共通名Hashを、YaneuraOuのUSI名へ変換する。
        # Threadsより先に送って、既定のUSI_Hash=1024を一時確保させない。
        hash_tokens = []
        other_tokens = []
        for token in tokens:
            name, separator, value = token.partition('=')
            if separator and name.casefold() == 'hash':
                hash_tokens.append('USI_Hash=%s' % value)
            else:
                other_tokens.append(token)

        return hash_tokens + other_tokens

    @staticmethod
    def basic_settings(config):

        # Assume Fischer if FRC, 960, or FISCHER appears in the Opening Book
        book_name = config.workload['test']['book']['name'].upper()
        is_frc    = 'FRC' in book_name or '960' in book_name or 'FISCHER' in book_name
        variant   = ['standard', 'fischerandom'][is_frc]

        # Only include -repeat if not skipping the reverses in DATAGEN
        is_datagen = config.workload['test']['type'] == 'DATAGEN'
        no_reverse = is_datagen and not config.workload['test']['play_reverses']

        # Always include -recover, -variant, and -testEnv
        return ['-repeat', ''][no_reverse] + ' -recover -variant %s -testEnv' % (variant)

    @staticmethod
    def concurrency_settings(config):

        # Already computed for us by the Server
        concurrency = config.workload['distribution']['concurrency-per']
        total_games = config.workload['distribution']['games-per-runner']

        # shogitest plays (pairings x rounds x games) games, where rounds is
        # the number of games per opening (2, from -repeat). fastchess counts
        # the total directly via -rounds.
        if MatchRunner.is_shogi(config):
            return '-concurrency %d -games %d' % (concurrency, max(1, total_games // 2))

        return '-concurrency %d -rounds %d' % (concurrency, total_games)

    @staticmethod
    def affinity_settings(config, runner_idx):

        distribution = config.workload['distribution']
        if not MatchRunner.is_shogi(config) or not distribution.get('cpu-affinity', False):
            return ''

        if platform.system() != 'Linux':
            raise RuntimeError('Ponder CPU affinity is supported only on Linux workers')

        dev_threads = int(extract_option(config.workload['test']['dev']['options'], 'Threads'))
        base_threads = int(extract_option(config.workload['test']['base']['options'], 'Threads'))
        expected_threads = dev_threads + base_threads
        game_threads = distribution.get('threads-per-game')
        if game_threads != expected_threads:
            raise ValueError(
                'Invalid Ponder thread budget: server sent %r, expected %d'
                % (game_threads, expected_threads))

        runner_count = distribution['runner-count']
        concurrency = distribution['concurrency-per']
        cpus_per_runner = concurrency * game_threads
        total_required = runner_count * cpus_per_runner
        physical_cpus = linux_physical_cpu_ids()
        configured_capacity = min(config.threads, len(physical_cpus))
        if total_required > configured_capacity:
            raise RuntimeError(
                'Ponder requires %d dedicated physical CPUs, but only %d are available '
                'to this worker' % (total_required, configured_capacity))
        if runner_idx < 0 or runner_idx >= runner_count:
            raise ValueError('Invalid match runner index %d' % runner_idx)

        start = runner_idx * cpus_per_runner
        cpus = physical_cpus[start:start + cpus_per_runner]
        return '-cpu-affinity ' + ','.join(str(cpu) for cpu in cpus)

    @staticmethod
    def adjudication_settings(config):

        # All three possible adjudication settings
        win_adj    = config.workload['test']['win_adj'   ]
        draw_adj   = config.workload['test']['draw_adj'  ]
        syzygy_adj = config.workload['test']['syzygy_adj']

        # Empty, unless specified in the settings
        win_flags    = ['', '-resign ' + win_adj ][win_adj  != 'None']
        draw_flags   = ['', '-draw '   + draw_adj][draw_adj != 'None']
        syzygy_flags = ''

        # Set the tb path if we have them, and are allowed to use them
        if syzygy_adj != 'DISABLED' and config.syzygy_max:
            syzygy_flags = '-tb %s' % (config.syzygy_path.replace('\\', '\\\\'))

        # We would only get a test we can do; specify a limit if needed
        if syzygy_adj != 'DISABLED' and syzygy_adj != 'OPTIONAL':
            syzygy_flags += ' -tbpieces %s' % (syzygy_adj.split('-')[0])

        return '%s %s %s' % (win_flags, draw_flags, syzygy_flags)

    @staticmethod
    def book_settings(config, runner_idx):

        # DATAGEN creates their own book
        if config.workload['test']['type'] == 'DATAGEN':

            # -repeat might not be applied, so handle the book offsets
            no_reverse = not config.workload['test']['play_reverses']
            pairs      = config.workload['distribution']['games-per-runner'] // 2
            start      = 1 + (runner_idx * pairs * (1 + no_reverse))
            return '-openings file=Books/openbench.genfens.epd format=epd order=sequential start=%d' % (start)

        # Can handle EPD and PGN Books, which must be specified
        book_name   = config.workload['test']['book']['name']
        book_suffix = book_name.split('.')[-1]

        # Start position is determined partially by runner index
        pairs = config.workload['distribution']['games-per-runner'] // 2
        start = config.workload['test']['book_index'] + runner_idx * pairs

        return '-openings file=Books/%s format=%s order=random start=%d -srand %d' % (
            book_name, book_suffix, start, config.workload['test']['book_seed'])

    @staticmethod
    def engine_settings(config, command, branch, scale_factor, runner_idx):

        # Extract configuration from the Workload
        options = config.workload['test'][branch]['options']
        network = config.workload['test'][branch]['network']
        private = config.workload['test'][branch]['private']
        engine  = config.workload['test'][branch]['engine']
        syzygy  = config.workload['test']['syzygy_wdl']

        # Human-readable name, and scale the time control
        name    = command.replace('.exe', '')
        proto   = ["uci", "usi"][MatchRunner.is_shogi(config)]
        control = scale_time_control(config.workload, scale_factor, branch)
        ponder  = ''

        # ponder= は shogitest 固有のエンジン設定。fastchessへは渡さない。
        if MatchRunner.is_shogi(config):
            ponder_mode = config.workload['test'][branch].get('ponder_mode', 'off')
            if ponder_mode not in ['off', 'standard', 'early']:
                raise ValueError('Unknown Ponder mode for %s: %s' % (branch, ponder_mode))
            ponder = ' ponder=%s' % ponder_mode

        # Private engines, when using Networks, must set them via UCI
        if private and network and network != 'None':
            options += ' EvalFile=%s' % (os.path.join('../Networks', network))
            name    += '-%s' % (network)

        # Public engines whose Makefile cannot embed a Network (no EVALFILE
        # support, eg YaneuraOu) receive it as a runtime option instead.
        # Engines launched by the match runner run from Engines/, hence '..'
        for opt_name, opt_value in stage_network_options(config, branch, prefix='..'):
            options += ' %s=%s' % (opt_name, opt_value)

        # Path-type options in the Test's option field may reference this
        # branch's network staging directory as {DIR}, whose location is
        # unknowable when the test is created (eg LS_PROGRESS_COEFF=
        # {DIR}/coeff.bin, pointing at one of the network's aux files)
        if '{DIR}' in options:
            staged_dir = staged_network_dir(config, branch)
            if not staged_dir:
                print ('Warning: {DIR} used, but %s has no staged network directory' % (branch))
            options = options.replace('{DIR}', staged_dir)

        # Set the SyzygyPath if we have them, and are allowed to use them
        if syzygy != 'DISABLED' and config.syzygy_max:
            options += ' SyzygyPath=%s' % (config.syzygy_path.replace('\\', '\\\\'))

        # Set a SyzygyProbeLimit if we may only use up-to N-Man
        if syzygy != 'DISABLED' and syzygy != 'OPTIONAL':
            options += ' SyzygyProbeLimit=%s' % (syzygy.split('-')[0])

        # Add any of the custom SPSA settings
        if config.workload['test']['type'] == 'SPSA':
            for param, data in config.workload['spsa'].items():
                options += ' %s=%s' % (param, str(data[branch][runner_idx]))

        # Join options together in format expected by match runner
        options = ' option.'.join([''] + MatchRunner.engine_option_tokens(config, options))
        return '-engine dir=Engines/ cmd=./%s proto=%s %s%s%s name=%s-%s' % (
            command, proto, control, ponder, options, engine, branch)

    @staticmethod
    def pgnout_settings(config, timestamp, runner_idx):
        return '-pgnout file=%s seldepth=true nodes=true' % (MatchRunner.pgn_name(config, timestamp, runner_idx))

    @staticmethod
    def update_results(results, line):

        # Given any game #, find the other in the pair
        def game_to_pair(g):
            return (g, g+1) if g % 2 else (g-1, g)

        game, result, reason = MatchRunner.parse_finished_game(line)

        # Parse for errors resulting in adjudication
        results['crashes'   ] += 'disconnect' in reason or 'stalls' in reason
        results['timelosses'] += 'on time' in reason
        results['illegals'  ] += 'illegal' in reason

        # 局番号と Dev 視点の結果を保存
        results['games'][game] = result

        # Check to see if the Pair has finished
        first, second = game_to_pair(game)
        if first not in results['games'] or second not in results['games']:
            return

        # Get the indices for the Pentanomial, and the two for Trinomial
        p = results['games'][first] + results['games'][second]
        t1, t2 = results['games'][first], results['games'][second]

        # Update everything
        results['trinomial'  ][t1] += 1
        results['trinomial'  ][t2] += 1
        results['pentanomial'][p ] += 1

        # Clean up results['games']
        del results['games'][first]
        del results['games'][second]

    @staticmethod
    def parse_finished_game(line):

        def parse_engine_role(name):
            name = name.strip()
            if name == 'dev' or name.endswith('-dev'):
                return 'dev'
            if name == 'base' or name.endswith('-base'):
                return 'base'
            raise ValueError('Unable to identify engine role in result line: %s' % (line))

        def dev_result(white, black, result):
            white_role = parse_engine_role(white)
            black_role = parse_engine_role(black)

            if {white_role, black_role} != {'dev', 'base'}:
                raise ValueError('Unable to identify dev/base pairing in result line: %s' % (line))

            if result == '1/2-1/2':
                return 1
            if result == '1-0':
                return 2 if white_role == 'dev' else 0
            if result == '0-1':
                return 2 if black_role == 'dev' else 0

            raise ValueError('Unable to convert result to dev perspective: %s' % (line))

        match = re.match(
            r'^Finished game\s+'
            r'(?P<game>[0-9]+)'
            r'(?:\s+of\s+(?:[0-9]+|infinite))?'
            r'\s+\((?P<white>.*?)\s+vs\s+(?P<black>.*?)\):\s+'
            r'(?P<result>1-0|0-1|1/2-1/2|undetermined)'
            r'\s+\{(?P<reason>.*)\}\s*$',
            line.strip())

        if not match:
            raise ValueError('Unable to parse match runner result line: %s' % (line))

        return (
            int(match.group('game')),
            dev_result(match.group('white'), match.group('black'), match.group('result')),
            match.group('reason'))

    @staticmethod
    def kill_everything(dev_process, base_process):

        if IS_LINUX:
            utils.kill_process_by_name('fastchess-ob')
            utils.kill_process_by_name('shogitest-ob')

        if IS_WINDOWS:
            utils.kill_process_by_name('fastchess-ob.exe')
            utils.kill_process_by_name('shogitest-ob.exe')

        utils.kill_process_by_name(dev_process)
        utils.kill_process_by_name(base_process)

    @staticmethod
    def pgn_name(config, timestamp, runner_idx):

        test_id   = int(config.workload['test']['id'])
        result_id = int(config.workload['result']['id'])

        # Format: <Test>-<Result>-<Time>-<Index>.pgn
        return 'PGNs/%d.%d.%d.%d.pgn' % (test_id, result_id, timestamp, runner_idx)


class PGNHelper:

    @staticmethod
    def slice_pgn_file(file):

        if not os.path.isfile(file):
            reason = 'Unable to find %s. Match runner exited with no finished games.' % (file)
            raise utils.OpenBenchMisssingPGNException(reason)

        with open(file) as pgn:

            while True:

                headers = list(iter(lambda: pgn.readline().rstrip(), ''))
                moves   = list(iter(lambda: pgn.readline().rstrip(), ''))

                if not headers or not moves:
                    break

                yield (headers, moves)

    @staticmethod
    def get_pgn_header(sliced_headers, header):
        for line in sliced_headers:
            if line.startswith('[%s ' % header):
                return line.split('"')[1]

    @staticmethod
    def get_error_reason(sliced_headers):

        reason = PGNHelper.get_pgn_header(sliced_headers, 'Termination')

        if reason and 'abandoned' in reason:
            return 'Disconnect'

        if reason and 'stalled' in reason:
            return 'Stalled'

        if reason and 'illegal' in reason:
            return 'Illegal Move'

    @staticmethod
    def pretty_format(headers, moves):
        return '\n'.join(headers + [''] + moves)


class PGNArchiveReporter:

    def __init__(self, config, file_names, scale_factor):
        self.config       = config
        self.file_names   = file_names
        self.scale_factor = scale_factor
        self.compact      = config.workload['test']['upload_pgns'] == 'COMPACT'
        self.enabled      = config.workload['test']['upload_pgns'] != 'FALSE'
        self.offsets      = {}
        self.part         = 0

    def checkpoint(self, final=False):

        if not self.enabled:
            return False

        compressed, next_offsets = pgn_util.compress_new_pgns(
            self.file_names, self.offsets, self.scale_factor, self.compact, final=final)

        if compressed is None:
            return False

        # 送信が成功した後だけ位置と連番を進める。応答消失時は同じpartを再送し、
        # サーバー側の一意制約で二重アーカイブを防ぐ。
        response = ServerReporter.report_pgn(self.config, compressed, self.part)
        response.raise_for_status()
        self.offsets = next_offsets
        self.part += 1
        return True

class ResultsReporter(object):

    ## Handles idle looping while reading from the results Queue that the match runner
    ## workers place results into. Once finished, this class can be used to collect
    ## all of the errors in the PGN, and send htem back to the server.

    def __init__(self, config, tasks, results_queue, abort_flag, pgn_reporter=None):
        self.config        = config
        self.tasks         = tasks
        self.results_queue = results_queue
        self.abort_flag    = abort_flag
        self.pgn_reporter  = pgn_reporter

    def checkpoint_pgn(self):

        if self.pgn_reporter is None:
            return

        try:
            self.pgn_reporter.checkpoint()
        except (BadVersionException, utils.OpenBenchFatalWorkerException):
            raise
        except Exception:
            # 結果報告は成功済みなので、棋譜だけ次の周期に同じ位置から再送する。
            traceback.print_exc()
            print ('[Note] Failed to checkpoint PGNs; retrying on the next report...')

    def process_until_finished(self):

        self.last_report = 0
        self.pending     = []

        # Don't report until finished, for BULK SPSA tests
        self.bulk = self.config.workload['test']['type'] == 'SPSA'
        self.bulk = self.bulk and self.config.workload['reporting_type'] == 'BULK'

        # Block up-to 5 seconds to get a new result
        def get_next_result():
            try: return self.results_queue.get(timeout=5)
            except queue.Empty: return False

        # Collect results until all Tasks are done
        while any(not task.done() for task in self.tasks):

            result = get_next_result()
            if result:
                self.pending.append(result)

            # Send results, or a heartbeat, every REPORT_INTERVAL seconds until done
            if self.send_results(report_interval=REPORT_INTERVAL):
                return

            # Kill everything if openbench.exit is created
            if os.path.isfile('openbench.exit'):
                return self.abort_flag.set()

        # Exhaust the Results Queue completely since Tasks are done
        while True:
            result = get_next_result()
            if result:
                self.pending.append(result)
            else:
                break

        # Send any remaining results immediately
        self.send_results(report_interval=0, final_report=True)

    def send_results(self, report_interval, final_report=False):

        # Do not send more often than report_interval dictates
        if self.last_report + report_interval > time.time():
            return False

        try:

            # Heartbeat when no results, or still awaiting bulk results
            if not self.pending or (self.bulk and not final_report):
                response = ServerReporter.report_heartbeat(self.config).json()
                self.last_report = time.time()

            else: # Send all of the queued Results at once
                response = ServerReporter.report_results(self.config, self.pending).json()
                self.last_report = time.time()
                self.pending = []

            self.checkpoint_pgn()

            # If the test ended, kill all tasks
            if 'stop' in response:
                self.abort_flag.set()

            # Signal an exit if the test ended
            return 'stop' in response

        except (BadVersionException, utils.OpenBenchFatalWorkerException):
            raise

        except Exception:
            traceback.print_exc()
            print ('[Note] Failed to upload results to server...')
            self.last_report = time.time()

    def send_errors(self, timestamp, runner_cnt):

        for x in range(runner_cnt):

            # Reuse logic that was given to match runner to decide the PGN name
            fname = MatchRunner.pgn_name(self.config, timestamp, x)

            # For any game with weird Termination, report it
            for header, moves in PGNHelper.slice_pgn_file(fname):
                error = PGNHelper.get_error_reason(header)
                if error:
                    as_str = PGNHelper.pretty_format(header, moves)
                    ServerReporter.report_engine_error(self.config, error, as_str)


def get_version(program):

    # Try to execute the program from the command line
    # First with `--version`, and again with just `version`

    try:
        process = Popen([program, '--version'], stdout=PIPE, stderr=PIPE)
        stdout  = process.communicate()[0].decode('utf-8')
        return re.search(r'\d+\.\d+(\.\d+)?', stdout).group()

    except:
        process = Popen([program, 'version'], stdout=PIPE, stderr=PIPE)
        stdout  = process.communicate()[0].decode('utf-8')
        return re.search(r'\d+\.\d+(\.\d+)?', stdout).group()

def compare_versions(program_path, min_version_str):

    if not program_path:
        return None

    version_str = get_version(program_path)

    if not version_str:
        return None

    program_ver = tuple(map(int, version_str.split('.')))
    minimum_ver = tuple(map(int, min_version_str.split('.')))
    return version_str if program_ver >= minimum_ver else None

def locate_utility(util, force_exit=True, report_error=True):

    try: return get_version(util)

    except Exception:
        if report_error: print('[Error] Unable to locate %s' % (util))
        if force_exit: sys.exit()

def set_runner_permissions():

    status = os.system('sudo -n chmod 777 fastchess-ob > /dev/null 2>&1')
    if status != 0:
        status = os.system('chmod 777 fastchess-ob > /dev/null 2>&1')
    if status != 0:
        print ('[ERROR] Unable to set execute permissions on fastchess-ob')

    status = os.system('sudo -n chmod 777 shogitest-ob > /dev/null 2>&1')
    if status != 0:
        status = os.system('chmod 777 shogitest-ob > /dev/null 2>&1')
    if status != 0:
        print ('[ERROR] Unable to set execute permissions on shogitest-ob')


def cleanup_client():

    SECONDS_PER_DAY   = 60 * 60 * 24
    SECONDS_PER_WEEK  = SECONDS_PER_DAY * 7
    SECONDS_PER_MONTH = SECONDS_PER_WEEK * 4

    file_age = lambda x: time.time() - os.path.getmtime(x)

    for file in os.listdir('PGNs'):
        if file_age(os.path.join('PGNs', file)) > SECONDS_PER_DAY:
            os.remove(os.path.join('PGNs', file))

    for file in os.listdir('Engines'):
        if file_age(os.path.join('Engines', file)) > SECONDS_PER_WEEK:
            os.remove(os.path.join('Engines', file))

    for file in os.listdir('Networks'):
        if file_age(os.path.join('Networks', file)) > SECONDS_PER_MONTH:
            os.remove(os.path.join('Networks', file))

    # 実行中でない SPSA (rshogi) の残骸 run dir も一ヶ月で掃除する
    spsa_rshogi.cleanup_stale_runs(SECONDS_PER_MONTH)

def validate_syzygy_exists(config, K):

    letters = ['', 'Q', 'R', 'B', 'N', 'P']

    # Generate many potential K[] v K[], including all valid ones
    candidates = ['K%svK%s' % (''.join(lhs), ''.join(rhs))
        for N in range(1, K - 1)
            for lhs in combinations_with_replacement(letters, N)
                for rhs in combinations_with_replacement(letters, K - N - 2)]

    # Syzygy does LHS having more pieces first, stronger pieces second
    def valid_filename(name):
        for i, letter in enumerate(letters[1:]):
            name = name.replace(letter, str(9 - i))
        lhs, rhs = name.replace('K', '9').split('v')
        return int(lhs) >= int(rhs) and name != 'KvK'

    # See if file exists in (any of) the paths
    def has_filename(paths, name):
        for path in paths:
            if os.path.isfile(os.path.join(path, name + '.rtbw')):
                return True
        return False

    # Split paths, using ":" on Unix, and ";" on Windows
    paths = config.syzygy_path.split(':' if IS_LINUX else ';')

    # Check to see if each Syzygy File exists as desired
    for filename in list(filter(valid_filename, set(candidates))):
        if not has_filename(paths, filename):
            return False

    return True


def scale_time_control(workload, scale_factor, branch):

    # Extract everything from the workload dictionary
    time_control  = workload['test'][branch]['time_control']
    is_shogi      = 'SHOGI' in workload['test']['book']['name'].upper()

    # Searching for Nodes or Depth time controls ("N=", "D=")
    pattern = r'(?P<mode>((N))|(D))=(?P<value>(\d+))'
    results = re.search(pattern, time_control.upper())

    # No scaling is needed for fixed nodes or fixed depth games
    if results:
        mode, value = results.group('mode', 'value')

        # shogitest takes the node limit as the time control itself,
        # and rejects tc=inf outright
        if is_shogi and mode == 'N':
            return 'nodes=%s' % (value)

        return 'tc=inf %s=%s' % ({'N' : 'nodes', 'D' : 'depth'}[mode], value)

    # Searching for MoveTime or Fixed Time Controls ("MT=")
    pattern = r'(?P<mode>(MT))=(?P<value>(\d+))'
    results = re.search(pattern, time_control.upper())

    # Scale the time based on this machine's NPS. Add a time Margin to avoid time losses.
    if results:
        mode, value = results.group('mode', 'value')

        # shogitest expects st= in whole milliseconds
        if is_shogi:
            return 'st=%d timemargin=250' % (max(1, int(float(value) * scale_factor)))

        return 'st=%.2f timemargin=250' % ((float(value) * scale_factor / 1000))

    # Searching for "X/Y+Z" time controls
    pattern = r'(?P<moves>(\d+/)?)(?P<base>\d*(\.\d+)?)(?P<inc>\+(\d+\.)?\d+)?'
    results = re.search(pattern, time_control)
    moves, base, inc = results.group('moves', 'base', 'inc')

    # Strip the trailing and leading symbols
    moves = None if moves == '' else moves.rstrip('/')
    inc   = 0.0  if inc   is None else inc.lstrip('+')

    # Scale the time based on this machine's NPS
    base = float(base) * scale_factor
    inc  = float(inc ) * scale_factor

    # Format the time control for match runner
    if moves is None:
        return 'tc=%.2f+%.2f timemargin=250' % (base, inc)
    return 'tc=%d/%.2f+%.2f timemargin=250' % (int(moves), base, inc)

def find_pgn_error(reason, command):

    pgn_file = command.split('-pgnout file=')[1].split()[0]
    with open(pgn_file, 'r') as fin:
        data = fin.readlines()

    reason = reason.split('{')[1]
    for ii in range(len(data) - 1, -1, -1):
        if reason in data[ii]:
            break

    pgn = ""
    while "[Event " not in data[ii]:
        pgn = data[ii] + pgn
        ii = ii - 1
    return data[ii] + pgn


def determine_scale_factor(config, dev_name, dev_network, base_name, base_network):

    # Run the benchmarks and compute the scaling NPS value
    dev_nps  = safe_run_benchmarks(config, 'dev' , dev_name , dev_network )
    base_nps = safe_run_benchmarks(config, 'base', base_name, base_network)
    ServerReporter.report_nps(config, dev_nps, base_nps)

    dev_factor = base_factor = None

    # Scaling is only done relative to the Dev Engine
    if config.workload['test']['scale_method'] == 'DEV':
        factor = config.workload['test']['scale_nps'] / dev_nps
        print ('\nScale Factor (Using Dev): %.4f' % (factor))

    # Scaling is only done relative to the Base Engine
    elif config.workload['test']['scale_method'] == 'BASE':
        factor = config.workload['test']['scale_nps'] / base_nps
        print ('\nScale Factor (Using Base): %.4f' % (factor))

    # Scaling is done using an average of both Engines
    else:
        dev_factor  = config.workload['test']['scale_nps'] / dev_nps
        base_factor = config.workload['test']['scale_nps'] / base_nps
        factor      = (dev_factor + base_factor) / 2
        print ('\nScale Factor (Using Dev ): %.4f' % (dev_factor))
        print ('Scale Factor (Using Base): %.4f' % (base_factor))
        print ('Scale Factor (Using Both): %.4f' % (factor))

    return factor

## Functions interacting with the OpenBench server that establish the initial
## connection and then make simple requests to retrieve Workloads as json objects

def server_configure_fastchess(config):
    server_configure_match_runner(config, 'fastchess', build_fastchess_in_dir)

def server_configure_shogitest(config):
    server_configure_match_runner(config, 'shogitest', build_shogitest_in_dir)

def server_configure_match_runner(config, name, build_func):

    # OpenBench Server holds the runner repo and git-ref
    print ('\nConfiguring %s...' % name)
    print ('> Requesting %s configuration from openbench' % name)
    target  = url_join(config.server, 'clientMatchRunnerVersionRef')
    payload = { 'username' : config.username, 'password' : config.password }
    data    = requests.post(target, data=payload, timeout=TIMEOUT_HTTP).json()

    # キー失効なら try_forever に握られる前にワーカーごと終了する。この関数は
    # 起動順で最初の認証付き通信なので、ここを素通しにすると失効キーが
    # 15秒毎の永久リトライになってしまう
    if 'error' in data:
        shutdown_if_revoked(data)

    # The 'error' header is included if there was an issue (eg Bad Credentials)
    if 'error' in data:
        raise Exception('Server error: %s' % data['error'])

    # Might already have a sufficiently new Fastchess binary
    print ('> Checking for existing %s-ob binary' % name)
    runner_path = os.path.join(os.getcwd(), '%s-ob' % name)
    runner_path = utils.check_for_engine_binary(runner_path)
    acceptable_ver = compare_versions(runner_path, data['%s_min_version' % name])

    if acceptable_ver:
        print ('> Found %s-ob v%s' % (name, acceptable_ver))
        setattr(config, '%s_ver' % name, acceptable_ver)
        return

    # Download a .zip archive of the git-ref from the specified repo
    repo_url, repo_ref = data['%s_repo_url' % name], data['%s_repo_ref' % name]
    print ('> Downloading %s from %s' % (repo_ref, repo_url))
    response = requests.get(url_join(repo_url, 'archive', '%s.zip' % repo_ref))

    with tempfile.TemporaryDirectory() as temp_dir:

        # Move the .zip contents into a temporary .zip file
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            tmp_file.write(response.content)
            temp_zip_path = tmp_file.name

        # Extract the .zip file into our local directory
        with zipfile.ZipFile(temp_zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)

        # Prepare to build, using the root folder of the extracted files as the cwd
        print ('> Extracting and building %s %s' % (name, repo_ref))
        runner_dir = os.path.join(temp_dir, os.listdir(temp_dir)[0])
        bin_path   = os.path.join(runner_dir, name)

        build_func(config, runner_dir)

        # Somehow we built runner but failed to find the binary
        if not utils.check_for_engine_binary(bin_path):
            raise OpenBenchMatchRunnerBuildFailedException()

        # Append .exe if needed, and then report the match runner version that was built
        binary  = utils.check_for_engine_binary(bin_path)
        version = get_version(binary)
        setattr(config, '%s_ver' % name, version)
        print ('> Finished building v%s' % version)

        # Move the finished match runner binary to the Client's Root directory
        out_path = os.path.join(os.getcwd(), os.path.basename(binary).replace(name, '%s-ob' % name))
        shutil.move(binary, out_path)

def build_fastchess_in_dir(config, runner_dir):
    print ('> Using C++ compiler %s...' % config.cxx_comp)

    # Execute the build, using our C++ compiler, and record any output
    make_cmd    = ['make', '-j', 'CXX=%s' % config.cxx_comp]
    process     = subprocess.Popen(make_cmd, cwd=runner_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    comp_output = process.communicate()[0].decode('utf-8')

    # Make threw an error, and thus failed to build
    if process.returncode:
        print ('\nFailed to build fastchess\n\nCompiler Output:')
        for line in comp_output.split('\n'):
            print ('> %s' % (line))
        raise OpenBenchMatchRunnerBuildFailedException()

def build_shogitest_in_dir(config, runner_dir):
    make_cmd    = ['make', 'openbench']
    process     = subprocess.Popen(make_cmd, cwd=runner_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    comp_output = process.communicate()[0].decode('utf-8')

    # Make threw an error, and thus failed to build
    if process.returncode:
        print ('\nFailed to build shogitest\n\nCompiler Output:')
        for line in comp_output.split('\n'):
            print ('> %s' % (line))
        raise OpenBenchMatchRunnerBuildFailedException()

def server_configure_worker(config):

    # Server tells us how to build or obtain binaries
    target = utils.url_join(config.server, 'clientGetBuildInfo')
    data   = requests.get(target, timeout=TIMEOUT_HTTP).json()

    config.scan_for_compilers(data)      # Public engine build tools
    config.scan_for_private_tokens(data) # Private engine access tokens
    config.scan_for_cpu_flags(data)      # For executing binaries
    config.machine_id = None             # None, until registration occurs for a session

    system_info = {
        'compilers'      : config.compilers,      # Key: Engine, Value: (Compiler, Version)
        'tokens'         : config.git_tokens,     # Key: Engine, Value: True, for tokens we have
        'cpu_flags'      : config.cpu_flags,      # List of CPU flags found in the Client or Server
        'cpu_name'       : config.cpu_name,       # Raw CPU name as per py-cpuinfo
        'os_name'        : config.os_name,        # Should be Windows, Linux, or Darwin
        'os_ver'         : config.os_ver,         # Release version of the OS
        'python_ver'     : config.python_ver,     # Python version running the Client
        'mac_address'    : config.mac_address,    # Used to softly verify the Machine IDs
        'machine_token'  : load_machine_token(),  # Stable per-instance id, dedupes re-registration
        'logical_cores'  : config.logical_cores,  # Logical cores, to differentiate hyperthreads
        'physical_cores' : config.physical_cores, # Physical cores, to differentiate hyperthreads
        'hard_cpu_affinity': config.hard_cpu_affinity, # 物理コア単位の固定が可能か
        'ram_total_mb'   : config.ram_total_mb,   # Total RAM on the system, to avoid over assigning
        'machine_id'     : config.machine_id,     # Assigned value, or None. Will be replaced if wrong
        'machine_name'   : config.identity,       # Optional pseudonym for the machine, otherwise None
        'concurrency'    : config.threads,        # Threads to use to play games
        'sockets'        : config.sockets,        # Match runner copies, usually equal to Socket count
        'syzygy_max'     : config.syzygy_max,     # Whether or not the machine has Syzygy support
        'noisy'          : config.noisy,          # Whether our results are unstable for time-based workloads
        'focus'          : config.focus,          # List of engines we have a preference to help
        'cxx_comp'       : config.cxx_comp,       # C++ Compiler used to build Fastchess binaries
        'fastchess_ver'  : config.fastchess_ver,  # Fastchess Version, set during server_configure_fastchess()
        'shogitest_ver'  : config.shogitest_ver,  # Shogitest Version, set during server_configure_shogitest()
        'client_ver'     : CLIENT_VERSION,        # Version of the Client, which the server may reject
    }

    payload = {
        'username'    : config.username,
        'password'    : config.password,
        'system_info' : json.dumps(system_info),
    }

    # Send all of this to the server, and get a Machine Id + Secret Token
    target   = utils.url_join(config.server, 'clientWorkerInfo')
    response = requests.post(target, data=payload, timeout=TIMEOUT_HTTP).json()

    # Throw all the way back to the client.py
    if 'Bad Client Version' in response.get('error', ''):
        raise BadVersionException();

    # 認証拒否 (キーの無効化/削除) はリトライしても直らないので終了する
    if 'error' in response:
        shutdown_if_revoked(response)

    # The 'error' header is included if there was an issue
    if 'error' in response:
        raise utils.OpenBenchFatalWorkerException(response['error'])

    # Store machine_id, and the secret for this session
    config.machine_id   = response['machine_id']
    config.secret_token = response['secret']

def server_request_workload(config):

    print('\nRequesting Workload from Server...')

    payload  = { 'machine_id' : config.machine_id, 'secret' : config.secret_token, 'blacklist' : config.blacklist }
    target   = utils.url_join(config.server, 'clientGetWorkload')
    response = requests.post(target, data=payload, timeout=TIMEOUT_HTTP)

    # Server errors produce garbage back, which we should not alarm a user with
    try: response = response.json()
    except json.decoder.JSONDecodeError:
        raise utils.OpenBenchBadServerResponseException() from None

    # Throw all the way back to the client.py
    if 'Bad Client Version' in response.get('error', ''):
        raise BadVersionException();

    # キーが無効化/削除されたら、ポーリングを続けず終了する
    if 'error' in response:
        shutdown_if_revoked(response)

    # Something very bad happened. Re-initialize the Client
    if 'error' in response:
        raise utils.OpenBenchFatalWorkerException(response['error'])

    # Log the start of a new Workload
    if 'workload' in response:
        dev_engine  = response['workload']['test']['dev' ]['engine']
        dev_name    = response['workload']['test']['dev' ]['name'  ]
        base_engine = response['workload']['test']['base']['engine']
        base_name   = response['workload']['test']['base']['name'  ]
        print('Workload [%s] %s vs [%s] %s\n' % (dev_engine, dev_name, base_engine, base_name))

    config.workload = response.get('workload', None)


def complete_workload(config):

    # Download the opening book, throws an exception on corruption
    utils.download_opening_book(
        config.workload['test']['book']['sha'   ],
        config.workload['test']['book']['source'],
        config.workload['test']['book']['name'  ],
    )

    # Download each NNUE file, throws an exception on corruption
    dev_network  = safe_download_network_weights(config, 'dev' )
    base_network = safe_download_network_weights(config, 'base')

    # Build or download each engine, or exit if an error occured
    dev_name  = safe_download_engine(config, 'dev' , dev_network )
    base_name = safe_download_engine(config, 'base', base_network)

    # Datagen creates a book on-the-fly
    if config.workload['test']['type'] == 'DATAGEN':
        safe_create_genfens_opening_book(config, dev_name, dev_network)

    # Scale time control based on the Engine's local NPS
    scale_factor = determine_scale_factor(config, dev_name, dev_network, base_name, base_network)

    # SPSA は rshogi の spsa チューナーを 1 コピー実行する専用経路へ
    if config.workload['test']['type'] == 'SPSA':
        return complete_spsa_workload(config, dev_name, scale_factor)

    # Server knows how many copies of the match runner we should run
    runner_cnt      = config.workload['distribution']['runner-count']
    concurrency_per = config.workload['distribution']['concurrency-per']
    games_per       = config.workload['distribution']['games-per-runner']

    print () # Record this information
    print ('%d match runner copies' % (runner_cnt))
    print ('%d concurrent games per copy' % (concurrency_per))
    print ('%d total games per match runner copy\n' % (games_per))

    # Launch and manage all of the match runner workers
    with ThreadPoolExecutor(max_workers=runner_cnt) as executor:

        timestamp  = time.time()
        results    = multiprocessing.Queue()
        abort_flag = threading.Event()

        tasks = [] # Create each of the match runner workers
        for x in range(runner_cnt):
            cmd = build_runner_command(config, dev_name, base_name, scale_factor, timestamp, x)
            tasks.append(executor.submit(run_and_parse_runner, config, cmd, x, results, abort_flag))

        pgn_files = [MatchRunner.pgn_name(config, timestamp, x) for x in range(runner_cnt)]
        pgn_reporter = PGNArchiveReporter(config, pgn_files, scale_factor)

        # Process the Queue until we exit, finish, or are told to stop by the server
        try:
            rr = ResultsReporter(config, tasks, results, abort_flag, pgn_reporter)
            rr.process_until_finished()
            MatchRunner.kill_everything(dev_name, base_name)
            # 対局プロセスが棋譜ファイルを閉じてから、EOFを最終局として扱う。
            wait(tasks)
            rr.send_errors(timestamp, runner_cnt)
            pgn_reporter.checkpoint(final=True)

        # Kill everything during an Exception, but print it.
        # SystemExit (キー失効による自己終了) でも対局を残さない
        except (Exception, KeyboardInterrupt, SystemExit):
            abort_flag.set()
            MatchRunner.kill_everything(dev_name, base_name)
            raise

def safe_download_network_weights(config, branch):

    # Wraps utils.py:download_network()
    # May raise utils.OpenBenchCorruptedNetworkException

    engine   = config.workload['test'][branch]['engine' ]
    net_name = config.workload['test'][branch]['netname']
    net_sha  = config.workload['test'][branch]['network']
    aux_list = config.workload['test'][branch].get('network_aux_files', [])
    net_path = os.path.join('Networks', net_sha)

    # Not all engines use Network files
    if not net_sha or net_sha == 'None':
        return None

    credentials = (config.server, config.username, config.password)
    utils.download_network(*credentials, engine, net_name, net_sha, net_path)

    # Auxiliary files (eg progress.bin, eval_options.txt), addressed via
    # the main Network and fetched by their original filename
    for aux in aux_list:
        aux_path = os.path.join('Networks', aux['sha'])
        endpoint = 'api/networks/%s/%s/aux/%s' % (engine, net_sha, aux['name'])
        utils.download_network(
            *credentials, engine, '%s (%s)' % (net_name, aux['name']), aux['sha'], aux_path, endpoint)

    return net_path

def safe_download_engine(config, branch, net_path):

    # Wraps utils.py:download_public_engine() and utils.py:download_private_engine()

    engine      = config.workload['test'][branch]['engine']
    branch_name = config.workload['test'][branch]['name']
    commit_sha  = config.workload['test'][branch]['sha']
    source      = config.workload['test'][branch]['source']
    private     = config.workload['test'][branch]['private']
    build_args  = config.workload['test'][branch].get('build_args', '')

    # SPSA の TUNE ビルド: .tune キットをビルド前にソースへ注入する。
    # キットが違えば別バイナリなので、キャッシュ名にもハッシュを含める
    tune     = config.workload['test'][branch].get('tune')
    tune_sha = tune['sha'] if tune else ''

    bin_name = utils.engine_binary_name(engine, commit_sha, net_path, private, build_args, tune_sha)
    out_path = os.path.join('Engines', bin_name)

    if private:

        try:
            return utils.download_private_engine(
                engine, branch_name, source, out_path, config.cpu_name, config.cpu_flags)

        except utils.OpenBenchMissingArtifactException as error:
            ServerReporter.report_missing_artifact(config, branch, error.name, error.logs)
            raise

    else:

        make_path  = config.workload['test'][branch]['build']['path']
        alt_binary = config.workload['test'][branch]['build'].get('binary', '')
        compiler   = config.compilers[engine][0]
        source_request = None

        if source.startswith(utils.PRIVATE_SOURCE_PREFIX):
            source_request = {
                'server'  : config.server,
                'payload' : {
                    'machine_id' : config.machine_id,
                    'secret'     : config.secret_token,
                    'test_id'    : config.workload['test']['id'],
                    'side'       : branch,
                },
            }

        try:
            return utils.download_public_engine(
                engine, net_path, branch_name, source, make_path, out_path,
                compiler, build_args, alt_binary, tune, source_request)

        except utils.OpenBenchBuildFailedException as error:

            print ('Failed to build %s-%s...\n\nCompiler Output:' % (engine, branch_name))
            for line in error.logs.split('\n'):
                print ('> %s' % (line))
            print ()

            config.blacklist.append(config.workload['test']['id'])
            ServerReporter.report_build_fail(config, branch, error.logs)
            raise

def safe_create_genfens_opening_book(config, dev_name, dev_network):

    with open(os.path.join('Books', 'openbench.genfens.epd'), 'w') as fout:

        args = {
            'N'       : genfens.genfens_required_openings_each(config),
            'book'    : genfens.genfens_book_input_name(config),
            'seeds'   : config.workload['test']['genfens_seeds'],
            'extra'   : config.workload['test']['genfens_args'],
            'private' : config.workload['test']['dev']['private'],
            'engine'  : os.path.join('Engines', dev_name),
            'network' : dev_network,
            'threads' : config.threads,
            'output'  : fout,
        }

        try: genfens.create_genfens_opening_book(args)

        except utils.OpenBenchFailedGenfensException as error:
            ServerReporter.report_engine_error(config, error.message)
            raise

def staged_network_dir(config, branch):

    ## The directory stage_network_options() stages this branch's Network
    ## into (absolute), or '' when the branch has no directory-style Network

    test = config.workload['test'][branch]
    if test['private'] or not test['build'].get('network_filename'):
        return ''
    if not test['network'] or test['network'] == 'None':
        return ''
    return os.path.abspath(os.path.join('Networks', '%s-dir' % (test['network'])))

def file_sha256_prefix(path, length=8):

    hasher = hashlib.sha256()
    with open(path, 'rb') as fin:
        while chunk := fin.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()[:length].upper()

def stage_hashed_file(source_path, staged_path, expected_sha):

    expected = expected_sha.upper()

    if os.path.exists(staged_path):
        found = file_sha256_prefix(staged_path, len(expected))
        if found == expected:
            return
        print ('Replacing staged file %s: expected %s, found %s' % (
            staged_path, expected, found))
        os.remove(staged_path)

    try:
        os.link(source_path, staged_path)
    except OSError:
        shutil.copyfile(source_path, staged_path)

    found = file_sha256_prefix(staged_path, len(expected))
    if found != expected:
        os.remove(staged_path)
        raise utils.OpenBenchCorruptedNetworkException(
            'Invalid SHA for staged file %s' % (staged_path))

def stage_network_options(config, branch, prefix=''):

    ## Returns [(option, value)] pairs pointing a public engine at its
    ## Network files at runtime (build.network_option engines), staging
    ## directory-style files (build.network_filename) as needed. Paths are
    ## passed as absolute: engines disagree on how to resolve relative
    ## ones (older YaneuraOu uses the working directory, newer ones the
    ## executable's directory), and absolute paths satisfy them all.

    test       = config.workload['test'][branch]
    build_conf = test['build']
    network    = test['network']
    private    = test['private']
    net_option = build_conf.get('network_option')
    net_fname  = build_conf.get('network_filename')

    if private or not net_option or not network or network == 'None':
        return []

    if not net_fname:
        return [(net_option, os.path.abspath(os.path.join('Networks', network)))]

    # Directory-style engines (YaneuraOu's EvalDir) expect a fixed file
    # name inside a directory: stage Networks/<sha>-dir/<name>
    dir_path = os.path.join('Networks', '%s-dir' % (network))
    os.makedirs(dir_path, exist_ok=True)
    staged = os.path.join(dir_path, net_fname)
    stage_hashed_file(os.path.join('Networks', network), staged, network)

    pairs = [(net_option, os.path.abspath(dir_path))]

    # Every auxiliary file goes next to the Network under its original
    # name. Files with an entry in build.network_aux_options additionally
    # get their path passed as that USI option (eg progress.bin ->
    # ProgressFilePath). A file named eval_options.txt is special: each
    # "Name=Value" line becomes a setoption, so per-eval mandatory
    # settings travel with the Network and can never be forgotten
    aux_options = build_conf.get('network_aux_options', {})
    extra_pairs = []

    for aux in test.get('network_aux_files', []):

        staged_aux = os.path.join(dir_path, aux['name'])
        stage_hashed_file(os.path.join('Networks', aux['sha']), staged_aux, aux['sha'])

        if aux['name'] in aux_options:
            pairs.append((aux_options[aux['name']], os.path.abspath(staged_aux)))

        if aux['name'].lower() == 'eval_options.txt':
            extra_pairs += parse_eval_options_file(staged_aux)

    # The worker owns the file-location options: an eval_options.txt that
    # tries to override them (eg EvalDir=eval) would silently point the
    # engine at a wrong eval, so the workload must stop before launching.
    managed = { name.lower() for name, value in pairs }
    managed.update({ 'evaldir', 'evalfile', 'ls_progress_coeff', 'progressfilepath' })
    for option in aux_options.values():
        managed.add(option.lower())

    for name, value in extra_pairs:
        if name.lower() in managed:
            raise utils.OpenBenchCorruptedNetworkException(
                'eval_options.txt may not set managed path option %s' % (name))
        pairs.append((name, value.replace('{DIR}', os.path.abspath(dir_path))))

    return pairs

def parse_eval_options_file(path):

    ## "Name=Value" or "Name Value" lines from a Network's eval_options.txt, applied to
    ## the engine at both bench and game time. '#' starts a comment.
    ## Values must not contain spaces: the match runner splits on them.

    pairs = []
    with open(path, encoding='utf-8-sig') as fin:
        for line in fin:
            line = line.split('#')[0].strip()
            if not line:
                continue
            if '=' in line:
                name, value = line.split('=', 1)
            elif ' ' in line:
                name, value = line.split(None, 1)
            else:
                print ('Ignoring malformed eval_options.txt line: %s' % (line))
                continue

            name, value = name.strip(), value.strip()
            if not name or not value or ' ' in name or ' ' in value:
                print ('Ignoring malformed eval_options.txt line: %s' % (line))
                continue
            pairs.append((name, value))

    return pairs

def safe_run_benchmarks(config, branch, engine, network):

    name       = config.workload['test'][branch]['name']
    private    = config.workload['test'][branch]['private']
    expected   = int(config.workload['test'][branch]['bench'])
    bench_args = config.workload['test'][branch]['build'].get('bench_args', '')
    binary     = os.path.join('Engines', engine)

    # Engines that take their Network as a runtime option need it for the
    # bench as well; the bench runs from the Client root (prefix '')
    usi_options = stage_network_options(config, branch, prefix='')
    for opt_name, opt_value in usi_options:
        print ('Bench option for %s: %s = %s' % (name, opt_name, opt_value))

    # Optional engine-config patterns that turn warnings in the bench
    # output (eg a wrong-architecture eval file) into visible failures
    fatal_patterns = config.workload['test'][branch]['build'].get('bench_fatal_patterns', [])

    try:
        print('\nRunning %dx Benchmarks for %s' % (config.threads, name))
        speed, nodes = bench.run_benchmark(
            binary, network, private, config.threads, 1, expected, bench_args, usi_options, fatal_patterns)

    except utils.OpenBenchBadBenchException as error:
        ServerReporter.report_bad_bench(config, error.message)
        raise

    print('Bench for %s is %d' % (name, nodes))
    print('Speed for %s is %d' % (name, speed))
    return speed


def collect_spsa_usi_options(config):

    ## rshogi spsa に --usi-option として渡す (name, value) の一覧。
    ## Network 由来の必須設定 (EvalDir / progress / eval_options.txt) を先に置き、
    ## テスト作成時のオプション欄がそれ以外を上書きできるようにする。
    ## Threads / Hash は rshogi のフラグ (--threads / --hash-mb) 側で渡すので除く

    staged  = stage_network_options(config, 'dev', prefix='')
    managed = { name.lower() for name, value in staged }

    options    = config.workload['test']['dev']['options']
    staged_dir = staged_network_dir(config, 'dev')
    if '{DIR}' in options:
        if not staged_dir:
            print ('Warning: {DIR} used, but dev has no staged network directory')
        options = options.replace('{DIR}', staged_dir)

    pairs = list(staged)
    for token in re.findall(r'"[^"]*"|\S+', options):

        if '=' not in token:
            print ('Ignoring malformed option token: %s' % (token))
            continue

        name, value = token.split('=', 1)

        # rshogi へはフラグで渡す
        if name.lower() in ('threads', 'hash', 'usi_hash'):
            continue

        # ファイル位置系はワーカーが所有する (存在しない eval を指す事故の防止)
        if name.lower() in managed:
            print ('Ignoring option %s: managed by the worker' % (name))
            continue

        pairs.append((name, value.strip('"')))

    return pairs

def complete_spsa_workload(config, dev_name, scale_factor):

    ## SPSA (rshogi ラッパー)。対局・SPSA スケジュール・θ 更新はすべて rshogi の
    ## spsa チューナーが担い、ワーカーは起動・監視・進捗報告だけを行う

    threads = int(extract_option(config.workload['test']['dev']['options'], 'Threads') or 1)
    hash_mb = int(extract_option(config.workload['test']['dev']['options'], 'Hash') or 16)

    engine_path = os.path.abspath(os.path.join('Engines', dev_name))
    book_path   = os.path.abspath(os.path.join('Books', config.workload['test']['book']['name']))
    usi_options = collect_spsa_usi_options(config)

    spsa_rshogi.run_workload(
        config, ServerReporter, engine_path, usi_options, book_path,
        threads, hash_mb, scale_factor)

def extract_option(options, option):

    if (match := re.search('(?<=%s=")[^"]*' % (option), options)):
        return match.group()

    if (match := re.search('(?<=%s=\')[^\']*' % (option), options)):
        return match.group()

    if (match := re.search('(?<=%s=)[^ ]*' % (option), options)):
        return match.group()

def linux_physical_cpu_ids():

    get_affinity = getattr(os, 'sched_getaffinity', None)
    if platform.system() != 'Linux' or get_affinity is None:
        raise RuntimeError('Linux sched_getaffinity() is required for Ponder CPU isolation')

    allowed_cpus = sorted(get_affinity(0))
    if not allowed_cpus:
        raise RuntimeError('The worker process has no allowed CPUs')

    representatives = {}
    for cpu in allowed_cpus:
        topology_dir = '/sys/devices/system/cpu/cpu%d/topology' % cpu
        try:
            with open(os.path.join(topology_dir, 'physical_package_id')) as fin:
                package_id = int(fin.read().strip())
            with open(os.path.join(topology_dir, 'core_id')) as fin:
                core_id = int(fin.read().strip())
        except (OSError, ValueError) as error:
            raise RuntimeError(
                'Unable to determine physical topology for CPU %d: %s' % (cpu, error))

        representatives.setdefault((package_id, core_id), cpu)

    return [representatives[key] for key in sorted(representatives)]

def build_runner_command(config, dev_cmd, base_cmd, scale_factor, timestamp, runner_idx):

    flags  = ' ' + MatchRunner.basic_settings(config)
    flags += ' ' + MatchRunner.concurrency_settings(config)
    affinity = MatchRunner.affinity_settings(config, runner_idx)
    if affinity:
        flags += ' ' + affinity
    flags += ' ' + MatchRunner.adjudication_settings(config)
    flags += ' ' + MatchRunner.engine_settings(config, dev_cmd, 'dev', scale_factor, runner_idx)
    flags += ' ' + MatchRunner.engine_settings(config, base_cmd, 'base', scale_factor, runner_idx)
    flags += ' ' + MatchRunner.book_settings(config, runner_idx)
    flags += ' ' + MatchRunner.pgnout_settings(config, timestamp, runner_idx)

    return MatchRunner.executable(config) + flags

def run_and_parse_runner(config, command, runner_idx, results_queue, abort_flag):

    print('\n[#%d] Launching match runner...\n%s\n' % (runner_idx, command))
    runner = Popen(command.split(), stdout=PIPE)

    results = {

        'trinomial'   : [0, 0, 0],       # LDW
        'pentanomial' : [0, 0, 0, 0, 0], # LL DL DD DW WW
        'games'       : {},              # game_id : result_str

        'crashes'     : 0,               # " disconnect" or "connection stalls"
        'timelosses'  : 0,               # " loses on time "
        'illegals'    : 0,               # " illegal move "
    }

    while True:

        # Read each line of output until the pipe closes and we get "" back
        line = runner.stdout.readline().strip().decode('utf-8', 'replace')
        if not line:
            break

        if abort_flag.is_set():
            break

        if 'Started game' not in line and 'Score of' not in line:
            print('[#%d] %s' % (runner_idx, line))

        if 'Finished game' in line:
            MatchRunner.update_results(results, line)

        # Add to the results queue every time we have a game-pair finished
        if any(results['pentanomial']):

            # Place the results into the Queue, and be sure to copy the lists
            results_queue.put({
                'trinomial'     : list(results['trinomial']),
                'pentanomial'   : list(results['pentanomial']),
                'crashes'       : results['crashes'],
                'timelosses'    : results['timelosses'],
                'illegals'      : results['illegals'],
                'runner_idx'    : runner_idx,
            })

            # Clear out all the results, so we can start collecting a new set
            results['trinomial'  ] = [0, 0, 0]
            results['pentanomial'] = [0, 0, 0, 0, 0]
            results['crashes'    ] = 0
            results['timelosses' ] = 0
            results['illegals'   ] = 0

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                           #
#                                                                           #
#                                                                           #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def reload_local_imports():

    import bench
    import genfens
    import pgn_util
    import spsa_rshogi
    import utils

    importlib.reload(bench)
    importlib.reload(genfens)
    importlib.reload(pgn_util)
    importlib.reload(spsa_rshogi)
    importlib.reload(utils)

def parse_arguments(client_args):

    # Pretty formatting
    p = argparse.ArgumentParser(
        formatter_class=lambda prog:
            argparse.ArgumentDefaultsHelpFormatter(prog, max_help_position=10)
    )

    # Arguments specific to worker.py
    p.add_argument('-T', '--threads' , help='Total Threads'               , required=True      )
    p.add_argument('-N', '--nsockets', help='Number of Sockets'           , required=True      )
    p.add_argument('-I', '--identity', help='Machine pseudonym'           , required=False     )
    p.add_argument(      '--syzygy'  , help='Syzygy WDL'                  , required=False     )
    p.add_argument(      '--fleet'   , help='Fleet Mode'                  , action='store_true')
    p.add_argument(      '--noisy'   , help='Reject time-based workloads' , action='store_true')
    p.add_argument(      '--focus'   , help='Prefer certain engine(s)'    , nargs='+'          )

    # Ignore unknown arguments ( from client )
    worker_args, unknown = p.parse_known_args()

    # Add the client args (Username, Password, and Server) to the worker args
    return argparse.Namespace(**{ **vars(client_args), **vars(worker_args) })

def run_openbench_worker(client_args):

    # If the client was updated, we must reload everything
    reload_local_imports()

    fastchess_error  = '[Note] Unable to locate and/or build desired Fastchess version!'
    shogitest_error  = '[Note] Unable to locate and/or build desired Shogitest version!'
    setup_error      = '[Note] Unable to establish initial connection with the Server!'
    connection_error = '[Note] Unable to reach the server to request a workload!'

    args   = parse_arguments(client_args) # Merge client.py and worker.py args
    config = Configuration(args)          # Holds System info, args, and Workload info

    # 二重起動の防止 (接続リトライで積み上がった古い起動ループ対策)
    acquire_single_instance_lock()

    # サーバとクライアントのバージョンが噛み合うまで登録しない
    # (サーバのデプロイ待ちで Machine 登録が無限に増えるのを防ぐ)
    wait_for_server_version(config)

    try_forever(server_configure_fastchess, [config], fastchess_error)
    try_forever(server_configure_shogitest, [config], shogitest_error)
    try_forever(server_configure_worker, [config], setup_error)

    if IS_LINUX:
        set_runner_permissions()

    # openbench.exit はここでは消さない: 消してしまうと、契約を知らない
    # supervisor (独自 systemd 等) が再起動したときに停止指示が静かに
    # 無効化される。解除は setup_worker.sh (再接続) が行う

    while True:

        # Check for exit signal via openbench.exit. Exit code 66 stops the
        # setup_worker.sh restart loop as well, so this is a full stop
        if os.path.isfile('openbench.exit'):
            print('Exited via openbench.exit')
            stop_detached_spsa()
            terminate_legacy_wrapper()
            sys.exit(EXIT_SHUTDOWN)

        try:
            # Cleanup on each workload request
            cleanup_client()

            # Keep asking for a workload until we get a response
            try_forever(server_request_workload, [config], connection_error)

            # Complete the workload if there was work to be done
            if config.workload: complete_workload(config)

            # Otherwise --fleet workers will exit when there is no work
            elif config.fleet: time.sleep(TIMEOUT_ERROR); sys.exit()

            # In either case, wait before requesting again
            else: time.sleep(TIMEOUT_WORKLOAD)

        # Caught by client.py, prompting a Client Update
        except BadVersionException:
            raise BadVersionException()

        # Fatal error, fully restart the Worker
        except utils.OpenBenchFatalWorkerException:
            traceback.print_exc()
            time.sleep(TIMEOUT_ERROR)
            config = Configuration(args)
            try_forever(server_configure_worker, [config], setup_error)

        except Exception:
            traceback.print_exc()
            time.sleep(TIMEOUT_ERROR)
