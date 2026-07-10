# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
#   OpenBench is a chess engine testing framework authored by Andrew Grant.   #
#   <https://github.com/AndyGrant/OpenBench>           <andrew@grantnet.us>   #
#                                                                             #
#   OpenBench is free software: you can redistribute it and/or modify         #
#   it under the terms of the GNU General Public License as published by      #
#   the Free Software Foundation, either version 3 of the License, or         #
#   (at your option) any later version.                                       #
#                                                                             #
#   OpenBench is distributed in the hope that it will be useful,              #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of            #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the             #
#   GNU General Public License for more details.                              #
#                                                                             #
#   You should have received a copy of the GNU General Public License         #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# Module serves a singular purpose, to invoke:
# >>> get_workload(Machine)
#
# Refer to: https://github.com/AndyGrant/OpenBench/wiki/Workload-Assignment

import random
import re
import sys

import OpenBench.utils

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import Network, Result, Test

def network_aux_files(engine, sha):

    # Every auxiliary file travelling with a Network (eg progress.bin,
    # eval_options.txt), as [{name, sha}] for the worker to stage
    if not sha or sha == 'None':
        return []
    network = Network.objects.filter(engine=engine, sha256=sha).first()
    if not network:
        return []
    return [{ 'name' : aux.name, 'sha' : aux.sha256 } for aux in network.aux_files.all()]

from django.db import transaction

SHUTDOWN_ERROR = 'Worker key disabled or deleted. Shut down.'

def get_workload(request, machine):

    # Machines stopped from the /workers/ page receive no new work, but may
    # be resumed later, so the worker just keeps idling
    if machine.info.get('stop_requested'):
        return {}

    # A deleted or disabled Worker Key is permanent: tell the worker
    # explicitly so it can shut itself down (including its restart wrapper),
    # instead of polling forever with dead credentials
    if OpenBench.utils.machine_key_revoked(machine):
        return { 'error' : SHUTDOWN_ERROR }

    # Select a workload from the possible ones, if we can
    if not (test := select_workload(request, machine)):
        return {}

    # Avoid creating duplicate Result objects
    result, created = Result.objects.get_or_create(test=test, machine=machine)

    # Update the Machine's status and save everything
    machine.workload = test.id;
    machine.mnps = machine.dev_mnps = machine.base_mnps = 0.00
    machine.save(); result.save()

    return { 'workload' : workload_to_dictionary(test, result, machine) }

def select_workload(request, machine):

    # Step 1: Refine active workloads to the candidate assignments
    candidates, has_focus = filter_valid_workloads(request, machine)
    if not candidates:
        return None

    # Step 2: Count relevant threads on each candidate test
    worker_dist, engine_freq = compute_resource_distribution(candidates, machine, has_focus)

    # Step 3: Determine the effective-throughput for each workload
    if OPENBENCH_CONFIG['balance_engine_throughputs']:
        for id, data in worker_dist.items():
            data['throughput'] = data['throughput'] / engine_freq[data['engine']]

    # Step 4: Compute the Resource Ratios for each of the workloads, if we were assigned
    for id, data in worker_dist.items():
        data['ratio'] = (data['threads'] + machine.info['concurrency']) / data['throughput']

    # Step 5: Compute the idealized "Fair-Ratio" once our machine is added
    min_ratio      = min(x['ratio'] for x in worker_dist.values())
    thread_sum     = sum(x['threads'] for x in worker_dist.values()) + machine.info['concurrency']
    throughput_sum = sum(x['throughput'] for x in worker_dist.values())
    fair_ratio     = thread_sum / throughput_sum

    # Step 6: Repeat the same machine, if we are still within +- 25% fairness
    if machine.workload in worker_dist.keys():
        this_ratio = worker_dist[machine.workload]['ratio']
        if min_ratio / fair_ratio > 0.75 and this_ratio / fair_ratio < 1.25:
            return Test.objects.get(id=machine.workload)

    # Step 7: Pick a random test, amongst those who share the min_ratio, weighted by throughput
    choices = [id for id, data in worker_dist.items() if data['ratio'] == min_ratio]
    weights = [data['throughput'] for id, data in worker_dist.items() if data['ratio'] == min_ratio]
    return Test.objects.get(id=random.choices(choices, weights=weights)[0])

def filter_valid_workloads(request, machine):

    workloads = OpenBench.utils.get_active_tests()

    # Skip workloads whose engine was removed or renamed in the config,
    # since we can no longer look up how to build or run them
    known_engines = list(OPENBENCH_CONFIG['engines'].keys())
    workloads = workloads.filter(dev_engine__in=known_engines, base_engine__in=known_engines)

    # Skip engines that the Machine cannot handle
    for engine in OPENBENCH_CONFIG['engines'].keys():
        if engine not in machine.info['supported']:
            workloads = workloads.exclude(dev_engine=engine)
            workloads = workloads.exclude(base_engine=engine)

    # Skip workloads that are blacklisted on the machine
    if blacklisted := request.POST.getlist('blacklist'):
        workloads = workloads.exclude(id__in=blacklisted)

    # Skip workloads with unmet Syzygy requirements
    for K in range(machine.info['syzygy_max'] + 1, 10):
        workloads = workloads.exclude(syzygy_adj='%d-MAN' % (K))
        workloads = workloads.exclude(syzygy_wdl='%d-MAN' % (K))

    # Skip any workload using, or measuring, Time, for --noisy workers
    if machine.info.get('noisy'):
        workloads = [x for x in workloads if not OpenBench.utils.workload_uses_time_based_tc(x)]

    # Skip SPSA workloads this machine cannot serve (rshogi ラッパーの制約)
    workloads = [x for x in workloads if valid_spsa_assignment(x, machine)]

    # Skip workloads that we have insufficient threads to play
    options = [x for x in workloads if valid_hardware_assignment(x, machine)]

    # Possible that no work exists for the machine
    if not options:
        return [], False

    # Refine to workloads of the highest priority
    priorities = [x.priority for x in options]
    candidates = [x for x in options if x.priority == max(priorities)]

    # Refine to workloads that match our focus, if applicable
    focuses    = machine.info.get('focus', [])
    has_focus  = any(x.dev_engine in focuses for x in candidates)

    if has_focus:
        candidates = list(filter(lambda x: x.dev_engine in focuses, candidates))

    return candidates, has_focus

def valid_spsa_assignment(workload, machine):

    ## SPSA (rshogi ラッパー) は 1 台のワーカーが rshogi の spsa を丸ごと実行する。
    ## - 旧スキーマ (分散SPSA) のレコードは新ワーカーでは実行できないので配らない
    ## - rshogi のビルド・実行は Linux ワーカーのみサポート
    ## - 既に他のマシンが走らせている間は誰にも配らない (単一ランナー専有)

    if workload.test_mode != 'SPSA':
        return True

    if not isinstance(workload.spsa, dict) or workload.spsa.get('wrapper') != 'RSHOGI':
        return False

    if machine.info.get('os_name') != 'Linux':
        return False

    for other in OpenBench.utils.getRecentMachines(minutes=3):
        if other.id != machine.id and other.workload == workload.id:
            return False

    return True

def valid_hardware_assignment(workload, machine):

    # Extract thread requirements from the workload itself
    dev_threads  = int(OpenBench.utils.extract_option(workload.dev_options,  'Threads'))
    base_threads = int(OpenBench.utils.extract_option(workload.base_options, 'Threads'))

    # Extract the information from our machine
    threads      = machine.info['concurrency']
    hyperthreads = machine.info['physical_cores'] < threads

    # For core-odds tests, disable hyperthreads, by halving the thread count
    if hyperthreads and dev_threads != base_threads:
        threads = threads // 2

    # SPSA plays a pair at a time, not a game at a time
    is_spsa = workload.test_mode == 'SPSA'

    # Refuse if there are not enough threads for the test
    if (1 + is_spsa) * max(dev_threads, base_threads) > threads:
        return False

    # All Criteria have been met
    return True

def compute_resource_distribution(workloads, machine, has_focus):

    # Return a thread count, and engine name for each workload, as well as the throughput.
    # The throughput may be scaled down later, due to balance_engine_throughputs

    worker_dist = {
        workload.id : { 'threads' : 0, 'engine' : workload.dev_engine, 'throughput' : workload.throughput }
            for workload in workloads
    }

    # Ignore our own machine;
    # Ignore machines working on non-candidates;
    # Ignore focus-assigned machines when has_focus is false

    for x in OpenBench.utils.getRecentMachines():
        if x != machine and x.workload in worker_dist:
            if has_focus or worker_dist[x.workload]['engine'] not in x.info.get('focus', []):
                worker_dist[x.workload]['threads'] += x.info['concurrency']

    # Count of tests that exist for a particular dev_engine

    engine_freq = {}
    for workload in workloads:
        engine_freq[workload.dev_engine] = engine_freq.get(workload.dev_engine, 0) + 1

    return worker_dist, engine_freq

def workload_to_dictionary(test, result, machine):

    # HACK: Remove this after a while, to avoid a complex DB migration
    if test.scale_nps == 0:
        test.scale_nps = OPENBENCH_CONFIG['engines'][test.base_engine]['nps']
        test.save()

    workload = {}

    workload['result'] = {
        'id'  : result.id,
    }

    workload['test'] = {
        'id'            : test.id,
        'type'          : test.test_mode,
        'syzygy_wdl'    : test.syzygy_wdl,
        'syzygy_adj'    : test.syzygy_adj,
        'win_adj'       : test.win_adj,
        'draw_adj'      : test.draw_adj,
        'workload_size' : test.workload_size,
        'upload_pgns'   : test.upload_pgns,
        'genfens_args'  : test.genfens_args,
        'play_reverses' : test.play_reverses,
        'scale_method'  : test.scale_method,
        'scale_nps'     : test.scale_nps,
    }

    workload['test']['book'] = {
        'name'   : test.book_name,
        'sha'    : OPENBENCH_CONFIG['books'].get(test.book_name, { 'sha'    : None })['sha'   ],
        'source' : OPENBENCH_CONFIG['books'].get(test.book_name, { 'source' : None })['source'],
    }

    workload['test']['dev'] = {
        'id'           : test.dev.id,
        'name'         : test.dev.name,
        'source'       : test.dev.source,
        'sha'          : test.dev.sha,
        'bench'        : test.dev.bench,
        'engine'       : test.dev_engine,
        'options'      : test.dev_options,
        'network'      : test.dev_network,
        'network_aux_files' : network_aux_files(test.dev_engine, test.dev_network),
        'netname'      : test.dev_netname,
        'time_control' : test.dev_time_control,
        'build'        : OPENBENCH_CONFIG['engines'][test.dev_engine]['build'],
        'build_name'   : test.dev_build_name,
        'build_args'   : test.dev_build_args,
        'private'      : OPENBENCH_CONFIG['engines'][test.dev_engine]['private'],
    }

    workload['test']['base'] = {
        'id'           : test.base.id,
        'name'         : test.base.name,
        'source'       : test.base.source,
        'sha'          : test.base.sha,
        'bench'        : test.base.bench,
        'engine'       : test.base_engine,
        'options'      : test.base_options,
        'network'      : test.base_network,
        'network_aux_files' : network_aux_files(test.base_engine, test.base_network),
        'netname'      : test.base_netname,
        'time_control' : test.base_time_control,
        'build'        : OPENBENCH_CONFIG['engines'][test.base_engine]['build'],
        'build_name'   : test.base_build_name,
        'build_args'   : test.base_build_args,
        'private'      : OPENBENCH_CONFIG['engines'][test.base_engine]['private'],
    }

    # SPSA (rshogi ラッパー) の TUNE ビルド: ワーカーはビルド前にこの .tune を
    # ソースへ当てて、パラメータを USI option 化したバイナリを作る
    if test.test_mode == 'SPSA' and (kit := (test.spsa or {}).get('tune_kit')):
        tune_payload = {
            'name'        : kit['name'],
            'sha'         : kit['sha'],
            'tune_text'   : kit['tune_text'],
            'params_text' : kit['params_text'],
        }
        workload['test']['dev' ]['tune'] = tune_payload
        workload['test']['base']['tune'] = tune_payload

    workload['distribution'] = game_distribution(test, machine)
    workload['spsa']         = spsa_to_dictionary(test, machine)

    with transaction.atomic():

        test = Test.objects.select_for_update().get(id=test.id)
        workload['test']['book_seed' ] = test.id
        workload['test']['book_index'] = test.book_index

        # SPSA (rshogi ラッパー) は開始局面を rshogi 側が seed から抽選するため、
        # 開始局面インデックスを消費しない
        if test.test_mode != 'SPSA':

            runner_cnt    = workload['distribution']['runner-count']
            pairs_per_cnt = workload['distribution']['games-per-runner'] // 2
            per_opening   = 2 if (test.test_mode == 'DATAGEN' and not test.play_reverses) else 1

            test.book_index += runner_cnt * pairs_per_cnt * per_opening

        if test.test_mode == 'DATAGEN':
            workload['test']['genfens_seeds'] = [
                random.randint(0, 2**31 - 1) for x in range(machine.info['concurrency'])]

        test.save()

    return workload

def spsa_to_dictionary(test, machine):

    ## rshogi ラッパーの SPSA 設定一式。ワーカーはこれをそのまま rshogi の
    ## spsa コマンドラインへ変換する。carry は「以前のラン (別マシン含む) までの
    ## 累計」で、途中から引き継ぐ場合の累計報告のベースになる

    if test.test_mode != 'SPSA':
        return None

    spsa     = test.spsa
    progress = spsa.get('progress', {}) or {}

    # 1 batch で並列実行できる対局数は 2 × batch_pairs が上限 (rshogi の仕様)
    engine_threads = int(OpenBench.utils.extract_option(test.dev_options, 'Threads') or 1)
    max_games      = machine.info['concurrency'] // max(1, engine_threads)
    concurrency    = max(1, min(max_games, 2 * spsa['batch_pairs']))

    return {
        'wrapper'      : 'RSHOGI',

        'alpha'        : spsa['alpha'],
        'gamma'        : spsa['gamma'],
        'a_ratio'      : spsa['a_ratio'],
        'total_pairs'  : spsa['total_pairs'],
        'batch_pairs'  : spsa['batch_pairs'],
        'seed'         : spsa.get('seed'),

        'active_regex' : spsa.get('active_regex', ''),
        'mapping'      : spsa.get('mapping', 'NONE'),
        'early_stop'   : spsa.get('early_stop', { 'patience' : 0 }),

        'params_text'  : spsa['params_text'],
        'state_params' : spsa.get('state_params', ''),
        'concurrency'  : concurrency,

        'carry' : {
            'pairs'  : progress.get('completed_pairs', 0),
            'games'  : progress.get('total_games', 0),
            'wins'   : test.wins,
            'losses' : test.losses,
            'draws'  : test.draws,
        },
    }

def extract_option(options, option):

    if (match := re.search('(?<=%s=")[^"]*' % (option), options)):
        return match.group()

    if (match := re.search('(?<=%s=\')[^\']*' % (option), options)):
        return match.group()

    if (match := re.search('(?<=%s=)[^ ]*' % (option), options)):
        return match.group()

def game_distribution(test, machine):

    dev_threads  = int(extract_option(test.dev_options, 'Threads'))
    base_threads = int(extract_option(test.base_options, 'Threads'))

    worker_threads = machine.info['concurrency']
    worker_sockets = machine.info['sockets']

    # For core-odds tests, disable hyperthreads, by halving the thread count
    if machine.info['physical_cores'] < worker_threads and dev_threads != base_threads:
        worker_threads = worker_threads // 2

    # Ignore sockets for concurrent match runners, when playing with more than one thread
    if max(dev_threads, base_threads) > 1:
        worker_sockets = 1

    # Max possible concurrent engine games, per copy of match runner
    max_concurrency = (worker_threads // worker_sockets) // max(dev_threads, base_threads)

    # SPSA (rshogi ラッパー) は 1 コピーの rshogi spsa が全対局を回す。
    # 数値は情報表示用で、実際の並列度は spsa_to_dictionary が決める
    if test.test_mode == 'SPSA':
        return {
            'runner-count'     : 1,
            'concurrency-per'  : max_concurrency,
            'games-per-runner' : 2 * test.spsa['total_pairs'],
        }

    return {
        'runner-count'     : worker_sockets,
        'concurrency-per'  : max_concurrency,
        'games-per-runner' : 2 * test.workload_size * max_concurrency,
    }
