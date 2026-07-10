# .tune キットのサーバ側ロジック。
#
# 解析・照合・追随の実体は Client/yotune.py (ワーカーと共用する純ロジック) にあり、
# ここでは GitHub からの対象ソース取得と、GUI 向けの操作 (照合 / 自動追随 /
# params 同期 / SPSA 作成時の検証) をまとめる。

import importlib.util
import os
import sys

import requests

from OpenSite.settings import PROJECT_PATH

import OpenBench.utils
from OpenBench.config import OPENBENCH_CONFIG


def load_yotune():

    ## Client/yotune.py を単一ソースとして読み込む (ワーカーと同じ実装を使う)。
    ## Client/ を sys.path に足すと utils.py 等が紛れ込むので、ファイル単位でロードする

    if 'shogibench_yotune' in sys.modules:
        return sys.modules['shogibench_yotune']

    path = os.path.join(PROJECT_PATH, 'Client', 'yotune.py')
    spec = importlib.util.spec_from_file_location('shogibench_yotune', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules['shogibench_yotune'] = module
    return module


class TuneSourceError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


def fetch_tune_sources(engine, repo_url, ref, files):

    ## .tune の '#set file' が指すソースを GitHub contents API から取得する。
    ## パスはエンジン設定の build.path (例: 'source') 起点。
    ## 返り値: { '#set file のパス' : ソース全文 }

    if engine not in OPENBENCH_CONFIG['engines']:
        raise TuneSourceError('Unknown engine: %s' % (engine))

    base_path = OPENBENCH_CONFIG['engines'][engine]['build'].get('path', '')
    api_base  = repo_url.rstrip('/').replace('github.com', 'api.github.com/repos')

    if not api_base.startswith('https://api.github.com/'):
        raise TuneSourceError('ソースは https://github.com/ 上のリポジトリだけ照合できます')

    headers = OpenBench.utils.read_git_credentials(engine) or {}
    headers['Accept'] = 'application/vnd.github.raw+json'

    sources = {}
    for file in files:

        path = '/'.join(x for x in [base_path.strip('/'), file.strip('/')] if x)
        url  = OpenBench.utils.path_join(api_base, 'contents', path) + '?ref=%s' % (ref)

        try:
            response = requests.get(url, headers=headers, timeout=30)
        except requests.exceptions.RequestException as error:
            raise TuneSourceError('GitHub からソースを取得できません (%s): %s' % (file, error))

        if response.status_code == 404:
            raise TuneSourceError('%s が %s (ref=%s) に見つかりません' % (path, repo_url, ref))

        if response.status_code != 200:
            raise TuneSourceError('GitHub API error %d while fetching %s' % (response.status_code, path))

        sources[file] = response.text

    return sources


def check_kit(tune_text, engine, repo_url, ref):

    ## キットの全 context と挿入マーカーをブランチの現行ソースと照合する。
    ## 返り値: (results, counts) - yotune.check_contexts の結果と件数集計

    yotune  = load_yotune()
    files   = yotune.tune_files(tune_text)

    if not files:
        raise TuneSourceError('.tune に "#set file" がありません')

    sources = fetch_tune_sources(engine, repo_url, ref, files)
    results = yotune.check_contexts(tune_text, sources)
    results += yotune.check_markers(tune_text, sources)
    return results, yotune.summarize_check(results)


def retune_kit(tune_text, engine, repo_url, ref):

    ## NUMDRIFT ブロックを現行ソースへ自動追随した tune_text を返す。
    ## 返り値: (new_tune_text, report)

    yotune  = load_yotune()
    files   = yotune.tune_files(tune_text)
    sources = fetch_tune_sources(engine, repo_url, ref, files)
    return yotune.retune(tune_text, sources)


def sync_params(tune_text, params_text):

    ## .tune のパラメータ集合へ .params を同期する。
    ## 返り値: (params_text, report)

    yotune = load_yotune()
    return yotune.params_from_tune(tune_text, params_text)


def kit_param_names(tune_text):

    ## .tune が定義するパラメータ名の集合

    yotune = load_yotune()
    return [name for name, value in yotune.tune_param_names(tune_text)]


def verify_kit_matches_branch(kit, repo_url, ref):

    ## SPSA 作成時のゲート: キットが対象ブランチと全 EXACT かを確認し、
    ## 問題を人間向けメッセージの一覧で返す (空なら合格)。
    ## ここで止めることで、ワーカーの TUNE ビルドが replaced count = 0 で
    ## 失敗する事故を作成時に防ぐ

    try:
        results, counts = check_kit(kit.tune_text, kit.engine, repo_url, ref)
    except TuneSourceError as error:
        return ['.tuneキット照合に失敗しました: %s' % (error.message)]

    yotune = load_yotune()
    errors = []

    if counts[yotune.NUMDRIFT] or counts[yotune.MISSING]:
        errors.append(
            '.tuneキット "%s" が %s からずれています (NUMDRIFT %d / MISSING %d)。'
            'キットページで「照合」→「数値ドリフトを自動追随」で更新してください'
            % (kit.name, ref, counts[yotune.NUMDRIFT], counts[yotune.MISSING]))
        for entry in results:
            if entry['status'] != yotune.EXACT:
                errors.append('  [%s] %s: %s' % (entry['status'], entry['name'], entry['detail']))

    return errors
