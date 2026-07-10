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
# >>> verify_workload(request, type)
#
# Given a request, and a workload_type [TEST, TUNE, DATAGEN], verify all of
# the form inputs, collect all of the information from Github, verify all of
# the data from Github, and return a tuple of (errors, engines)
#
# For Verifying and Collection information on a Test:
#   >>> errors, engine_info = verify_workload(request, 'TEST')
#   >>> dev_info, base_info = engine_info
#
# For Verifying and Collection information on a Tune:
#   >>> errors, engine_info = verify_workload(request, 'TUNE')
#
# For Verifying and Collection information on Datagen:
#   >>> errors, engine_info = verify_workload(request, 'DATAGEN')
#   >>> dev_info, base_info = engine_info

import datetime
import os
import re
import requests
import traceback

import OpenBench.config
import OpenBench.spsa_params
import OpenBench.utils

from OpenBench.models import *

class GithubAPIError(Exception):
    pass

def github_json(response, branch):

    try: data = response.json()
    except:
        raise GithubAPIError('GitHub API returned a non-JSON response while checking %s' % (branch or 'Branch'))

    message = data.get('message', 'HTTP %d' % response.status_code) if isinstance(data, dict) else 'HTTP %d' % response.status_code

    if response.status_code == 404:
        raise GithubAPIError('%s could not be found' % (branch or 'Branch'))

    if response.status_code == 403 and response.headers.get('x-ratelimit-remaining') == '0':
        reset = response.headers.get('x-ratelimit-reset')
        retry = ''
        if reset:
            try:
                when = datetime.datetime.fromtimestamp(int(reset), datetime.timezone.utc)
                retry = ' Retry after %s UTC.' % when.strftime('%Y-%m-%d %H:%M:%S')
            except: pass
        raise GithubAPIError(
            'GitHub API rate limit exceeded while checking %s.%s Configure OPENBENCH_GITHUB_TOKEN for authenticated requests.'
            % (branch or 'Branch', retry))

    if response.status_code >= 400:
        raise GithubAPIError('GitHub API error while checking %s: %s' % (branch or 'Branch', message))

    return data

def verify_workload(request, workload_type):

    assert workload_type in [ 'TEST', 'TUNE', 'DATAGEN' ]

    errors = []

    if workload_type == 'TEST':
        verify_test_creation(errors, request)
        dev  = collect_github_info(errors, request, 'dev')
        base = collect_github_info(errors, request, 'base')
        return errors, (dev, base)

    if workload_type == 'TUNE':
        verify_tune_creation(errors, request)
        engine = collect_github_info(errors, request, 'dev')
        return errors, engine

    if workload_type == 'DATAGEN':
        verify_datagen_creation(errors, request)
        dev  = collect_github_info(errors, request, 'dev')
        base = collect_github_info(errors, request, 'base')
        return errors, (dev, base)

def verify_test_creation(errors, request):

    verifications = [

        # Verify everything about the Dev Engine
        (verify_configuration  , 'dev_engine', 'Dev Engine', 'engines'),
        (verify_github_repo    , 'dev_repo'),
        (verify_network        , 'dev_network', 'Dev Network', 'dev_engine'),
        (verify_build_variant  , 'dev_build', 'Dev Build', 'dev_engine'),
        (verify_options        , 'dev_options', 'Threads', 'Dev Options'),
        (verify_options        , 'dev_options', 'Hash', 'Dev Options'),
        (verify_time_control   , 'dev_time_control', 'Dev Time Control'),

        # Verify everything about the Base Engine
        (verify_configuration  , 'base_engine', 'Base Engine', 'engines'),
        (verify_github_repo    , 'base_repo'),
        (verify_network        , 'base_network', 'Base Network', 'base_engine'),
        (verify_build_variant  , 'base_build', 'Base Build', 'base_engine'),
        (verify_options        , 'base_options', 'Threads', 'Base Options'),
        (verify_options        , 'base_options', 'Hash', 'Base Options'),
        (verify_time_control   , 'base_time_control', 'Base Time Control'),

        # Verify everything about the Test Settings
        (verify_configuration  , 'book_name', 'Book', 'books'),
        (verify_upload_pgns    , 'upload_pgns', 'Upload PGNs'),
        (verify_test_mode      , 'test_mode'),
        (verify_sprt_bounds    , 'test_bounds'),
        (verify_sprt_conf      , 'test_confidence'),
        (verify_max_games      , 'test_max_games'),

        # Verify everything about the General Settings
        (verify_integer        , 'priority', 'Priority'),
        (verify_greater_than   , 'throughput', 'Throughput', 0),
        (verify_syzygy_field   , 'syzygy_wdl', 'Syzygy WDL'),

        # Verify everything about the Workload Settings
        (verify_integer        , 'workload_size', 'Workload Size'),
        (verify_greater_than   , 'workload_size', 'Workload Size', 0),

        # Verify the Scaling Mechanisms
        (verify_scale_method   , 'scale_method'),
        (verify_integer        , 'scale_nps', 'Scale NPS'),
        (verify_greater_than   , 'scale_nps', 'Scale NPS', 0),

        # Verify everything about the Adjudicaton Settings
        (verify_syzygy_field   , 'syzygy_adj', 'Syzygy Adjudication'),
        (verify_win_adj        , 'win_adj'),
        (verify_draw_adj       , 'draw_adj'),
    ]

    for verification in verifications:
        verification[0](errors, request, *verification[1:])

def verify_tune_creation(errors, request):

    # SPSA (rshogi ラッパー): 対局・スケジュールは丸ごと rshogi の spsa が担うので、
    # ここでは .params テキストと rshogi へ渡すフラグ類だけを検証する

    verifications = [

        # Verify the SPSA raw inputs
        (verify_spsa_inputs           , 'spsa_inputs'),

        # Verify everything about the Engine
        (verify_configuration         , 'dev_engine', 'Engine', 'engines'),
        (verify_github_repo           , 'dev_repo'),
        (verify_network               , 'dev_network', 'Network', 'dev_engine'),
        (verify_build_variant         , 'dev_build', 'Build', 'dev_engine'),
        (verify_options               , 'dev_options', 'Threads', 'Options'),
        (verify_options               , 'dev_options', 'Hash', 'Options'),
        (verify_tune_time_control     , 'dev_time_control', 'Time Control'),

        # Verify everything about the Test Settings
        (verify_configuration         , 'book_name', 'Book', 'books'),

        # Verify everything about the General Settings
        (verify_integer               , 'priority', 'Priority'),
        (verify_greater_than          , 'throughput', 'Throughput', 0),

        # Verify the Scaling Mechanisms
        (verify_scale_method   , 'scale_method'),
        (verify_integer        , 'scale_nps', 'Scale NPS'),
        (verify_greater_than   , 'scale_nps', 'Scale NPS', 0),

        # Verify everything about the SPSA (rshogi) Settings
        (verify_float                 , 'spsa_alpha', 'SPSA Alpha'),
        (verify_float                 , 'spsa_gamma', 'SPSA Gamma'),
        (verify_float                 , 'spsa_a_ratio', 'SPSA A-Ratio'),
        (verify_greater_than          , 'spsa_alpha', 'SPSA Alpha', 0.00),
        (verify_greater_than          , 'spsa_gamma', 'SPSA Gamma', 0.00),
        (verify_greater_than          , 'spsa_a_ratio', 'SPSA A-Ratio', 0.00),
        (verify_integer               , 'spsa_total_pairs', 'SPSA Total Pairs'),
        (verify_integer               , 'spsa_batch_pairs', 'SPSA Batch Pairs'),
        (verify_greater_than          , 'spsa_total_pairs', 'SPSA Total Pairs', 0),
        (verify_greater_than          , 'spsa_batch_pairs', 'SPSA Batch Pairs', 0),
        (verify_spsa_pair_counts      , 'spsa_total_pairs'),
        (verify_spsa_seed             , 'spsa_seed'),
        (verify_spsa_active_regex     , 'spsa_active_regex'),
        (verify_spsa_mapping          , 'spsa_mapping', 'Parameter Mapping'),
        (verify_spsa_early_stop       , 'spsa_early_patience'),
    ]

    for verification in verifications:
        verification[0](errors, request, *verification[1:])

def verify_datagen_creation(errors, request):

    verifications = [

        # Verify everything about the Dev Engine
        (verify_configuration  , 'dev_engine', 'Dev Engine', 'engines'),
        (verify_github_repo    , 'dev_repo'),
        (verify_network        , 'dev_network', 'Dev Network', 'dev_engine'),
        (verify_build_variant  , 'dev_build', 'Dev Build', 'dev_engine'),
        (verify_options        , 'dev_options', 'Threads', 'Dev Options'),
        (verify_options        , 'dev_options', 'Hash', 'Dev Options'),
        (verify_time_control   , 'dev_time_control', 'Dev Time Control'),

        # Verify everything about the Base Engine
        (verify_configuration  , 'base_engine', 'Base Engine', 'engines'),
        (verify_github_repo    , 'base_repo'),
        (verify_network        , 'base_network', 'Base Network', 'base_engine'),
        (verify_build_variant  , 'base_build', 'Base Build', 'base_engine'),
        (verify_options        , 'base_options', 'Threads', 'Base Options'),
        (verify_options        , 'base_options', 'Hash', 'Base Options'),
        (verify_time_control   , 'base_time_control', 'Base Time Control'),

        # Verify everything about the Datagen Settings
        (verify_datagen_games  , 'datagen_max_games'),
        (verify_datagen_genfens, 'datagen_custom_genfens'),
        (verify_datagen_reverse, 'datagen_play_reverses'),
        (verify_datagen_book   , 'book_name', 'Book', 'books'),
        (verify_upload_pgns    , 'upload_pgns', 'Upload PGNs'),

        # Verify everything about the General Settings
        (verify_integer        , 'priority', 'Priority'),
        (verify_greater_than   , 'throughput', 'Throughput', 0),
        (verify_syzygy_field   , 'syzygy_wdl', 'Syzygy WDL'),

        # Verify everything about the Workload Settings
        (verify_integer        , 'workload_size', 'Workload Size'),
        (verify_greater_than   , 'workload_size', 'Workload Size', 0),

        # Verify the Scaling Mechanisms
        (verify_scale_method   , 'scale_method'),
        (verify_integer        , 'scale_nps', 'Scale NPS'),
        (verify_greater_than   , 'scale_nps', 'Scale NPS', 0),

        # Verify everything about the Adjudicaton Settings
        (verify_syzygy_field   , 'syzygy_adj', 'Syzygy Adjudication'),
        (verify_win_adj        , 'win_adj'),
        (verify_draw_adj       , 'draw_adj'),
    ]

    for verification in verifications:
        verification[0](errors, request, *verification[1:])


def verify_integer(errors, request, field, field_name):
    try: int(request.POST[field])
    except: errors.append('"{0}" is not an Integer'.format(field_name))

def verify_float(errors, request, field, field_name):
    try: float(request.POST[field])
    except: errors.append('"{0}" is not a Float'.format(field_name))

def verify_greater_than(errors, request, field, field_name, value):
    try: assert float(request.POST[field]) > value
    except: errors.append('"{0}" is not greater than {1}'.format(field_name, value))

def verify_options(errors, request, field, option, field_name):
    try: assert int(OpenBench.utils.extract_option(request.POST[field], option)) >= 1
    except: errors.append('"{0}" needs to be at least 1 for {1}'.format(option, field_name))

def verify_configuration(errors, request, field, field_name, parent):
    try: assert request.POST[field] in OpenBench.config.OPENBENCH_CONFIG[parent].keys()
    except: errors.append('{0} was not found in the configuration'.format(field_name))

def verify_time_control(errors, request, field, field_name):
    try: OpenBench.utils.TimeControl.parse(request.POST[field])
    except: errors.append('{0} is not parsable'.format(field_name))

def verify_win_adj(errors, request, field):
    try:
        if (content := request.POST[field]) == 'None': return
        assert re.match('movecount=[0-9]+ score=[0-9]+', content)
    except: errors.append('Invalid Win Adjudication Setting. Try "None"?')

def verify_draw_adj(errors, request, field):
    try:
        if (content := request.POST[field]) == 'None': return
        assert re.match('movenumber=[0-9]+ movecount=[0-9]+ score=[0-9]+', content)
    except: errors.append('Invalid Draw Adjudication Setting. Try "None"?')

def verify_github_repo(errors, request, field):
    pattern = r'^https:\/\/github\.com\/[A-Za-z0-9-]+\/[A-Za-z0-9_.-]+\/?$'
    try: assert re.match(pattern, request.POST[field])
    except: errors.append('Sources must be found on https://github.com/<User>/<Repo>')

def verify_network(errors, request, field, field_name, engine_field):
    try:
        if request.POST[field] == '': return
        Network.objects.get(engine=request.POST[engine_field], sha256=request.POST[field])
    except: errors.append('Unknown Network Provided for {0}'.format(field_name))

def verify_build_variant(errors, request, field, field_name, engine_field):
    try:
        import OpenBench.views

        # A free-form build command overrides the dropdown; check it parses
        if (custom := request.POST.get(field + '_custom', '').strip()):
            args, dropped = OpenBench.views.normalize_build_command(custom)
            assert len(args) <= 512
            return

        engine  = request.POST[engine_field]
        variant = request.POST.get(field, 'default')
        assert variant in OpenBench.views.engine_build_variants(engine)
    except: errors.append('Unknown Build Variant Provided for {0}'.format(field_name))

def verify_test_mode(errors, request, field):
    try: assert request.POST[field] in ['SPRT', 'GAMES']
    except: errors.append('Unknown Test Mode')

def verify_sprt_bounds(errors, request, field):
    try:
        if request.POST['test_mode'] != 'SPRT': return
        pattern = r'^\[(-?\d+(?:\.\d+)?), (-?\d+(?:\.\d+)?)\]$'
        match   = re.match(pattern, request.POST['test_bounds'])
        assert float(match.group(1)) < float(match.group(2))
    except: errors.append('SPRT Bounds must be formatted as [float1, float2]')

def verify_sprt_conf(errors, request, field):
    try:
        if request.POST['test_mode'] != 'SPRT': return
        pattern = r'^\[(-?\d+(?:\.\d+)?), (-?\d+(?:\.\d+)?)\]$'
        match   = re.match(pattern, request.POST['test_confidence'])
        assert 0.00 < float(match.group(1)) < 1.00
        assert 0.00 < float(match.group(2)) < 1.00
    except: errors.append('Confidence Bounds must be formatted as [float1, float2], within (0.00, 1.00)')

def verify_max_games(errors, request, field):
    try:
        if request.POST['test_mode'] != 'GAMES': return
        assert int(request.POST['test_max_games']) > 0
    except: errors.append('Fixed Games Tests must last at least one game')

def verify_syzygy_field(errors, request, field, field_name):
    candidates = ['OPTIONAL', 'DISABLED', '3-MAN', '4-MAN', '5-MAN', '6-MAN', '7-MAN']
    try: assert request.POST[field] in candidates
    except: errors.append('%s must be in %s' % (field_name, ', '.join(candidates)))

def verify_spsa_inputs(errors, request, field):

    # rshogi / tune.py 共通の 7 カラム .params 形式。コメントや
    # [[NOT USED]] 行も許容する (詳細は OpenBench/spsa_params.py)
    try:
        rows, row_errors = OpenBench.spsa_params.parse_params_text(request.POST[field])
        errors.extend(row_errors)
    except:
        traceback.print_exc()
        errors.append('Malformed SPSA Input')

def verify_tune_time_control(errors, request, field, field_name):

    # rshogi spsa が対応する時間制御のみ: フィッシャー (--btime/--binc),
    # 秒読み MT= (--byoyomi), ノード固定 N= (--nodes)
    try:
        parsed  = OpenBench.utils.TimeControl.parse(request.POST[field])
        allowed = [ OpenBench.utils.TimeControl.FISCHER,
                    OpenBench.utils.TimeControl.FIXED_TIME,
                    OpenBench.utils.TimeControl.FIXED_NODES ]
        if OpenBench.utils.TimeControl.control_type(parsed) not in allowed:
            errors.append('%s: SPSAで使えるのは 秒+加算 (例 2+0.02) / MT=ミリ秒 (秒読み) / N=ノード数 です' % (field_name))
    except:
        errors.append('{0} is not parsable'.format(field_name))

def verify_spsa_pair_counts(errors, request, field):
    try:
        total = int(request.POST['spsa_total_pairs'])
        batch = int(request.POST['spsa_batch_pairs'])
        if total < batch:
            errors.append('SPSA Total Pairs must be at least Batch Pairs')
    except:
        pass # 個別の integer 検証が報告する

def verify_spsa_seed(errors, request, field):
    raw = request.POST.get(field, '').strip()
    if raw == '':
        return # 空欄 = ランダム seed
    try: assert int(raw) >= 0
    except: errors.append('SPSA Seed must be blank, or a non-negative integer')

def verify_spsa_active_regex(errors, request, field):

    raw = request.POST.get(field, '').strip()
    if raw == '':
        return

    try: pattern = re.compile(raw)
    except: return errors.append('SPSA Active Regex does not compile')

    # パラメータが正しく読めているときだけ、少なくとも 1 つの
    # 生きているパラメータに一致することを確認する
    try: rows, row_errors = OpenBench.spsa_params.parse_params_text(request.POST['spsa_inputs'])
    except: return

    if not row_errors and not any(pattern.search(x['name']) for x in rows if not x['not_used']):
        errors.append('SPSA Active Regex はどのチューニング対象パラメータにも一致しません')

def verify_spsa_mapping(errors, request, field, field_name):
    candidates = ['NONE', 'YO']
    try: assert request.POST.get(field, 'NONE') in candidates
    except: errors.append('%s must be in %s' % (field_name, ', '.join(candidates)))

def verify_spsa_early_stop(errors, request, field):

    # 早期停止は 3 点セット: patience > 0 のときだけ有効で、その場合は
    # 両方の閾値が必要 (rshogi 側の判定が両閾値の AND のため)
    patience = request.POST.get('spsa_early_patience', '').strip()
    avg_thr  = request.POST.get('spsa_early_avg_update', '').strip()
    var_thr  = request.POST.get('spsa_early_result_var', '').strip()

    if not patience and not avg_thr and not var_thr:
        return

    try: assert int(patience) > 0
    except: return errors.append('早期停止を使うには patience に正の整数を指定してください')

    try: assert float(avg_thr) > 0.0 and float(var_thr) > 0.0
    except: errors.append('早期停止には avg|update| と |raw|/batch の両閾値 (正の実数) が必要です')

def verify_upload_pgns(errors, request, field, field_name):
    try: request.POST[field] in ['FALSE', 'COMPACT', 'VERBOSE']
    except: errors.append('"%s" must be FALSE, COMPACT, or VERBOSE' % (field_name))

def verify_datagen_games(errors, request, field):
    try: assert int(request.POST[field]) > 0
    except: errors.append('Data Generation must last for at least one game')

def verify_datagen_genfens(errors, request, field):
    try: assert '"' not in request.POST[field]
    except: errors.append('Quotes are not allowed in genfens args')

def verify_datagen_reverse(errors, request, field):
    try: assert request.POST[field] in ['YES', 'NO']
    except: errors.append('Play Reverses must either be YES or NO')

def verify_datagen_book(errors, request, field, field_name, parent):
    try:
        valid = ['NONE'] + list(OpenBench.config.OPENBENCH_CONFIG[parent].keys())
        assert request.POST[field] in valid
    except: errors.append('{0} was neither NONE nor found in the configuration'.format(field_name))

def verify_scale_method(errors, request, field):
    try: assert(request.POST[field] in Test.ScaleMethod)
    except:
        choices = [f[0] for f in Test.ScaleMethod.choices]
        errors.append('Unknown Scale Method. Expected one of {%s}.' % (', '.join(choices)))


def collect_github_info(errors, request, field):

    # Get branch name / commit sha / tag, and the API path for it
    branch = request.POST['{0}_branch'.format(field)]
    bysha  = bool(re.search('^[0-9a-fA-F]{40}$', branch))

    # All API requests will share this common path. Some engines are private.
    base    = request.POST['%s_repo' % (field)].replace('github.com', 'api.github.com/repos')
    engine  = request.POST['%s_engine' % (field)]
    private = OpenBench.config.OPENBENCH_CONFIG['engines'][engine]['private']
    headers = OpenBench.utils.read_git_credentials(engine) or {}

    ## Step 1: Verify the target of the API requests
    ## [A] We will not attempt to reach any site other than api.github.com
    ## [B] Private engines may only use their main repo for sources of tests
    ## [C] Determine which, if any, credentials we want to pass along

    # Private engines must have a token stored in credentials.enginename
    if private and not headers:
        errors.append('Server does not have access tokens for this engine')
        return (None, None)

    # Do not allow private engines to use forked repos ( We don't have a token! )
    if requests_illegal_fork(request, field):
        errors.append('Forked Repositories are not allowed for Private engines')
        return (None, None)

    # Avoid leaking our credentials to other websites
    if not base.startswith('https://api.github.com/'):
        errors.append('OpenBench may only reach Github\'s API')
        return (None, None)

    ## Step 2: Connect to the Github API for the given Branch or Commit SHA.
    ## [A] We will attempt to parse the most recent commit message for a
    ##     bench, unless one was supplied.
    ## [B] We will translate any branch name into a commit SHA for later use,
    ##     so we may compare branches and generate diff URLs
    ## [C] If the engine is public, we will construct the URL to download the
    ##     source code from Github into a .zip file.
    ## [D] If the engine is private, we will carry onto Step 3.

    try: # Fetch data from the Github API

        # Lookup branch or commit sha, but will fail for tags
        url  = OpenBench.utils.path_join(base, 'commits' if bysha else 'branches', branch)
        data = github_json(requests.get(url, headers=headers), branch)

        # Check to see if the branch name was actually a tag name
        if not bysha and 'commit' not in data:
            url  = OpenBench.utils.path_join(base, 'commits', branch)
            data = github_json(requests.get(url, headers=headers), branch)

        # Actual branches have to go one layer deeper
        elif not bysha: data = data['commit']

        # Check that all the data we need going forward is present
        assert 'message' in data['commit'] and 'sha' in data
        assert private or 'sha' in data['commit']['tree']

    except GithubAPIError as error:
        errors.append(str(error))
        return (None, None)

    except: # Unable to find for whatever reason
        traceback.print_exc()
        errors.append('%s could not be found' % (branch or 'Branch'))
        return (None, None)

    # Extract the bench from the web form, or from the commit message
    if (bench := determine_bench(request, field, data['commit']['message'])) is None:
        errors.append('Unable to parse a Bench for %s' % (branch))
        return (None, None)

    # Public Engines: Construct the .zip download and return everything
    if not private:
        treeurl = data['commit']['tree']['sha'] + '.zip'
        source  = OpenBench.utils.path_join(request.POST['%s_repo' % (field)], 'archive', treeurl)
        return (source, branch, data['sha'], bench), True

    ## Step 3: Construct the URL for the API request to list all Artifacts
    ## [A] OpenBench artifacts are always run via a file named openbench.yml
    ## [B] These should contain combinations for windows/linux, avx2/avx512, popcnt/pext
    ## [C] If those artifacts are not found, we flag the test as awaiting, and try later.

    url, has_all = fetch_artifact_url(base, engine, headers, data['sha'])
    return (url, branch, data['sha'], bench), has_all

def requests_illegal_fork(request, field):

    # Strip trailing '/'s for sanity
    engine  = OpenBench.config.OPENBENCH_CONFIG['engines'][request.POST['%s_engine' % (field)]]
    eng_src = engine['source'].rstrip('/')
    tar_src = request.POST['%s_repo' % (field)].rstrip('/')

    # Illegal if sources do not match for Private engines
    return engine['private'] and eng_src != tar_src

def determine_bench(request, field, message):

    raw = request.POST.get('{0}_bench'.format(field), '').strip()

    # An empty field means the bench check is skipped: workers treat an
    # expected bench of 0 as "measure NPS, but verify nothing". Useful for
    # engines like YaneuraOu whose bench depends on the build and eval file
    if raw == '' or raw.lower() in ('skip', 'none', 'n/a', '0'):
        return 0

    # Use the provided bench if possible
    try: return int(raw)
    except: pass

    # Fallback to try to parse the Bench from the commit ("Autofill")
    try:
        benches = re.findall('(?:BENCH|NODES)[ :=]+([0-9,]+)', message, re.IGNORECASE)
        return int(benches[-1].replace(',', ''))
    except: return None

def fetch_artifact_url(base, engine, headers, sha):

    try:
        # Fetch the run id for the openbench workflow for this comment
        url    = OpenBench.utils.path_join(base, 'actions', 'workflows', 'openbench.yml', 'runs')
        url   += '?head_sha=%s' % (sha)
        run_id = requests.get(url=url, headers=headers).json()['workflow_runs'][0]['id']

        # Fetch information about individual job results
        url  = OpenBench.utils.path_join(base, 'actions', 'runs', str(run_id), 'jobs')
        jobs = requests.get(url=url, headers=headers).json()['jobs']

        # Fetch information about individual artifact
        url       = OpenBench.utils.path_join(base, 'actions', 'runs', str(run_id), 'artifacts')
        artifacts = requests.get(url=url, headers=headers).json()['artifacts']

        # All jobs finished, with at least one non-expired Artifact
        assert not any(job['conclusion'] != 'success' for job in jobs)
        assert not any(artifact['expired'] for artifact in artifacts)
        assert len(artifacts) >= len(jobs)

        # Only set the url if we have everything we need
        return (url, True)

    except Exception as error:
        # If anything goes wrong, retry later with the same base URL
        return (base, False)
