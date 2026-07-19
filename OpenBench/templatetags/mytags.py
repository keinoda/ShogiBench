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

import re, django
import OpenBench.config, OpenBench.utils, OpenBench.stats, OpenBench.models

def oneDigitPrecision(value):
    try:
        value = round(value, 1)
        if '.' not in str(value):
            return str(value) + '.0'
        pre, post = str(value).split('.')
        post += '0'
        return pre + '.' + post[0:1]
    except:
        return value

def twoDigitPrecision(value):
    try:
        value = round(value, 2)
        if '.' not in str(value):
            return str(value) + '.00'
        pre, post = str(value).split('.')
        post += '00'
        return pre + '.' + post[0:2]
    except:
        return value

def gitDiffLink(test):

    engines = OpenBench.config.OPENBENCH_CONFIG['engines']

    if test.dev_engine in engines and engines[test.dev_engine]['private']:
        repo = OpenBench.config.OPENBENCH_CONFIG['engines'][test.dev_engine]['source']
    else:
        repo = OpenBench.utils.path_join(*test.dev.source.split('/')[:-2])

    if test.test_mode == 'SPSA':
        return OpenBench.utils.path_join(repo, 'compare', test.dev.sha[:8])

    return OpenBench.utils.path_join(repo, 'compare',
        '{0}..{1}'.format( test.base.sha[:8], test.dev.sha[:8]))

def shortStatBlock(test):

    tri_line   = 'Games: %d W: %d L: %d D: %d' % test.as_nwld()
    penta_line = 'Ptnml(0-2): %d, %d, %d, %d, %d' % test.as_penta()

    if test.test_mode == 'SPSA' and spsa_is_rshogi(test):

        progress = test.spsa.get('progress', {}) or {}
        pairs    = progress.get('completed_pairs', 0)
        total    = max(1, test.spsa.get('total_pairs', 1))
        active   = sum(1 for p in test.spsa['parameters'].values() if not p.get('not_used'))

        statlines = [
            'Tuning %d Parameters (rshogi)' % (active),
            '%d/%d Pairs (%.1f%%)' % (pairs, total, 100.0 * pairs / total),
            '%d Games Played' % (test.games)]

    elif test.test_mode == 'SPSA':
        statlines = [
            'Tuning %d Parameters' % (len(test.spsa['parameters'].keys())),
            '%d/%d Iterations' % (test.games / (2 * test.spsa['pairs_per']), test.spsa['iterations']),
            '%d/%d Games Played' % (test.games, 2 * test.spsa['iterations'] * test.spsa['pairs_per'])]

    elif test.test_mode == 'SPRT':
        llr_line = 'LLR: %0.2f (%0.2f, %0.2f) [%0.2f, %0.2f]' % (
            test.currentllr, test.lowerllr, test.upperllr, test.elolower, test.eloupper)
        statlines = [llr_line, tri_line, penta_line] if test.use_penta else [llr_line, tri_line]

    elif test.test_mode == 'GAMES':
        lower, elo, upper = OpenBench.stats.Elo(test.results())
        elo_line = 'Elo: %0.2f +- %0.2f (95%%) [N=%d]' % (elo, max(upper - elo, elo - lower), test.max_games)
        statlines = [elo_line, tri_line, penta_line] if test.use_penta else [elo_line, tri_line]

    elif test.test_mode == 'DATAGEN':
        status_line = 'Generated %d/%d Games' % (test.games, test.max_games)
        lower, elo, upper = OpenBench.stats.Elo(test.results())
        elo_line = 'Elo: %0.2f +- %0.2f (95%%) [N=%d]' % (elo, max(upper - elo, elo - lower), test.max_games)
        statlines = [status_line, elo_line, penta_line] if test.use_penta else [status_line, elo_line, tri_line]

    return '\n'.join(statlines)

def longStatBlock(test):

    assert test.test_mode != 'SPSA'

    def option_value(options, name):
        return OpenBench.utils.extract_option(options, name) or '?'

    def timecontrol_text(timecontrol):
        return timecontrol + ['s', '']['=' in timecontrol]

    def discord_text(value):
        return str(value).replace('`', "'")

    def branch_name(branch):
        display = getattr(test, '%s_display' % branch)
        netname = getattr(test, '%s_netname' % branch)
        engine  = getattr(test, branch)
        name    = discord_text(display or netname or prettyName(engine.name))
        suffix  = '-%s' % branch
        return name if name.lower().endswith(suffix) else name + suffix

    def match_settings():
        dev_tc      = timecontrol_text(test.dev_time_control)
        base_tc     = timecontrol_text(test.base_time_control)
        dev_threads = option_value(test.dev_options, 'Threads')
        base_threads= option_value(test.base_options, 'Threads')
        dev_hash    = option_value(test.dev_options, 'Hash')
        base_hash   = option_value(test.base_options, 'Hash')

        if dev_tc == base_tc and dev_threads == base_threads and dev_hash == base_hash:
            return '%s, Threads=%s, Hash=%sMB' % (
                dev_tc, dev_threads, dev_hash)

        return 'Dev %s T=%s H=%sMB / Base %s T=%s H=%sMB' % (
            dev_tc, dev_threads, dev_hash, base_tc, base_threads, base_hash)

    type_text = 'SPRT' if test.test_mode == 'SPRT' else 'Conf'

    lower, elo, upper = OpenBench.stats.Elo(test.results())

    lines = [
        '```text',
        '%s vs %s' % (branch_name('dev'), branch_name('base')),
        'Score for: %s' % (branch_name('dev')),
        'Elo      : %0.2f +- %0.2f (95%%)' % (elo, max(upper - elo, elo - lower)),
        '%-8s : %s' % (type_text, match_settings()),
        'Book     : %s' % (discord_text(book_display_name(test))),
    ]

    if test.test_mode == 'SPRT':
        lines.append('LLR      : %0.2f (%0.2f, %0.2f) [%0.2f, %0.2f]' % (
            test.currentllr, test.lowerllr, test.upperllr, test.elolower, test.eloupper))

    lines.append('Games    : N=%d W=%d L=%d D=%d' % test.as_nwld())
    if test.use_penta:
        lines.append('Ptnml    : [%d, %d, %d, %d, %d]' % test.as_penta())
    lines.append('```')

    return '\n'.join(lines)

def testResultColour(test):

    if test.passed:
        if test.elolower + test.eloupper < 0: return 'blue'
        return 'green'
    if test.failed:
        if test.wins >= test.losses: return 'yellow'
        return 'red'
    return ''

def sumAttributes(iterable, attribute):
    try: return sum([getattr(f, attribute) for f in iterable])
    except: return 0

def insertCommas(value):
    return '{:,}'.format(int(value))

def prettyName(name):
    if re.search('^[0-9a-fA-F]{40}$', name):
        return name[:16].upper()
    return name

def prettyDevName(test):

    # A user-provided label always wins over the derived names
    if test.dev_display:
        return test.dev_display

    # If engines are different, use the base name + branch
    if test.dev_engine != test.base_engine:
        return '[%s] %s' % (test.base_engine, test.base.name)

    # If testing different Networks, possibly use the Network name
    if test.dev.name == test.base.name and test.dev_netname != '':

        # Nets match as well, so revert back to the branch name
        if test.dev_network == test.base_network:
            return prettyName(test.dev.name)

        # Use the network's name, if we still have it saved
        try: return OpenBench.models.Network.objects.get(sha256=test.dev_network).name
        except: return test.dev_netname # File has since been deleted ?

    return prettyName(test.dev.name)

def testIdToPrettyName(test_id):
    return prettyName(OpenBench.models.Test.objects.get(id=test_id).dev.name)

def testIdToTimeControl(test_id):
    return OpenBench.models.Test.objects.get(id=test_id).dev_time_control

def cpuflagsBlock(machine, N=8):

    reported = []
    flags    = machine.info['cpu_flags']

    general_flags   = ['BMI2', 'POPCNT']
    broad_avx_flags = ['AVX2', 'AVX', 'SSE42', 'SSE41', 'SSSE3']

    for flag in general_flags:
        if flag in flags:
            reported.append(flag)
            break

    for flag in broad_avx_flags:
        if flag in flags:
            reported.append(flag)
            break

    for flag in flags:
        if flag not in general_flags and flag not in broad_avx_flags:
            reported.append(flag)

    return ' '.join(reported)

def compilerBlock(machine):
    string = ''
    for engine, info in machine.info['compilers'].items():
        string += '%-16s %-8s (%s)\n' % (engine, info[0], info[1])
    return string

def removePrefix(value, prefix):
    return value.removeprefix(prefix)

def machine_name(machine_id):
    try:
        machine = OpenBench.models.Machine.objects.get(id=machine_id)
        return machine.info['machine_name']
    except: return 'None'


register = django.template.Library()
register.filter('oneDigitPrecision', oneDigitPrecision)
register.filter('twoDigitPrecision', twoDigitPrecision)
register.filter('gitDiffLink', gitDiffLink)
register.filter('shortStatBlock', shortStatBlock)
register.filter('longStatBlock', longStatBlock)
register.filter('testResultColour', testResultColour)
register.filter('sumAttributes', sumAttributes)
register.filter('insertCommas', insertCommas)
register.filter('prettyName', prettyName)
register.filter('prettyDevName', prettyDevName)
register.filter('testIdToPrettyName', testIdToPrettyName)
register.filter('testIdToTimeControl', testIdToTimeControl)
register.filter('cpuflagsBlock', cpuflagsBlock)
register.filter('compilerBlock', compilerBlock)
register.filter('removePrefix', removePrefix)
register.filter('machine_name', machine_name)

####

def spsa_is_rshogi(workload):
    return isinstance(workload.spsa, dict) and workload.spsa.get('wrapper') == 'RSHOGI'

def spsa_sorted_names(workload):
    return sorted(
        workload.spsa['parameters'].keys(),
        key=lambda x: workload.spsa['parameters'][x].get('index', -1)
    )

def spsa_progress(workload):

    ## workload.html の進捗ブロック用。rshogi ラッパーのみ意味を持つ

    spsa     = workload.spsa
    progress = spsa.get('progress', {}) or {}

    pairs = progress.get('completed_pairs', 0)
    total = max(1, spsa.get('total_pairs', 1))

    return {
        'started'             : bool(progress),
        'completed_pairs'     : pairs,
        'completed_batches'   : progress.get('completed_batches', 0),
        'total_pairs'         : spsa.get('total_pairs', 0),
        'total_batches'       : (spsa.get('total_pairs', 0) + spsa.get('batch_pairs', 1) - 1)
                                    // max(1, spsa.get('batch_pairs', 1)),
        'percent'             : '%.1f' % (100.0 * pairs / total),
        'total_games'         : progress.get('total_games', 0),
        'last_raw_result'     : '%+.3f' % (progress.get('last_raw_result', 0.0)),
        'last_avg_abs_update' : '%.6f' % (progress.get('last_avg_abs_update', 0.0)),
        'updated_at'          : progress.get('updated_at', ''),
        'machine_id'          : progress.get('machine_id', 0),
        'has_final'           : bool(spsa.get('final_params')),
    }

def spsa_trajectory(workload):

    ## json_script へ渡す、描画に必要な最小データ。パラメータ名は values.csv の
    ## 列順と一致し、definition は [start, min, max, active] の順。

    if not spsa_is_rshogi(workload):
        return {}

    spsa       = workload.spsa
    trajectory = spsa.get('trajectory', {}) or {}
    names      = trajectory.get('names', [])
    parameters = spsa.get('parameters', {})

    if not names or any(name not in parameters for name in names):
        return {}

    return {
        'names'       : names,
        'definitions' : [[parameters[name]['start'], parameters[name]['min'],
                          parameters[name]['max'], not parameters[name].get('not_used')]
                         for name in names],
        'stats'        : trajectory.get('stats', []),
        'values'       : trajectory.get('values', []),
        'total_batches': (spsa.get('total_pairs', 0) + spsa.get('batch_pairs', 1) - 1)
                            // max(1, spsa.get('batch_pairs', 1)),
        'window'       : 16,
    }

def spsa_param_digest(workload):

    if spsa_is_rshogi(workload):
        return rshogi_param_digest(workload)

    digest = []

    # C and R are compressed as we progress iterations
    iteration     = 1 + (workload.games / (workload.spsa['pairs_per'] * 2))
    c_compression = iteration ** workload.spsa['Gamma']
    r_compression = (workload.spsa['A'] + iteration) ** workload.spsa['Alpha']

    for name in spsa_sorted_names(workload):

        param = workload.spsa['parameters'][name]

        # C and R if we got a workload right now
        c = max(param['c'] / c_compression, 0.00 if param['float'] else 0.50)
        r = param['a'] / r_compression / c ** 2

        fstr = '%.4f' if param['float'] else '%d'

        digest.append([
            name,
            '%.4f' % (param['value']),
            fstr   % (param['start']),
            fstr   % (param['min'  ]),
            fstr   % (param['max'  ]),
            '%.4f' % (c),
            '%.4f' % (param['c_end']),
            '%.4f' % (r),
            '%.4f' % (param['r_end']),
        ])

    return digest

def rshogi_param_digest(workload):

    ## rshogi ラッパー: スケジュール (c_k, R_k) は rshogi 側の管轄なので、
    ## ここでは 初期値 → 現在値 の推移とレンジ張り付きの注意を出す

    digest = []
    for name in spsa_sorted_names(workload):

        param = workload.spsa['parameters'][name]
        fstr  = '%.4f' if param['float'] else '%g'

        # 現在値が min/max に C_end 以内まで寄っていたら要注意 (レンジ拡大の検討対象)
        near = ''
        if not param.get('not_used'):
            span = param['max'] - param['min']
            edge = max(param['c_end'], span * 0.01)
            if param['value'] <= param['min'] + edge:
                near = '⚠ min付近'
            elif param['value'] >= param['max'] - edge:
                near = '⚠ max付近'

        if param.get('not_used'):
            near = '[[NOT USED]]'

        digest.append([
            name,
            '%.4f' % (param['value']),
            fstr   % (param['start']),
            '%+.4f' % (param['value'] - param['start']),
            fstr   % (param['min'  ]),
            fstr   % (param['max'  ]),
            fstr   % (param['c_end']),
            '%g'   % (param['r_end']),
            near,
        ])

    return digest

def spsa_param_digest_headers(workload):
    if spsa_is_rshogi(workload):
        return ['Name', 'Curr', 'Start', 'Δ', 'Min', 'Max', 'C_end', 'R_end', '']
    return ['Name', 'Curr', 'Start', 'Min', 'Max', 'C', 'C_end', 'R', 'R_end']

def spsa_original_input(workload):

    # rshogi ラッパーは貼り付けられた .params 原文をそのまま保持している
    if spsa_is_rshogi(workload):
        return workload.spsa.get('params_text', '')

    lines = []
    for name in spsa_sorted_names(workload):

        param = workload.spsa['parameters'][name]
        dtype = 'float' if param['float'] else 'int'

        # Original 7 token Input
        lines.append(', '.join([
            name,
            dtype,
            str(param['start']),
            str(param['min'  ]),
            str(param['max'  ]),
            str(param['c_end']),
            str(param['r_end']),
        ]))

    return '\n'.join(lines)

def spsa_optimal_values(workload):

    # rshogi ラッパー: 完了後は final.params、実行中は state.params の内容を返す。
    # どちらも 7 カラム .params 形式なので、そのまま tune.py apply に渡せる
    if spsa_is_rshogi(workload):
        spsa = workload.spsa
        return spsa.get('final_params') or spsa.get('state_params') or spsa.get('params_text', '')

    lines = []
    for name in spsa_sorted_names(workload):
        param = workload.spsa['parameters'][name]
        value = param['value'] if param['float'] else round(param['value'])
        lines.append(', '.join([name, str(value)]))

    return '\n'.join(lines)


def book_download_link(workload):
    if workload.book_name in OpenBench.config.OPENBENCH_CONFIG['books']:
        return OpenBench.config.OPENBENCH_CONFIG['books'][workload.book_name]['source']

def book_display_name(workload):
    book = OpenBench.config.OPENBENCH_CONFIG['books'].get(workload.book_name, {})
    return book.get('display', workload.book_name)

def network_download_link(workload, branch):

    assert branch in [ 'dev', 'base' ]

    sha    = workload.dev_network if branch == 'dev' else workload.base_network
    engine = workload.dev_engine  if branch == 'dev' else workload.base_engine

    # Network could have been deleted after this workload was finished
    if (network := OpenBench.models.Network.objects.filter(sha256=sha, engine=engine).first()):
        return '/networks/%s/download/%s/' % (engine, sha)

    return '/networks/%s/' % (engine)

def workload_url(workload):

    # Might be a workload id
    if type(workload) == int:
        workload = OpenBench.models.Test.objects.get(id=workload)

    # Differentiate between Tunes, Datagen, and regular Tests
    mapping = { 'SPSA' : 'tune', 'DATAGEN' : 'datagen' }
    return '/%s/%d/' % (mapping.get(workload.test_mode, 'test'), workload.id)

def workload_pretty_name(workload):

    # Might be a workload id
    if type(workload) == int:
        workload = OpenBench.models.Test.objects.get(id=workload)

    # Convert commit sha's to just the first 16 characters
    if re.search('^[0-9a-fA-F]{40}$', workload.dev.name):
        return workload.dev.name[:16].lower()

    return workload.dev.name

def git_diff_text(workload, N=24):

    dev_name = workload.dev_display or workload.dev.name
    dev_name = dev_name[:N] + '...' if len(dev_name) > N else dev_name

    base_name = workload.base_display or workload.base.name
    base_name = base_name[:N] + '...' if len(base_name) > N else base_name

    return '%s vs %s' % (dev_name, base_name)


def test_is_smp_odds(test):
    dev_threads  = int(OpenBench.utils.extract_option(test.dev_options , 'Threads'))
    base_threads = int(OpenBench.utils.extract_option(test.base_options, 'Threads'))
    return dev_threads != base_threads

def test_is_time_odds(test):
    return test.dev_time_control != test.base_time_control

def test_is_fischer(test):
    return 'FRC' in test.book_name.upper() or '960' in test.book_name.upper()


register.filter('spsa_is_rshogi', spsa_is_rshogi)
register.filter('spsa_progress', spsa_progress)
register.filter('spsa_trajectory', spsa_trajectory)
register.filter('spsa_param_digest', spsa_param_digest)
register.filter('spsa_param_digest_headers', spsa_param_digest_headers)
register.filter('spsa_original_input', spsa_original_input)
register.filter('spsa_optimal_values', spsa_optimal_values)

register.filter('book_download_link', book_download_link)
register.filter('book_display_name', book_display_name)
register.filter('network_download_link', network_download_link)

register.filter('workload_url', workload_url)
register.filter('workload_pretty_name', workload_pretty_name)

register.filter('git_diff_text', git_diff_text)

register.filter('test_is_smp_odds'  , test_is_smp_odds  )
register.filter('test_is_time_odds' , test_is_time_odds )
register.filter('test_is_fischer'   , test_is_fischer   )


@register.filter
def next(iterable, index):
    try: return iterable[int(index) + 1]
    except: return None

@register.filter
def previous(iterable, index):
    try: return iterable[int(index) - 1]
    except: return None
