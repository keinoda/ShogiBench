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
# >>> create_workload(request, type)
#
# A Workload can be a "TEST", which is an SPRT, or FIXED type.
# A Workload can be a "TUNE", which is an SPSA tuning session
#
# This module will either create the workload and return the user to the index,
# which will display their newly created test. Or it will return them to index,
# with a list of errors that need to be fixed. A warning may also be displayed,
# if the Base branch appears ahead of the Dev branch.

import math

import OpenBench.spsa_params
import OpenBench.tune_kits
import OpenBench.utils
import OpenBench.views

from OpenBench.models import *
from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.workloads.verify_workload import verify_workload

def resolve_build_variant(request, engine_field, variant_field):

    # Returns (variant_name, make_arguments) for the requested engine.
    # A free-form build command in the _custom field takes precedence over
    # the dropdown; both were already validated by verify_workload.

    custom = request.POST.get(variant_field + '_custom', '').strip()
    if custom:
        args, dropped = OpenBench.views.normalize_build_command(custom)
        return 'custom', args

    engine  = request.POST[engine_field]
    variant = request.POST.get(variant_field, 'default')
    args    = OpenBench.views.engine_build_variants(engine).get(variant, '')
    return variant, args

def finalize_workload_creation(request, workload):

    warning = None
    if OpenBench.utils.branch_is_out_of_date(workload):
        warning = 'Consider Rebasing: Dev (%s) appears behind Base (%s)' % (workload.dev.name, workload.base.name)

    username = request.user.username
    profile  = Profile.objects.get(user=request.user)
    summary  = 'CREATE P=%d TP=%d' % (workload.priority, workload.throughput)
    LogEvent.objects.create(author=username, summary=summary, log_file='', test_id=workload.id)

    if not OPENBENCH_CONFIG['use_cross_approval'] and profile.approver:
        workload.approved = True
        workload.save(update_fields=['approved'])

    return warning

def create_workload(request, workload_type):

    assert workload_type in [ 'TEST', 'TUNE', 'DATAGEN' ]

    if not request.user.is_authenticated:
        return OpenBench.views.redirect(request, '/login/', error='Only enabled users can create tests')

    if not Profile.objects.get(user=request.user).enabled:
        return OpenBench.views.redirect(request, '/login/', error='Only enabled users can create tests')

    if request.method == 'GET':

        data = { 'networks' : list(Network.objects.all().values()) }

        # Static json variants merged with user-defined ones, per engine
        data['build_variants'] = {
            engine : OpenBench.views.engine_build_variants(engine)
            for engine in OPENBENCH_CONFIG['engines']
        }

        # .tune キット (SPSA 作成フォームでの選択と .params 自動転記に使う)
        data['tune_kits'] = list(
            TuneKit.objects.all().order_by('engine', 'name')
                .values('id', 'engine', 'name', 'params_text'))

        if workload_type == 'TEST':
            data['workload']        = workload_type
            data['dev_text']        = 'Dev'
            data['dev_title_text']  = 'Dev'
            data['submit_text']     = 'テストを作成'
            data['submit_endpoint'] = '/test/new/'

        if workload_type == 'TUNE':
            data['workload']        = workload_type
            data['dev_text']        = ''
            data['dev_title_text']  = 'エンジン'
            data['submit_text']     = 'SPSAチューニングを作成'
            data['submit_endpoint'] = '/tune/new/'

        if workload_type == 'DATAGEN':
            data['workload']        = workload_type
            data['dev_text']        = 'Dev'
            data['dev_title_text']  = 'Dev'
            data['submit_text']     = 'データ生成を作成'
            data['submit_endpoint'] = '/datagen/new/'

        return OpenBench.views.render(request, 'create_workload.html', data)

    if workload_type == 'TEST':
        workload, errors = create_new_test(request)

    elif workload_type == 'TUNE':
        workload, errors = create_new_tune(request)

    elif workload_type == 'DATAGEN':
        workload, errors = create_new_datagen(request)

    if errors != [] and errors != None:
        paths = { 'TEST' : '/test/new/', 'TUNE' : '/tune/new/', 'DATAGEN' : '/datagen/new/' }
        return OpenBench.views.redirect(request, paths[workload_type], error='\n'.join(errors))

    warning = finalize_workload_creation(request, workload)

    return OpenBench.views.redirect(request, '/index/', warning=warning)

def create_new_test(request):

    # Collects erros, and collects all data from the Github API
    errors, engine_info = verify_workload(request, 'TEST')
    dev_info, dev_has_all = engine_info[0]
    base_ingo, base_has_all = engine_info[1]

    if errors:
        return None, errors

    test                   = Test()
    test.author            = request.user.username
    test.book_name         = request.POST['book_name']
    test.upload_pgns       = request.POST['upload_pgns']

    test.dev               = get_engine(*dev_info)
    test.dev_repo          = request.POST['dev_repo']
    test.dev_display       = request.POST.get('dev_display', '').strip()[:64]
    test.dev_engine        = request.POST['dev_engine']
    test.dev_options       = OpenBench.utils.merge_engine_test_options(
        request.POST['dev_engine'], request.POST['dev_options'])
    test.dev_network       = request.POST['dev_network']
    test.dev_time_control  = OpenBench.utils.TimeControl.parse(request.POST['dev_time_control'])
    test.dev_ponder_mode   = request.POST.get('dev_ponder_mode', Test.PonderMode.OFF)

    test.dev_build_name, test.dev_build_args = resolve_build_variant(request, 'dev_engine', 'dev_build')

    test.base              = get_engine(*base_ingo)
    test.base_repo         = request.POST['base_repo']
    test.base_display      = request.POST.get('base_display', '').strip()[:64]
    test.base_engine       = request.POST['base_engine']
    test.base_options      = OpenBench.utils.merge_engine_test_options(
        request.POST['base_engine'], request.POST['base_options'])
    test.base_network      = request.POST['base_network']
    test.base_time_control = OpenBench.utils.TimeControl.parse(request.POST['base_time_control'])
    test.base_ponder_mode  = request.POST.get('base_ponder_mode', Test.PonderMode.OFF)

    test.base_build_name, test.base_build_args = resolve_build_variant(request, 'base_engine', 'base_build')

    test.workload_size     = int(request.POST['workload_size'])
    test.priority          = int(request.POST['priority'])
    test.throughput        = int(request.POST['throughput'])

    test.syzygy_wdl        = request.POST['syzygy_wdl']
    test.syzygy_adj        = request.POST['syzygy_adj']
    test.win_adj           = request.POST['win_adj']
    test.draw_adj          = request.POST['draw_adj']

    test.scale_method      = request.POST['scale_method']
    test.scale_nps         = int(request.POST['scale_nps'])

    test.test_mode         = request.POST['test_mode']
    test.awaiting          = not (dev_has_all and base_has_all)

    if test.test_mode == 'SPRT':
        test.elolower = float(request.POST['test_bounds'].split(',')[0].lstrip('['))
        test.eloupper = float(request.POST['test_bounds'].split(',')[1].rstrip(']'))
        test.alpha    = float(request.POST['test_confidence'].split(',')[1].rstrip(']'))
        test.beta     = float(request.POST['test_confidence'].split(',')[0].lstrip('['))
        test.lowerllr = math.log(test.beta / (1.0 - test.alpha))
        test.upperllr = math.log((1.0 - test.beta) / test.alpha)

    if test.test_mode == 'GAMES':
        test.max_games = int(request.POST['test_max_games'])

    if test.dev_network:
        test.dev_netname = OpenBench.utils.network_for_engine(
            test.dev_engine, sha256=test.dev_network).name

    if test.base_network:
        test.base_netname = OpenBench.utils.network_for_engine(
            test.base_engine, sha256=test.base_network).name

    test.save()

    profile = Profile.objects.get(user=request.user)
    profile.tests += 1
    profile.save()

    return test, None

def create_new_tune(request):

    # Collects erros, and collects all data from the Github API
    errors, engine_info = verify_workload(request, 'TUNE')
    dev_info, dev_has_all = engine_info

    if errors:
        return None, errors

    # .tune キット使用時は、対象ブランチと全 context / マーカーが一致することを
    # ここで確認する。ずれたままワーカーに渡すと TUNE ビルドが必ず失敗するので、
    # 作成時に止めてキットページ (照合 → 自動追随) へ誘導する
    if (kit := requested_tune_kit(request)):
        errors = OpenBench.tune_kits.verify_kit_matches_branch(
            kit, request.POST['dev_repo'], request.POST['dev_branch'])
        if errors:
            return None, errors

    test                  = Test()
    test.author           = request.user.username
    test.book_name        = request.POST['book_name']

    test.dev              = test.base              = get_engine(*dev_info)
    test.dev_display      = test.base_display      = request.POST.get('dev_display', '').strip()[:64]
    test.dev_repo         = test.base_repo         = request.POST['dev_repo']
    test.dev_engine       = test.base_engine       = request.POST['dev_engine']
    test.dev_options      = test.base_options      = request.POST['dev_options']
    test.dev_network      = test.base_network      = request.POST['dev_network']
    test.dev_time_control = test.base_time_control = OpenBench.utils.TimeControl.parse(request.POST['dev_time_control'])

    test.dev_build_name, test.dev_build_args = resolve_build_variant(request, 'dev_engine', 'dev_build')
    test.base_build_name, test.base_build_args = test.dev_build_name, test.dev_build_args

    test.priority         = int(request.POST['priority'])
    test.throughput       = int(request.POST['throughput'])

    test.scale_method     = request.POST['scale_method']
    test.scale_nps        = int(request.POST['scale_nps'])

    # rshogi spsa が対局を丸ごと担うため、棋譜保存・Syzygy・勝敗判定は使わない
    test.upload_pgns      = 'FALSE'
    test.syzygy_wdl       = 'DISABLED'
    test.syzygy_adj       = 'DISABLED'
    test.win_adj          = 'None'
    test.draw_adj         = 'None'

    test.test_mode        = 'SPSA'
    test.spsa             = extract_spsa_config(request)

    # 表示用: 1 バッチのペア数を割当サイズとして残す
    test.workload_size    = test.spsa['batch_pairs']

    test.awaiting         = not dev_has_all

    if test.dev_network:
        name = OpenBench.utils.network_for_engine(
            test.dev_engine, sha256=test.dev_network).name
        test.dev_netname = test.base_netname = name

    test.save()

    profile = Profile.objects.get(user=request.user)
    profile.tests += 1
    profile.save()

    return test, None

def create_new_datagen(request):

    # Collects erros, and collects all data from the Github API
    errors, engine_info = verify_workload(request, 'DATAGEN')
    dev_info, dev_has_all = engine_info[0]
    base_ingo, base_has_all = engine_info[1]

    if errors:
        return None, errors

    test                   = Test()
    test.author            = request.user.username
    test.book_name         = request.POST['book_name']
    test.upload_pgns       = request.POST['upload_pgns']

    test.dev               = get_engine(*dev_info)
    test.dev_repo          = request.POST['dev_repo']
    test.dev_display       = request.POST.get('dev_display', '').strip()[:64]
    test.dev_engine        = request.POST['dev_engine']
    test.dev_options       = request.POST['dev_options']
    test.dev_network       = request.POST['dev_network']
    test.dev_time_control  = OpenBench.utils.TimeControl.parse(request.POST['dev_time_control'])

    test.dev_build_name, test.dev_build_args = resolve_build_variant(request, 'dev_engine', 'dev_build')

    test.base              = get_engine(*base_ingo)
    test.base_repo         = request.POST['base_repo']
    test.base_display      = request.POST.get('base_display', '').strip()[:64]
    test.base_engine       = request.POST['base_engine']
    test.base_options      = request.POST['base_options']
    test.base_network      = request.POST['base_network']
    test.base_time_control = OpenBench.utils.TimeControl.parse(request.POST['base_time_control'])

    test.base_build_name, test.base_build_args = resolve_build_variant(request, 'base_engine', 'base_build')

    test.max_games         = int(request.POST['datagen_max_games'])
    test.genfens_args      = request.POST['datagen_custom_genfens']
    test.play_reverses     = request.POST['datagen_play_reverses'] == 'YES'

    test.workload_size     = int(request.POST['workload_size'])
    test.priority          = int(request.POST['priority'])
    test.throughput        = int(request.POST['throughput'])

    test.syzygy_wdl        = request.POST['syzygy_wdl']
    test.syzygy_adj        = request.POST['syzygy_adj']
    test.win_adj           = request.POST['win_adj']
    test.draw_adj          = request.POST['draw_adj']

    test.scale_method      = request.POST['scale_method']
    test.scale_nps         = int(request.POST['scale_nps'])

    test.test_mode         = 'DATAGEN'
    test.awaiting          = not (dev_has_all and base_has_all)

    test.use_tri           = not test.play_reverses
    test.use_penta         = test.play_reverses

    if test.dev_network:
        test.dev_netname = OpenBench.utils.network_for_engine(
            test.dev_engine, sha256=test.dev_network).name

    if test.base_network:
        test.base_netname = OpenBench.utils.network_for_engine(
            test.base_engine, sha256=test.base_network).name

    test.save()

    profile = Profile.objects.get(user=request.user)
    profile.tests += 1
    profile.save()

    return test, None

def requested_tune_kit(request):

    raw = request.POST.get('spsa_tune_kit', '').strip()
    if raw == '' or raw == 'NONE' or not raw.isdigit():
        return None
    return TuneKit.objects.filter(id=int(raw)).first()

def extract_spsa_config(request):

    ## rshogi ラッパー用の SPSA 設定。スケジュール計算 (c_k, a_k) は rshogi 側が
    ## fishtest 互換で行うので、サーバはフラグと .params 原文を保持するだけでよい。
    ## 'parameters' は GUI 表示用のビューで、current 値はワーカー報告で更新される

    params_text = OpenBench.spsa_params.normalize_params_text(request.POST['spsa_inputs'])
    rows, _     = OpenBench.spsa_params.parse_params_text(params_text)

    seed = request.POST.get('spsa_seed', '').strip()

    early_patience = request.POST.get('spsa_early_patience', '').strip()
    early_stop     = { 'patience' : 0, 'avg_abs_update' : None, 'result_variance' : None }
    if early_patience and int(early_patience) > 0:
        early_stop = {
            'patience'        : int(early_patience),
            'avg_abs_update'  : float(request.POST['spsa_early_avg_update']),
            'result_variance' : float(request.POST['spsa_early_result_var']),
        }

    # .tune キットはスナップショットで保存する: 後からキットが編集・追随されても、
    # 実行中 / 再開時のこのチューニングは作成時点の内容でビルドされ続ける
    # (rshogi の resume はパラメータ集合の変化を許さないため)
    tune_kit = None
    if (kit := requested_tune_kit(request)):
        tune_kit = {
            'id'          : kit.id,
            'name'        : kit.name,
            'engine'      : kit.engine,
            'sha'         : kit.content_sha(),
            'tune_text'   : kit.tune_text,
            'params_text' : kit.params_text,
        }

    return {
        # 新旧スキーマの判別子。旧 (分散SPSA) レコードにはこのキーが無い
        'wrapper'      : 'RSHOGI',

        # TUNE ビルド用の .tune キットのスナップショット (未使用時は None)
        'tune_kit'     : tune_kit,

        # rshogi spsa の fishtest 互換スケジュール設定
        'alpha'        : float(request.POST['spsa_alpha']),
        'gamma'        : float(request.POST['spsa_gamma']),
        'a_ratio'      : float(request.POST['spsa_a_ratio']),
        'total_pairs'  : int(request.POST['spsa_total_pairs']),
        'batch_pairs'  : int(request.POST['spsa_batch_pairs']),
        'seed'         : int(seed) if seed != '' else None,

        # 対象の絞り込みと名前空間変換
        'active_regex' : request.POST.get('spsa_active_regex', '').strip(),
        'mapping'      : request.POST.get('spsa_mapping', 'NONE'),
        'early_stop'   : early_stop,

        # ワーカーが --init-from に書き出す canonical .params の原文
        'params_text'  : params_text,

        # GUI 表示用 (current 値はワーカー報告で更新)
        'parameters'   : OpenBench.spsa_params.rows_to_parameters(rows),

        # ワーカーからの進捗報告で更新される欄
        'progress'     : {},
        'state_params' : '',
        'final_params' : '',
    }

def get_engine(source, name, sha, bench):

    engine = Engine.objects.filter(name=name, source=source, sha=sha, bench=bench)
    if engine.first() != None:
        return engine.first()

    return Engine.objects.create(name=name, source=source, sha=sha, bench=bench)
