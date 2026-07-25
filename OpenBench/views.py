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

import base64, binascii, io, os, hashlib, datetime, json, secrets, shlex, sys, re
from types import SimpleNamespace

import paramiko
import requests

import django.http
import django.shortcuts
import django.contrib.auth

import OpenBench.config
import OpenBench.utils
import OpenBench.model_utils
import OpenBench.pgn_archive

from OpenBench.workloads.create_workload import create_new_test, create_workload, finalize_workload_creation
from OpenBench.workloads.get_workload import get_workload
from OpenBench.workloads.modify_workload import modify_workload
from OpenBench.workloads.verify_workload import GithubAPIError, collect_github_branches, verify_workload
from OpenBench.workloads.view_workload import view_workload

from OpenBench.config import OPENBENCH_CONFIG, OPENBENCH_CONFIG_CHECKSUM, OPENBENCH_STATIC_VERSION
from OpenSite.settings import PROJECT_PATH

from OpenBench.models import *
from django.contrib.auth.models import User
from OpenSite.settings import MEDIA_ROOT

from django.db import transaction, IntegrityError
from django.db.models import Count, F, Q
from django.http import HttpResponse, JsonResponse, FileResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.core.files.storage import FileSystemStorage
from django.core.files.base import ContentFile
from django.utils import timezone

from wsgiref.util import FileWrapper

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                              GENERAL UTILITIES                              #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

ERROR_MESSAGES = {
    'disabled'            : 'Account has not been enabled. Contact an Administrator',
    'fakeuser'            : 'This is not a real OpenBench User. Create an OpenBench account',
    'requires_login'      : 'All pages require a user login to access',
    'manual_registration' : 'Registration can only be done via an Administrator',
}

class UnableToAuthenticate(Exception):
    pass

def render(request, template, content={}, always_allow=False, error=None, warning=None, status=None):

    data = content.copy()
    data.update({ 'config' : OPENBENCH_CONFIG })
    data.update({ 'static_version' : OPENBENCH_STATIC_VERSION })

    if OPENBENCH_CONFIG['require_login_to_view']:
        if not request.user.is_authenticated and not always_allow:
            return redirect(request, '/login/',  error=ERROR_MESSAGES['requires_login'])

    if request.user.is_authenticated:

        profile = Profile.objects.filter(user=request.user)
        data.update({'profile' : profile.first()})

        if profile.first() and not profile.first().enabled:
            request.session['error_message'] = ERROR_MESSAGES['disabled']

        elif request.user.is_authenticated and not profile.first():
            request.session['error_message'] = ERROR_MESSAGES['fakeuser']

    if error:
        request.session['error_message'] = error

    if warning:
        request.session['warning_message'] = error

    if status:
        request.session['status_message'] = status

    response = django.shortcuts.render(request, 'OpenBench/{0}'.format(template), data)

    for key in ['status_message', 'warning_message', 'error_message']:
        if key in request.session: del request.session[key]

    return response

def redirect(request, destination, error=None, warning=None, status=None):

    if error:
        request.session['error_message'] = error

    if warning:
        request.session['warning_message'] = warning

    if status:
        request.session['status_message'] = status

    return django.http.HttpResponseRedirect(destination)

def authenticate(request, requireEnabled=False):

    try:
        user = django.contrib.auth.authenticate(
            username = request.POST['username'],
            password = request.POST['password'])

        if requireEnabled:
            profile = OpenBench.models.Profile.objects.get(user=user)
            if not profile.enabled: raise UnableToAuthenticate()

    except Exception:
        raise UnableToAuthenticate()

    if user is None:
        raise UnableToAuthenticate()

    return user

def authenticate_worker_key(username, token):

    ## Returns the owning User when (username, token) matches an enabled
    ## Worker Key of an enabled account, else None. Failure reasons are
    ## printed (never the token itself) so a failing worker can be
    ## diagnosed from the server logs.

    username = (username or '').strip()
    token    = (token or '').strip()

    key = WorkerKey.objects.filter(token=token, enabled=True).first()

    if not key:
        print ('Worker auth failed: no enabled Worker Key matches the token supplied by %r' % (username), flush=True)
        return None

    # Token must be paired with the username of its owner
    if key.user.username.lower() != username.lower():
        print ('Worker auth failed: Key "%s" belongs to "%s", but username %r was supplied'
               % (key.name, key.user.username, username), flush=True)
        return None

    # Owner must still be an enabled user
    if not Profile.objects.filter(user=key.user, enabled=True).exists():
        print ('Worker auth failed: owner "%s" of Key "%s" is disabled' % (key.user.username, key.name), flush=True)
        return None

    key.last_used = timezone.now()
    key.save(update_fields=['last_used'])

    return key.user

def client_authenticate(request):

    ## Authentication for the client (worker) endpoints only. Workers may
    ## send either the account password, or a Worker Key token in place of
    ## the password. Worker Keys never grant access to the website itself.

    try:
        return authenticate(request, requireEnabled=True)
    except UnableToAuthenticate:
        pass

    user = authenticate_worker_key(request.POST.get('username'), request.POST.get('password'))
    if user is None:
        raise UnableToAuthenticate()

    return user

def credentials_look_revoked(username, token):

    ## 認証失敗のうち「ワーカーキーが無効化/削除された」(恒久的、ワーカーは
    ## 自己終了してよい) と、パスワード変更やアカウントの一時無効化など
    ## 可逆的な失敗を区別する。ワーカーキーのトークン形状 (token_hex(24))
    ## の資格情報だけを対象にするので、パスワード運用のワーカーは対象外

    token = (token or '').strip()

    if not re.match(r'^[0-9a-f]{48}$', token):
        return False # アカウントパスワードでの失敗 (可逆的)

    key = WorkerKey.objects.filter(token=token).first()

    if key is None:
        return True # キーが削除済み

    if not key.enabled:
        return True # キーが無効化済み

    return False # キー自体は有効 = アカウント側の一時的な問題 (可逆的)

def bad_credentials_response(request):

    ## 'Bad Credentials' 応答。キーの無効化/削除が原因のときだけ構造化フラグ
    ## 'shutdown' を立てる。ワーカーはこのフラグを見たときのみ exit 66 で
    ## 恒久停止する (文字列一致ではないので、可逆的な認証失敗を巻き込まない)

    response = { 'error' : 'Bad Credentials' }

    if credentials_look_revoked(request.POST.get('username'), request.POST.get('password')):
        response['shutdown'] = True

    return JsonResponse(response)

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                            ADMINISTRATIVE VIEWS                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def register(request):

    if OPENBENCH_CONFIG['require_manual_registration']:
        return redirect(request, '/login/', error=ERROR_MESSAGES['manual_registration'])

    if request.method == 'GET':
        return render(request, 'register.html', always_allow=True)

    if request.POST['password1'] != request.POST['password2']:
        return redirect(request, '/register/', error='Passwords do not match')

    if not request.POST['username'].isalnum():
        return redirect(request, '/register/', error='Alpha-numeric usernames Only')

    if User.objects.filter(username=request.POST['username']):
        return redirect(request, '/register/', error='That username is already taken')

    email    = request.POST['email']
    username = request.POST['username']
    password = request.POST['password1']

    user = User.objects.create_user(username, email, password)
    django.contrib.auth.login(request, user)
    Profile.objects.create(user=user)

    return redirect(request, '/index/')

def login(request):

    if request.method == 'GET':
        return render(request, 'login.html', always_allow=True)

    try:
        django.contrib.auth.login(request, authenticate(request))
        return redirect(request, '/index/')

    except UnableToAuthenticate:
        return redirect(request, '/login/', error='Unable to authenticate user')

def logout(request):

    django.contrib.auth.logout(request)
    return redirect(request, '/index/', status='Logged out')

def profile(request):

    if not request.user.is_authenticated:
        return redirect(request, '/login/')

    if not Profile.objects.filter(user=request.user).first():
        return redirect(request, '/index/')

    if request.method == 'GET':
        return render(request, 'profile.html')

    changes_message = ''
    if request.user.email != request.POST['email']:
        changes_message += 'Updated email address to %s' % (request.POST['email'])
        request.user.email = request.POST['email']
        request.user.save()

    if request.POST['password1'] != request.POST['password2']:
        return redirect(request, '/profile/', status=changes_message, error='Passwords do not match')

    if request.POST['password1']:
        request.user.set_password(request.POST['password1'])
        request.user.save()
        django.contrib.auth.login(request, request.user)
        changes_message += '\nUpdated password'

    return redirect(request, '/profile/', status=changes_message.removeprefix('\n'))

def profile_config(request):

    if not request.user.is_authenticated:
        return redirect(request, '/login/')

    if not (profile := Profile.objects.filter(user=request.user).first()):
        return redirect(request, 'index')

    if not profile.enabled:
        return redirect(request, '/profile/', error=ERROR_MESSAGES['disabled'])

    if request.method == 'GET':
        return render(request, 'profile.html')

    changes = ''

    if (engine := request.POST.get('default-status', profile.engine)) != profile.engine:
        changes += 'Set %s as the default, replacing %s\n' % (engine, profile.engine)
        profile.engine = engine

    for engine in json.loads(request.POST.get('deleted-repos', '[]')):
        profile.repos.pop(engine, False)
        changes += 'Deleted Engine: %s\n' % (engine)

    for (engine, current_repo) in profile.repos.items():
        repo_name = request.POST.get('engine-repo-%s' % (engine), '').removesuffix('/')
        repo = 'https://github.com/%s' % (repo_name)

        if repo != current_repo and repo_name:
            changes += 'Updated Engine: %s to use %s\n' % (engine, repo)
            profile.repos[engine] = repo

    if changes:
        profile.save()

    engine_name = request.POST.get('new-engine-name', 'None')
    engine_repo = request.POST.get('new-engine-repo', '').removesuffix('/')

    if engine_name != 'None' and engine_repo:

        if not engine_repo.startswith('https://github.com/'):
            return redirect(request, '/profile/', error='Repositories must be on Github')

        if not profile.engine:
            profile.engine = engine_name

        changes += 'Added Engine: %s at %s' % (engine_name, engine_repo)
        profile.repos[engine_name] = engine_repo
        profile.save()

    return redirect(request, '/profile/', status=changes)

def normalize_build_command(text):

    ## Turns a pasted build command into the make arguments ShogiBench
    ## stores for a Build Variant. Returns (args, dropped) where dropped
    ## lists the tokens that were removed because the worker manages them
    ## itself: the make invocation, -j (any form), and EXE=/EVALFILE=/CXX=/CC=.
    ##
    ## Multi-line pastes are handled: backslash continuations are joined,
    ## `cd` lines and `make clean` invocations are skipped, and the last
    ## remaining command is used. Quoting of arguments containing spaces
    ## (eg EXTRA_CPPFLAGS='-DA=1 -DB=2') survives the round-trip.

    # Join backslash-newline continuations, then split into commands
    joined   = re.sub(r'\\\s*\n', ' ', text.strip())
    commands = [
        part.strip()
        for line in joined.splitlines()
        for part in re.split(r'&&|;', line)
        if part.strip()
    ]

    # Skip directory changes and clean invocations
    candidates = []
    for command in commands:
        tokens = shlex.split(command)
        if not tokens or tokens[0] == 'cd':
            continue
        if 'clean' in tokens:
            continue
        candidates.append(tokens)

    if not candidates:
        raise ValueError('No build command found')

    kept, dropped, expect_jobs = [], [], False

    for index, token in enumerate(candidates[-1]):

        if index == 0 and token in ('make', 'gmake', 'mingw32-make', 'nmake'):
            dropped.append(token)
            continue

        # The count following a bare "-j", as in "make -j 8"
        if expect_jobs and re.match(r'^\d+$', token):
            dropped.append(token)
            expect_jobs = False
            continue
        expect_jobs = False

        # -j in any form: -j, -j8, -j"$(nproc)"
        if token.startswith('-j'):
            dropped.append(token)
            expect_jobs = token == '-j'
            continue

        if re.match(r'^(EXE|EVALFILE|CXX|CC|TARGET|TARGETDIR)=', token, re.IGNORECASE):
            dropped.append(token)
            continue

        kept.append(token)

    return ' '.join(shlex.quote(token) for token in kept), dropped

def engine_build_variants(engine):

    ## Static variants from the engine's json config, merged with the
    ## user-defined ones from the /builds/ page. Static names win, then
    ## variants for the selected engine, compatible engines, and finally
    ## variants shared across all engines (registered under '*')

    compatible_engines = OpenBench.utils.build_network_engines(engine)
    variants = {}

    for candidate in compatible_engines:
        for name, args in OPENBENCH_CONFIG['engines'][candidate]['build']['variants'].items():
            variants.setdefault(name, args)

    for candidate in compatible_engines:
        for variant in BuildVariant.objects.filter(engine=candidate).order_by('name'):
            variants.setdefault(variant.name, variant.args)

    for variant in BuildVariant.objects.filter(engine='*').order_by('name'):
        variants.setdefault(variant.name, variant.args)

    return variants

def builds(request):

    ## Manage user-defined Build Variants. Pasting a full build command
    ## normalizes it into make arguments automatically.

    if not request.user.is_authenticated:
        return redirect(request, '/login/')

    profile = Profile.objects.filter(user=request.user).first()
    if not profile or not profile.enabled:
        return redirect(request, '/index/', error='Only enabled users can manage Build Variants')

    if request.method == 'POST':

        action = request.POST.get('action')

        if action == 'create':

            engine  = request.POST.get('engine', '')
            name    = request.POST.get('name', '').strip()[:64]
            command = request.POST.get('command', '').strip()

            # '*' registers a variant shared by every engine
            if engine != '*' and engine not in OPENBENCH_CONFIG['engines']:
                return redirect(request, '/builds/', error='Unknown engine')

            if not re.match(r'^[\w.+()-]+$', name):
                return redirect(request, '/builds/', error='Variant names may only contain letters, numbers, and ._+()-')

            static_scope = (
                OPENBENCH_CONFIG['engines'].keys()
                if engine == '*'
                else OpenBench.utils.build_network_engines(engine)
            )
            for static_engine in static_scope:
                if name in OPENBENCH_CONFIG['engines'][static_engine]['build']['variants']:
                    return redirect(request, '/builds/', error='"%s" is a predefined variant of %s and cannot be changed' % (name, static_engine))

            if not command:
                return redirect(request, '/builds/', error='Provide a build command')

            try:
                args, dropped = normalize_build_command(command)
            except ValueError:
                return redirect(request, '/builds/', error='Unable to parse the build command')

            if len(args) > 512:
                return redirect(request, '/builds/', error='Build arguments are too long')

            existing = BuildVariant.objects.filter(engine=engine, name=name).first()
            if existing and existing.author != request.user.username and not profile.approver:
                return redirect(request, '/builds/', error='"%s" already exists and belongs to %s' % (name, existing.author))

            BuildVariant.objects.update_or_create(
                engine=engine, name=name,
                defaults={ 'args' : args, 'author' : request.user.username })

            status = 'Saved [%s] %s = "%s"' % (engine, name, args)
            if dropped:
                status += '\nRemoved (managed by the worker): %s' % (' '.join(dropped))
            return redirect(request, '/builds/', status=status)

        if action == 'delete':

            variant = BuildVariant.objects.filter(id=request.POST.get('variant_id', 0)).first()
            if not variant:
                return redirect(request, '/builds/', error='No such Build Variant')

            if variant.author != request.user.username and not profile.approver:
                return redirect(request, '/builds/', error='Only the author or an approver can delete this variant')

            variant.delete()
            return redirect(request, '/builds/', status='Deleted [%s] %s' % (variant.engine, variant.name))

        return redirect(request, '/builds/', error='Unknown action')

    data = {
        'variants' : BuildVariant.objects.all().order_by('engine', 'name'),
        'statics'  : {
            engine : OPENBENCH_CONFIG['engines'][engine]['build']['variants']
            for engine in OPENBENCH_CONFIG['engines']
        },
    }

    return render(request, 'builds.html', data)

def tunekits(request):

    ## .tune キットの一覧と新規作成。キットは「YaneuraOu 系ソースに TUNE マクロを
    ## 注入して探索パラメータを USI option 化する」パッチ定義で、SPSA 作成時に選ぶ

    import OpenBench.tune_kits

    if not request.user.is_authenticated:
        return redirect(request, '/login/')

    profile = Profile.objects.filter(user=request.user).first()
    if not profile or not profile.enabled:
        return redirect(request, '/index/', error='Only enabled users can manage Tune Kits')

    if request.method == 'POST' and request.POST.get('action') == 'create':

        engine    = request.POST.get('engine', '')
        name      = request.POST.get('name', '').strip()[:64]
        tune_text = request.POST.get('tune_text', '').replace('\r\n', '\n').replace('\r', '\n')

        if engine not in OPENBENCH_CONFIG['engines']:
            return redirect(request, '/tunekits/', error='Unknown engine')

        if not re.match(r'^[\w.+()-]+$', name):
            return redirect(request, '/tunekits/', error='Kit names may only contain letters, numbers, and ._+()-')

        if TuneKit.objects.filter(engine=engine, name=name).exists():
            return redirect(request, '/tunekits/', error='"%s" already exists for %s' % (name, engine))

        if not tune_text.strip():
            return redirect(request, '/tunekits/', error='.tune の内容を貼り付けてください')

        yotune = OpenBench.tune_kits.load_yotune()
        if not yotune.tune_files(tune_text):
            return redirect(request, '/tunekits/', error='.tune に "#set file" がありません')

        names = OpenBench.tune_kits.kit_param_names(tune_text)
        if not names:
            return redirect(request, '/tunekits/', error='.tune に @ マーカー付きのパラメータがありません')

        # .params は貼り付けがあればそれを .tune に同期、なければ .tune から生成
        params_text, report = OpenBench.tune_kits.sync_params(
            tune_text, request.POST.get('params_text', ''))

        kit = TuneKit.objects.create(
            engine=engine, name=name, author=request.user.username,
            tune_text=tune_text, params_text=params_text)

        LogEvent.objects.create(
            author=request.user.username, summary='TUNEKIT CREATE %s' % (name), log_file='', test_id=0)

        return redirect(request, '/tunekits/%d/' % (kit.id),
                        status='キット %s を作成しました (%d パラメータ)' % (name, len(names)))

    data = { 'kits' : TuneKit.objects.all().order_by('engine', 'name') }
    return render(request, 'tunekits.html', data)

def tunekit(request, pk):

    ## キット詳細: 編集・paramsの同期・ブランチ照合 (EXACT/NUMDRIFT/MISSING)・
    ## 数値ドリフトの自動追随・削除。バージョン (ブランチ) が進んだときは
    ## ここで「照合」→「自動追随」して .tune を現行ソースに合わせる

    import OpenBench.tune_kits

    if not request.user.is_authenticated:
        return redirect(request, '/login/')

    profile = Profile.objects.filter(user=request.user).first()
    if not profile or not profile.enabled:
        return redirect(request, '/index/', error='Only enabled users can manage Tune Kits')

    if not (kit := TuneKit.objects.filter(id=pk).first()):
        return redirect(request, '/tunekits/', error='No such Tune Kit')

    may_edit = profile.approver or kit.author == request.user.username

    data = {
        'kit'          : kit,
        'may_edit'     : may_edit,
        'param_names'  : OpenBench.tune_kits.kit_param_names(kit.tune_text),
        'check_repo'   : OPENBENCH_CONFIG['engines'][kit.engine]['source']
                             if kit.engine in OPENBENCH_CONFIG['engines'] else '',
        'check_branch' : '',
    }

    if request.method != 'POST':
        return render(request, 'tunekit.html', data)

    action = request.POST.get('action')

    # --- 誰でも実行できる読み取り系 (照合) ---

    if action == 'check':

        repo   = request.POST.get('repo', data['check_repo']).strip()
        branch = request.POST.get('branch', 'master').strip() or 'master'

        try:
            results, counts = OpenBench.tune_kits.check_kit(kit.tune_text, kit.engine, repo, branch)
            data['check_results'] = results
            data['check_counts']  = counts
            data['check_ok']      = not counts['NUMDRIFT'] and not counts['MISSING']
        except OpenBench.tune_kits.TuneSourceError as error:
            data['error'] = error.message

        data['check_repo'], data['check_branch'] = repo, branch
        return render(request, 'tunekit.html', data)

    # --- 以降は編集系 ---

    if not may_edit:
        return redirect(request, '/tunekits/%d/' % (kit.id), error='Only the author or an approver can edit this kit')

    if action == 'save':

        tune_text   = request.POST.get('tune_text', '').replace('\r\n', '\n').replace('\r', '\n')
        params_text = request.POST.get('params_text', '').replace('\r\n', '\n').replace('\r', '\n')

        if not tune_text.strip():
            return redirect(request, '/tunekits/%d/' % (kit.id), error='.tune を空にはできません')

        kit.tune_text, kit.params_text = tune_text, params_text
        kit.save()
        return redirect(request, '/tunekits/%d/' % (kit.id), status='保存しました')

    if action == 'sync_params':

        params_text, report = OpenBench.tune_kits.sync_params(kit.tune_text, kit.params_text)
        kit.params_text = params_text
        kit.save()

        status = '.params を .tune に同期しました (追加 %d / 引退 %d / 維持 %d)' % (
            len(report['added']), len(report['retired']), report['kept'])
        return redirect(request, '/tunekits/%d/' % (kit.id), status=status)

    if action == 'retune':

        repo   = request.POST.get('repo', data['check_repo']).strip()
        branch = request.POST.get('branch', 'master').strip() or 'master'

        try:
            new_text, report = OpenBench.tune_kits.retune_kit(kit.tune_text, kit.engine, repo, branch)
        except OpenBench.tune_kits.TuneSourceError as error:
            return redirect(request, '/tunekits/%d/' % (kit.id), error=error.message)

        kit.tune_text = new_text
        kit.save()

        status = '自動追随: %d ブロックを書き換え、%d は一致済み' % (len(report['auto']), len(report['exact']))
        if report['manual']:
            status += '\n手当てが必要 (MANUAL): %s' % (
                ', '.join('%s (%s)' % (name, why) for name, why in report['manual']))
        else:
            status += '\n全ブロックが現行ソースと一致しています'

        LogEvent.objects.create(
            author=request.user.username, summary='TUNEKIT RETUNE %s' % (kit.name), log_file='', test_id=0)

        return redirect(request, '/tunekits/%d/' % (kit.id), status=status)

    if action == 'delete':
        name = kit.name
        kit.delete()
        return redirect(request, '/tunekits/', status='キット %s を削除しました' % (name))

    return redirect(request, '/tunekits/%d/' % (kit.id), error='Unknown action')

def server_public_url(request):

    ## The URL workers must use to reach this server. Behind some proxies
    ## build_absolute_uri() reports http://, which gets 301-redirected and
    ## silently turns the client's POSTs into GETs — so prefer the configured
    ## public URL and only fall back to the request's own view of itself.

    from django.conf import settings
    url = getattr(settings, 'PUBLIC_URL', None) or request.build_absolute_uri('/')
    return url if url.endswith('/') else url + '/'

def load_server_ssh_key():

    ## The server logs into instances with one shared private key, provided
    ## by the admin: either the OPENBENCH_SSH_PRIVATE_KEY env var (on Fly:
    ## fly secrets set OPENBENCH_SSH_PRIVATE_KEY="$(cat key)") or a key file
    ## at settings.SSH_PRIVATE_KEY_FILE. The public half is registered with
    ## the provider (eg vast.ai's Account > SSH Keys), so every rented
    ## instance accepts it. Returns a paramiko key, or None if unset.

    from django.conf import settings

    material = getattr(settings, 'SSH_PRIVATE_KEY', '') or ''

    # Keys pasted through some UIs arrive with literal \n escapes
    if '\\n' in material and '\n' not in material:
        material = material.replace('\\n', '\n')

    if not material.strip():
        key_file = getattr(settings, 'SSH_PRIVATE_KEY_FILE', '')
        if key_file and os.path.exists(key_file):
            with open(key_file) as fin:
                material = fin.read()

    if not material.strip():
        return None

    for key_type in [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey]:
        try: return key_type.from_private_key(io.StringIO(material))
        except Exception: continue

    return None

def ssh_key_fingerprint(pkey):

    import base64
    digest = hashlib.sha256(pkey.asbytes()).digest()
    key_name = pkey.get_name().replace('ssh-', '').upper()
    return '%s SHA256:%s' % (key_name, base64.b64encode(digest).decode().rstrip('='))

def ssh_public_key_line(pkey):

    return '%s %s' % (pkey.get_name(), pkey.get_base64())

def parse_ssh_target(text):

    ## Accepts any of the formats vast.ai and users commonly paste:
    ##   "ssh -p 12345 root@ssh4.vast.ai"    (vast.ai's Connect button)
    ##   "root@ssh4.vast.ai:12345"
    ##   "ssh4.vast.ai:12345"
    ##   "203.0.113.7"                        (port defaults to 22)
    ## Returns (username, host, port), with username defaulting to root.

    text = text.strip()

    if (m := re.match(r'^ssh\s+(?:-p\s*(\d+)\s+)?(?:([\w.-]+)@)?([\w.-]+)(?:\s+-p\s*(\d+))?$', text)):
        port = int(m.group(1) or m.group(4) or 22)
        return (m.group(2) or 'root', m.group(3), port)

    if (m := re.match(r'^(?:([\w.-]+)@)?([\w.-]+)(?::(\d+))?$', text)):
        return (m.group(1) or 'root', m.group(2), int(m.group(3) or 22))

    raise ValueError('Unrecognized SSH target: %s' % (text))

def upload_worker_bootstrap(client, script_data, remote_path):

    ## Upload to a per-request temp path; the launch command mv's it into
    ## place atomically. Writing /tmp/shogibench_setup.sh directly would let
    ## a second connect truncate a script an earlier bash is still executing.

    try:
        with client.open_sftp() as sftp:
            sftp.putfo(io.BytesIO(script_data), remote_path)
        return
    except Exception as sftp_error:
        try:
            stdin, stdout, stderr = client.exec_command(
                'cat > %s' % (remote_path), timeout=20)
            stdin.write(script_data)
            stdin.flush()
            stdin.channel.shutdown_write()

            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                error = stderr.read()
                if isinstance(error, bytes):
                    error = error.decode('utf-8', 'replace')
                raise Exception('exit status %d: %s' % (exit_status, error.strip()))

        except Exception as exec_error:
            raise Exception(
                'Bootstrap upload failed: SFTP failed (%s); exec upload failed (%s)' %
                (sftp_error, exec_error))

def launch_worker_over_ssh(request, pkey, worker_key, target, threads):

    ## Connect to the instance with the server's key, upload the bootstrap
    ## script, and launch it detached with the connection settings for this
    ## server baked in. Returns a status string, or raises.

    user, host, port = parse_ssh_target(target)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            host, port=port, username=user, pkey=pkey,
            timeout=15, look_for_keys=False, allow_agent=False)

        # Upload our own copy of the bootstrap script, so nothing external is needed
        script = os.path.join(PROJECT_PATH, 'Deploy', 'worker', 'setup_worker.sh')
        remote_tmp = '/tmp/shogibench_setup.sh.%s' % (secrets.token_hex(8))
        with open(script, 'rb') as fin:
            upload_worker_bootstrap(client, fin.read(), remote_tmp)

        exports = {
            'OPENBENCH_SERVER'    : server_public_url(request),
            'OPENBENCH_USERNAME'  : worker_key.user.username,
            'OPENBENCH_PASSWORD'  : worker_key.token,
            'SHOGIBENCH_REPO_URL' : OPENBENCH_CONFIG['client_repo_url'],
            'SHOGIBENCH_REPO_REF' : OPENBENCH_CONFIG['client_repo_ref'],
        }

        if threads:
            exports['SHOGIBENCH_THREADS'] = str(int(threads))

        env_line = ' '.join('%s=%s' % (k, shlex.quote(v)) for k, v in exports.items())

        # The bootstrap runs fully detached in a subshell with all of its
        # descriptors pointed away from the SSH channel; otherwise the
        # long-lived worker process can hold the channel open forever.
        command = (
            'mv -f %s /tmp/shogibench_setup.sh && chmod +x /tmp/shogibench_setup.sh && '
            '( export SHOGIBENCH_PROTECTED_PIDS="$$ $PPID" %s ; nohup /tmp/shogibench_setup.sh '
            '> "$HOME/shogibench-worker.log" 2>&1 < /dev/null & ) && '
            'echo LAUNCHED'
        ) % (remote_tmp, env_line)

        # Read a single line rather than waiting for channel EOF, so a
        # stray descriptor on the remote side can never hang this request
        stdin, stdout, stderr = client.exec_command(command, timeout=20)
        output = stdout.readline()

        if isinstance(output, bytes):
            output = output.decode('utf-8', 'replace')

        if 'LAUNCHED' not in output:
            raise Exception('Bootstrap did not start (got: %s)' % (output.strip() or 'no output'))

        return 'Launched worker on %s:%d as %s. Logs: ~/shogibench-worker.log' % (host, port, user)

    finally:
        client.close()

def worker_connect(request):

    ## POST handler for the "Connect over SSH" form on the /workers/ page.

    if not request.user.is_authenticated:
        return redirect(request, '/login/')

    profile = Profile.objects.filter(user=request.user).first()
    if not profile or not profile.enabled:
        return redirect(request, '/index/', error='Only enabled users can connect Workers')

    if request.method != 'POST':
        return redirect(request, '/workers/')

    # A Worker Key is the API credential baked into the worker. Fall back
    # to any enabled key, and create one silently if the user has none
    worker_key = (
        WorkerKey.objects.filter(
            user=request.user, id=request.POST.get('key_id', 0), enabled=True).first()
        or WorkerKey.objects.filter(user=request.user, enabled=True).order_by('-id').first()
        or WorkerKey.objects.create(
            user=request.user, name='auto', token=secrets.token_hex(24)))

    if not (target := request.POST.get('ssh_target', '').strip()):
        return redirect(request, '/workers/', error='Provide the instance\'s SSH host and port')

    if not (pkey := load_server_ssh_key()):
        return redirect(request, '/workers/', error=
            'サーバーにSSH秘密鍵が設定されていません。管理者が一度だけ '
            'fly secrets set OPENBENCH_SSH_PRIVATE_KEY="$(cat <秘密鍵ファイル>)" '
            'を実行してください(vast.ai に公開鍵を登録済みの鍵)')

    try:
        status = launch_worker_over_ssh(
            request, pkey, worker_key, target, request.POST.get('threads', '').strip())
        return redirect(request, '/workers/', status=status)

    except ValueError as error:
        return redirect(request, '/workers/', error=str(error))

    except Exception as error:
        message = 'SSH connection failed: %s\n' % (error)
        message += 'Check that the host and port are correct, and that the key '
        message += 'configured on the server is authorized on the instance.'
        return redirect(request, '/workers/', error=message)

def workers(request):

    ## Manage Worker Keys, which are dedicated credentials for connecting
    ## remote worker machines (e.g. rented vast.ai instances). The page also
    ## displays copy-paste snippets for hooking a machine up to this server.

    if not request.user.is_authenticated:
        return redirect(request, '/login/')

    profile = Profile.objects.filter(user=request.user).first()
    if not profile or not profile.enabled:
        return redirect(request, '/index/', error='Only enabled users can manage Worker Keys')

    if request.method == 'POST':

        action = request.POST.get('action')

        if action == 'create':
            name  = request.POST.get('name', '').strip()[:64] or 'Unnamed'
            token = secrets.token_hex(24)
            WorkerKey.objects.create(user=request.user, name=name, token=token)
            return redirect(request, '/workers/', status='Created Worker Key "%s"' % (name))

        # Machines poll every ~30 seconds, so a stop flag on the Machine row
        # reaches them on their next report: current games are aborted and no
        # new workloads are served, freeing the instance without SSH access
        if action in ('stop_machine', 'resume_machine'):

            machine = Machine.objects.filter(id=request.POST.get('machine_id', 0)).first()
            if not machine:
                return redirect(request, '/workers/', error='No such Machine')

            if machine.user != request.user and not profile.approver:
                return redirect(request, '/workers/', error='Only the owner or an approver can control this Machine')

            machine.info['stop_requested'] = action == 'stop_machine'
            machine.save()

            if action == 'stop_machine':
                status = 'マシン #%d に停止を要求しました(次の通信、30秒以内に反映)。' % (machine.id)
                status += '長期間止める場合はワーカーキーの無効化もあわせて行ってください'
            else:
                status = 'マシン #%d の停止要求を解除しました' % (machine.id)

            return redirect(request, '/workers/', status=status)

        key = WorkerKey.objects.filter(user=request.user, id=request.POST.get('key_id', 0)).first()
        if not key:
            return redirect(request, '/workers/', error='No such Worker Key')

        if action == 'delete':
            key.delete()
            return redirect(request, '/workers/', status='Deleted Worker Key "%s"' % (key.name))

        if action in ('enable', 'disable'):
            key.enabled = action == 'enable'
            key.save(update_fields=['enabled'])
            return redirect(request, '/workers/', status='%sd Worker Key "%s"' % (action.capitalize(), key.name))

        return redirect(request, '/workers/', error='Unknown action')

    server_key = load_server_ssh_key()

    machines = OpenBench.utils.getRecentMachines()
    if not profile.approver:
        machines = machines.filter(user=request.user)

    data = {
        'keys'       : WorkerKey.objects.filter(user=request.user).order_by('-id'),
        'machines'   : machines.order_by('-id'),
        'server_url' : server_public_url(request),
        'ssh_key_configured'  : server_key is not None,
        'ssh_key_fingerprint' : ssh_key_fingerprint(server_key) if server_key else '',
        'ssh_public_key'      : ssh_public_key_line(server_key) if server_key else '',
    }

    return render(request, 'workers.html', data)

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                               TEST LIST VIEWS                               #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def index(request, page=1):

    pending   = OpenBench.utils.get_pending_tests()
    active    = OpenBench.utils.get_active_tests()
    completed = OpenBench.utils.get_completed_tests()
    awaiting  = OpenBench.utils.get_awaiting_tests()

    start, end, paging = OpenBench.utils.getPaging(completed, int(page), 'index')

    data = {
        'pending'   : pending,
        'active'    : active,
        'completed' : completed[start:end],
        'awaiting'  : awaiting,
        'paging'    : paging,
        'status'    : OpenBench.utils.getMachineStatus(),
    }

    return render(request, 'index.html', data)

def user(request, username, page=1):

    pending   = OpenBench.utils.get_pending_tests().filter(author=username)
    active    = OpenBench.utils.get_active_tests().filter(author=username)
    completed = OpenBench.utils.get_completed_tests().filter(author=username)
    awaiting  = OpenBench.utils.get_awaiting_tests().filter(author=username)

    start, end, paging = OpenBench.utils.getPaging(completed, int(page), 'user/%s' % (username))

    data = {
        'pending'   : pending,
        'active'    : active,
        'completed' : completed[start:end],
        'awaiting'  : awaiting,
        'paging'    : paging,
        'status'    : OpenBench.utils.getMachineStatus(username),
    }

    return render(request, 'index.html', data)

def greens(request, page=1):

    completed = OpenBench.utils.get_completed_tests().filter(passed=True)
    start, end, paging = OpenBench.utils.getPaging(completed, int(page), 'greens')

    data = { 'completed' : completed[start:end], 'paging' : paging }
    return render(request, 'index.html', data)

def search(request):

    if request.method == 'GET':
        return render(request, 'search.html', {})

    tests = Test.objects.all()

    # Optional Selection box filters

    if request.POST['author']:
        tests = tests.filter(author=request.POST['author'])

    if request.POST['engine']:
        tests = tests.filter(Q(base_engine=request.POST['engine']) | Q(dev_engine=request.POST['engine']))

    if request.POST['opening-book']:
        tests = tests.filter(book_name=request.POST['opening-book'])

    if request.POST['test-mode']:
        tests = tests.filter(test_mode=request.POST['test-mode'])

    if request.POST['syzygy-wdl']:
        tests = tests.filter(syzygy_wdl=request.POST['syzygy-wdl'])

    # Checkboxes for Test statuses

    if 'show-greens' not in request.POST:
        tests = tests.annotate(x=F('elolower') + F('eloupper')).exclude(x__gte=0, passed=True)

    if 'show-yellows' not in request.POST:
        tests = tests.exclude(failed=True, wins__gte=F('losses'))

    if 'show-reds' not in request.POST:
        tests = tests.exclude(failed=True, wins__lt=F('losses'))

    if 'show-blues' not in request.POST:
        tests = tests.annotate(x=F('elolower') + F('eloupper')).exclude(x__lt=0, passed=True)

    if 'show-stopped' not in request.POST:
        tests = tests.exclude(passed=False, failed=False)

    if 'show-deleted' not in request.POST:
        tests = tests.exclude(deleted=True)

    # Remaining filtering is hard to do with standard Django queries

    filtered = []
    keywords = request.POST['keywords'].upper().split()

    tc_type   = request.POST['tc-type']
    tc_value  = request.POST['tc-value-input']
    tc_select = request.POST['tc-value-select']

    # Attempt to parse the time control

    try:
        if tc_value:
            tc_value = OpenBench.utils.TimeControl.parse(tc_value)
    except:
        return redirect(request, '/search/', error='Invalid Time Control')

    # Filter out tests

    for test in tests:

        # None of the keywords appear in the dev branch name
        if keywords and not any(x in test.dev.name.upper() for x in keywords):
            continue

        # Determine the max number of threads that either engine used
        dev_threads  = OpenBench.utils.extract_option(test.dev_options, 'Threads')
        base_threads = OpenBench.utils.extract_option(test.base_options, 'Threads')
        max_threads  = max(int(dev_threads), int(base_threads))

        # Extract requsted configuration
        select_value = request.POST['threads-select']
        input_value  = int(request.POST['threads-input'])

        # Requested Threads value did not match observed value
        if select_value == '='  and max_threads != input_value: continue
        if select_value == '>=' and max_threads  < input_value: continue
        if select_value == '<=' and max_threads  > input_value: continue

        # Filter our undesired time control types
        if tc_type and tc_type != OpenBench.utils.TimeControl.control_type(test.dev_time_control):
            continue

        # Filter tests of the same time control type, but outside our range
        if tc_value:

            search_base = OpenBench.utils.TimeControl.control_base(tc_value)
            test_base   = OpenBench.utils.TimeControl.control_base(test.dev_time_control)

            if tc_select == '='  and search_base != test_base: continue
            if tc_select == '>=' and search_base  > test_base: continue
            if tc_select == '<=' and search_base  < test_base: continue

        filtered.append(test)

    error = 'No matching tests found' if not len(filtered) else None
    return render(request, 'search.html', { 'tests' : reversed(filtered) }, error=error)

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                           GENERAL DATA TABLE VIEWS                          #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def users(request):

    data = { 'profiles' : Profile.objects.order_by('-games', '-tests') }
    return render(request, 'users.html', data)

def event(request, pk):

    try:
        with open(os.path.join(MEDIA_ROOT, LogEvent.objects.get(id=pk).log_file)) as fin:
            return render(request, 'event.html', { 'content' : fin.read() })
    except:
        return redirect(request, '/index/', error='No logs for event exist')

def events_actions(request, page=1):

    events = LogEvent.objects.all().filter(machine_id=0).order_by('-id')
    start, end, paging = OpenBench.utils.getPaging(events, int(page), 'events')

    data = { 'events' : events[start:end], 'paging' : paging };
    return render(request, 'events.html', data)

def events_errors(request, page=1):

    events = LogEvent.objects.all().exclude(machine_id=0).order_by('-id')
    start, end, paging = OpenBench.utils.getPaging(events, int(page), 'errors')

    data = { 'events' : events[start:end], 'paging' : paging };
    return render(request, 'errors.html', data)

def machines(request, pk=None):

    if pk == None:
        data = { 'machines' : OpenBench.utils.getRecentMachines() }
        return render(request, 'machines.html', data)

    try:
        data = { 'machine' : OpenBench.models.Machine.objects.get(id=int(pk)) }
        return render(request, 'machine.html', data)

    except:
        return redirect(request, '/machines/', error='Machine does not exist')


# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                            TEST MANAGEMENT VIEWS                            #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def workload(request, workload_type, pk, action=None):

    if action != None:
        return modify_workload(request, pk, action)

    if not (workload := Test.objects.filter(id=int(pk)).first()):
        return redirect(request, '/index/', error='No such Workload exists')

    # Trying to view a Tune as a Test, for example
    if workload.workload_type_str() != workload_type:
        return django.http.HttpResponseRedirect('/%s/%d/' % (workload.workload_type_str(), int(pk)))

    return view_workload(request, workload, workload_type.upper())

def new_workload(request, workload_type):

    if workload_type.upper() not in [ 'TEST', 'TUNE', 'DATAGEN' ]:
        return redirect(request, '/index/', error='Unknown Workload type')

    return create_workload(request, workload_type.upper())

def github_branches(request):

    if not request.user.is_authenticated:
        return JsonResponse({ 'error' : 'ログインが必要です' }, status=401)

    profile = Profile.objects.filter(user=request.user).first()
    if not profile or not profile.enabled:
        return JsonResponse({ 'error' : '有効なユーザーのみ利用できます' }, status=403)

    if request.method != 'GET':
        return JsonResponse({ 'error' : 'GETリクエストのみ利用できます' }, status=405)

    try:
        branches, default_branch = collect_github_branches(
            request.GET.get('repo', ''), request.GET.get('engine', ''))
    except ValueError as error:
        return JsonResponse({ 'error' : str(error) }, status=400)
    except GithubAPIError as error:
        return JsonResponse({ 'error' : str(error) }, status=502)

    return JsonResponse({
        'branches'       : branches,
        'default_branch' : default_branch,
    })

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                          NETWORK MANAGEMENT VIEWS                           #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def networks(request, engine=None, action=None, name=None, client=False):

    # Without an identifier and a valid action, all we can do is view the list
    if not name or action.upper() not in ['UPLOAD', 'DEFAULT', 'DELETE', 'DOWNLOAD', 'EDIT']:
        networks = Network.objects.all()
        if engine and engine in OPENBENCH_CONFIG['engines'].keys():
            networks = networks.filter(engine=engine)
        return render(request, 'networks.html', { 'networks' : list(networks.order_by('-id').values()) })

    # Require logins. Clients will be artifically logged in
    if not request.user.is_authenticated:
        return django.http.HttpResponseRedirect('/login/')

    # Require approver credentials, unless downloading as a client
    if not client and not Profile.objects.get(user=request.user).approver:
        return django.http.HttpResponseRedirect('/index/')

    # Split out Uploads, since there is no logic to disambiguate the name
    if action.upper() == 'UPLOAD':
        return OpenBench.utils.network_upload(request, engine, name)

    # Push off all the actual effort to OpenBench.utils for all actions
    actions = {
        'DEFAULT'  : OpenBench.utils.network_default,
        'DELETE'   : OpenBench.utils.network_delete,
        'DOWNLOAD' : OpenBench.utils.network_download,
        'EDIT'     : OpenBench.utils.network_edit,
    }

    # Update the Network, if we can find one for the given name/sha256
    if (network := OpenBench.utils.network_disambiguate(engine, name)):
        return actions[action.upper()](request, engine, network)

    # Otherwise we could not find the Network, and cannot do anything
    return redirect(request, '/networks/', error='No network found with matching Sha')

def network_form(request):

    # Require logins. Clients will be artifically logged in
    if not request.user.is_authenticated:
        return django.http.HttpResponseRedirect('/login/')

    # Require approver credentials, unless downloading as a client
    if not Profile.objects.get(user=request.user).approver:
        return django.http.HttpResponseRedirect('/index/')

    # Get requests should not be reaching this point
    if request.method == 'GET':
        return render(request, 'uploadnet.html', {})

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                             OPENBENCH SCRIPTING                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

@csrf_exempt
def scripts(request):

    login(request) # All requests are attached to a User

    if request.POST['action'] == 'UPLOAD_NETWORK':
        engine = request.POST['engine']
        name   = request.POST['name']
        return networks(request, engine, 'upload', name)

    if request.POST['action'] == 'CREATE_TEST':
        return new_workload(request, "TEST")

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                              CLIENT HOOK VIEWS                              #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def verify_worker(function):

    def wrapped_verify_worker(*args, **kwargs):

        # Get the machine, assuming it exists
        try: machine = Machine.objects.get(id=int(args[0].POST['machine_id']))
        except: return JsonResponse({ 'error' : 'Bad Machine Id' })

        # Ensure the Client is using the same version as the Server
        if machine.info['client_ver'] != OPENBENCH_CONFIG['client_version']:
            expected_ver = OPENBENCH_CONFIG['client_version']
            return JsonResponse({ 'error' : 'Bad Client Version: Expected %d' % (expected_ver)})

        # Use the secret token as our soft verification
        if machine.secret != args[0].POST['secret']:
            return JsonResponse({ 'error' : 'Invalid Secret Token' })

        # Prompt the worker to soft-restart if its config is out of date
        if machine.info.get('OPENBENCH_CONFIG_CHECKSUM') != OPENBENCH_CONFIG_CHECKSUM:
            return JsonResponse({ 'error' : 'Server Configuration Changed' })

        # Otherwise, carry on, and pass along the machine
        return function(*args, machine)

    return wrapped_verify_worker

@csrf_exempt
def client_version_ref(request):

    # Verify the User's credentials or Worker Key
    try: user = client_authenticate(request)
    except UnableToAuthenticate:
        return bad_credentials_response(request)

    # Enough information to download the right Client
    return JsonResponse({
        'client_version'  : OPENBENCH_CONFIG['client_version' ],
        'client_repo_url' : OPENBENCH_CONFIG['client_repo_url'],
        'client_repo_ref' : OPENBENCH_CONFIG['client_repo_ref'],
    })

@csrf_exempt
def client_match_runner_version_ref(request):

    # Verify the User's credentials or Worker Key
    try: user = client_authenticate(request)
    except UnableToAuthenticate:
        return bad_credentials_response(request)

    # Enough information to build the right Fastchess version
    return JsonResponse({
        'fastchess_min_version' : OPENBENCH_CONFIG['fastchess_min_version'],
        'fastchess_repo_url'    : OPENBENCH_CONFIG['fastchess_repo_url'],
        'fastchess_repo_ref'    : OPENBENCH_CONFIG['fastchess_repo_ref'],
        'shogitest_min_version' : OPENBENCH_CONFIG['shogitest_min_version'],
        'shogitest_repo_url'    : OPENBENCH_CONFIG['shogitest_repo_url'],
        'shogitest_repo_ref'    : OPENBENCH_CONFIG['shogitest_repo_ref'],

        # SPSA (rshogi ラッパー) 用。ワーカーは SPSA ワークロードを受けたときに
        # このリポジトリから spsa バイナリをビルドする
        'rshogi_repo_url'       : OPENBENCH_CONFIG['rshogi_repo_url'],
        'rshogi_repo_ref'       : OPENBENCH_CONFIG['rshogi_repo_ref'],
    })

@csrf_exempt
def client_get_build_info(request):

    ## Information pulled from the config about how to build each engine.
    ## Toss in a private flag as well to indicate the need for Github Tokens.

    data = {}
    for engine, config in OPENBENCH_CONFIG['engines'].items():
        data[engine] = config['build'].copy()
        data[engine]['private'] = config['private']
    return JsonResponse(data)

@csrf_exempt
def client_worker_info(request):

    # Verify the User's credentials or Worker Key
    try: user = client_authenticate(request)
    except UnableToAuthenticate:
        return bad_credentials_response(request)

    # Create a new Machine for this session. If the worker sent its stable
    # per-instance token, reuse the existing row instead: re-registration
    # (crash loops, client updates) must not multiply the machine list
    info    = json.loads(request.POST['system_info'])
    token   = info.get('machine_token') or ''
    machine = None

    if token:
        machine = Machine.objects.filter(
            user=user, machine_token=token).order_by('-id').first()

        # 旧サーバ時代の行はカラムが空で JSON にだけトークンがあるので、
        # 一度だけそちらからも引き継ぐ (以後はカラムに載る)
        if machine is None:
            machine = Machine.objects.filter(
                user=user, info__machine_token=token).order_by('-id').first()

    if machine is None:
        machine = OpenBench.utils.get_machine('None', user, info)

    # Save the machine's latest information and Secret Token for this session
    machine.info          = info
    machine.secret        = secrets.token_hex(32)
    machine.machine_token = token

    # 再利用した行の workload は前セッションの値。残すと「そのテストを
    # まだ抱えている最近のマシン」に見え、SPSA の再割当や PGN API を
    # 数分間ブロックしてしまうのでクリアする
    machine.workload = 0

    # Note the Config checksum at the time of init, in case it changes
    machine.info['OPENBENCH_CONFIG_CHECKSUM'] = OPENBENCH_CONFIG_CHECKSUM

    # Remember which Worker Key opened this session: requests after init
    # authenticate with the session secret only, so this is what lets a
    # deleted or disabled key actually cut a running session off
    key = WorkerKey.objects.filter(
        token=(request.POST.get('password') or '').strip(), user=user).first()
    machine.info['worker_key_id'] = key.id if key else None

    # Tag engines that the Machine can build and/or run with binaries
    machine.info['supported'] = []
    for engine, data in OPENBENCH_CONFIG['engines'].items():

        # Must have all CPU flags, for both Public and Private engines
        if any([flag not in machine.info['cpu_flags'] for flag in data['build']['cpuflags']]):
            continue

        # Private engines must have, or think they have, a Git Token
        if data['private'] and engine not in machine.info['tokens'].keys():
            continue

        # Public engines must have a compiler of a sufficient version
        if not data['private'] and engine not in machine.info['compilers'].keys():
            continue

        # Must match the Operating Systems supported by the engine
        if machine.info['os_name'] not in data['build']['systems']:
            continue

        # All requirements are met, and this Machine can play with the given engine
        machine.info['supported'].append(engine)

    # Finish up. Two simultaneous first registrations with the same token can
    # race past the lookup above; the unique constraint turns the loser's
    # INSERT into an IntegrityError, and the loser adopts the winner's row
    try:
        with transaction.atomic():
            machine.save()
    except IntegrityError:
        winner = Machine.objects.filter(
            user=user, machine_token=token).order_by('-id').first()
        winner.info     = machine.info
        winner.secret   = machine.secret
        winner.workload = 0
        machine = winner
        machine.save()

    # Pass back the Machine Id, and Secret Token for this session
    return JsonResponse({ 'machine_id' : machine.id, 'secret' : machine.secret })

@csrf_exempt
def client_get_network(request, engine, name):

    # Verify the User's credentials or Worker Key
    try:
        user = client_authenticate(request)
        django.contrib.auth.login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    except UnableToAuthenticate: return HttpResponse('Bad Credentials')

    # Return the requested Neural Network file for the Client
    return networks(request, engine, 'DOWNLOAD', name, client=True)


@csrf_exempt
@verify_worker
def client_get_github_archive(request, machine):

    test = Test.objects.filter(id=request.POST.get('test_id', 0)).first()
    side = request.POST.get('side')

    if not test or side not in ('dev', 'base'):
        return HttpResponse('Invalid source request', status=400)

    # 非公開ソースは、そのテストを現在割り当てられている作成者本人の
    # workerにだけ渡す。他ユーザーのworkerへprivate codeを配布しない。
    if machine.workload != test.id or machine.user.username != test.author:
        return HttpResponse('Source access denied', status=403)

    repo        = getattr(test, '%s_repo' % side).rstrip('/')
    engine_name = getattr(test, '%s_engine' % side)
    engine      = getattr(test, side)

    if not OpenBench.utils.is_private_source(engine_name, repo):
        return HttpResponse('Source not found', status=404)

    try:
        expected_source = OpenBench.utils.private_source_archive(repo, engine.sha)
    except ValueError:
        return HttpResponse('Source not found', status=404)

    if engine.source != expected_source:
        return HttpResponse('Source not found', status=404)

    headers = OpenBench.utils.read_git_credentials(engine_name)
    if not headers:
        return HttpResponse('Private source token is not configured', status=503)

    match = re.fullmatch(
        r'https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)', repo)
    if not match:
        return HttpResponse('Source not found', status=404)

    url = OpenBench.utils.path_join(
        'https://api.github.com/repos', match.group(1), match.group(2),
        'zipball', engine.sha)

    try:
        upstream = requests.get(
            url, headers=headers, stream=True, timeout=(15, 300))
    except requests.RequestException:
        return HttpResponse('GitHub source download failed', status=502)

    if upstream.status_code != 200:
        upstream.close()
        return HttpResponse('GitHub source download failed', status=502)

    def chunks():
        try:
            for chunk in upstream.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response = StreamingHttpResponse(chunks(), content_type='application/zip')
    response['Content-Disposition'] = 'attachment; filename="%s-%s.zip"' % (
        match.group(2), engine.sha[:12])
    response['Cache-Control'] = 'private, no-store'
    return response


@csrf_exempt
@verify_worker
def client_get_workload(request, machine):
    return JsonResponse(get_workload(request, machine))

@csrf_exempt
@verify_worker
def client_bench_error(request, machine):

    # Find and stop the test with the bad bench
    test = Test.objects.get(id=int(request.POST['test_id']))
    test.finished = True; test.save()

    # Log the error into the Events table
    LogEvent.objects.create(
        author     = machine.user.username,
        summary    = request.POST['error'],
        log_file   = '',
        machine_id = int(request.POST['machine_id']),
        test_id    = int(request.POST['test_id']))

    return JsonResponse({})

@csrf_exempt
@verify_worker
def client_submit_nps(request, machine):

    # Update the NPS counters for the GUI views
    machine.mnps      = float(request.POST['nps'     ]) / 1e6;
    machine.dev_mnps  = float(request.POST['dev_nps' ]) / 1e6;
    machine.base_mnps = float(request.POST['base_nps']) / 1e6;
    machine.save()

    # Pass back an empty JSON response
    return JsonResponse({})

@csrf_exempt
@verify_worker
def client_submit_error(request, machine):

    ## Report an error when working on test. This could be one three kinds.
    ## 1. Error building the engine. Does not compile, for whatever reason.
    ## 2. Error getting the artifacts. Does not exist, lacks credentials.
    ## 3. Error during actual gameplay. Timeloss, Disconnect, Crash, etc.

    # Log the Error into the Events table
    event = LogEvent.objects.create(
        author     = machine.user.username,
        summary    = request.POST['error'],
        log_file   = '',
        machine_id = int(request.POST['machine_id']),
        test_id    = int(request.POST['test_id']))

    # Save the Logs to /Media/ to be viewed later
    logfile = ContentFile(request.POST['logs'])
    FileSystemStorage().save('event%d.log' % (event.id), logfile)
    event.log_file = 'event%d.log' % (event.id); event.save()

    return JsonResponse({})

@csrf_exempt
@verify_worker
def client_submit_results(request, machine):

    # Returns {}, or { 'stop' : True }
    response = OpenBench.utils.update_test(request, machine)

    # Stops requested from the /workers/ page, and revoked Worker Keys,
    # abort the current games
    if machine.info.get('stop_requested') or OpenBench.utils.machine_key_revoked(machine):
        response['stop'] = True

    return JsonResponse(response)

@csrf_exempt
@verify_worker
def client_submit_spsa(request, machine):

    # rshogi spsa を回しているワーカーからの進捗報告。
    # Returns {}, or { 'stop' : True }
    response = OpenBench.utils.update_spsa_workload(request, machine)

    # Stops requested from the /workers/ page, and revoked Worker Keys,
    # abort the current tuning run (state is preserved for resume)
    if machine.info.get('stop_requested') or OpenBench.utils.machine_key_revoked(machine):
        response['stop'] = True

    return JsonResponse(response)

@csrf_exempt
@verify_worker
def client_heartbeat(request, machine):

    # Force a refresh of the updated timestamp
    machine.save()

    # Stops requested from the /workers/ page, and revoked Worker Keys,
    # abort the current games
    if machine.info.get('stop_requested') or OpenBench.utils.machine_key_revoked(machine):
        return JsonResponse({ 'stop' : True })

    # Include a 'stop' header iff the test was finished
    test = Test.objects.get(id=int(request.POST['test_id']))
    return JsonResponse([{}, { 'stop' : True }][test.finished])

@csrf_exempt
@verify_worker
def client_submit_pgn(request, machine):

    with transaction.atomic():

        # part は同じ割当を定期回収する連番。再送時は既存行を返して
        # 同じ棋譜をtarへ二重追加しない。未指定の旧クライアントは part=0。
        values = {
            'test_id'    : int(request.POST['test_id']),
            'result_id'  : int(request.POST['result_id']),
            'book_index' : int(request.POST['book_index']),
            'part'       : int(request.POST.get('part', 0)),
        }
        if values['part'] < 0:
            return JsonResponse({ 'error' : 'PGN part must be zero or greater' })

        pgn, created = PGN.objects.get_or_create(**values)

        # 応答消失後の再送では、最初の受信済みファイルをそのまま採用する。
        if not created:
            return JsonResponse({})

        # Save the .pgn.bz2 to /Media/
        FileSystemStorage().save(pgn.filename(), ContentFile(request.FILES['file'].read()))

    return JsonResponse({})

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def api_response(data, status=200):
    return HttpResponse(
        json.dumps(data, indent=4),
        content_type='application/json',
        status=status,
    )

def api_credentials(request, data=None):

    authorization = request.META.get('HTTP_AUTHORIZATION', '')
    scheme, _, encoded = authorization.partition(' ')

    if scheme.lower() == 'basic' and encoded:
        try:
            decoded = base64.b64decode(encoded, validate=True).decode('utf-8')
            username, separator, password = decoded.partition(':')
            if not separator:
                return None, None
            return username, password
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None, None

    data = request.POST if data is None else data
    return data.get('username'), data.get('password')

def api_authenticated_user(request, data=None, allow_worker_key=False):

    if request.user.is_authenticated:
        return request.user

    username, password = api_credentials(request, data)
    if not username or password is None:
        return None

    user = django.contrib.auth.authenticate(username=username, password=password)

    if user is None and allow_worker_key:
        user = authenticate_worker_key(username, password)

    return user

@csrf_exempt
def api_authenticate(request, require_enabled=False, allow_worker_key=False):

    # Force requiring an enabled user when require_login_to_view is set
    require_enabled = require_enabled or OPENBENCH_CONFIG['require_login_to_view']

    # Don't require a login for Public frameworks
    if not require_enabled:
        return True

    user = api_authenticated_user(request, allow_worker_key=allow_worker_key)
    return bool(user and Profile.objects.filter(user=user, enabled=True).exists())

TEST_API_REQUIRED_FIELDS = (
    'dev_engine', 'dev_repo', 'dev_branch', 'dev_network',
    'dev_options', 'dev_time_control',
    'base_engine', 'base_repo', 'base_branch', 'base_network',
    'base_options', 'base_time_control',
    'book_name', 'upload_pgns', 'test_mode',
    'priority', 'throughput', 'workload_size',
    'scale_method', 'scale_nps',
    'syzygy_wdl', 'syzygy_adj', 'win_adj', 'draw_adj',
)

TEST_API_STATUSES = ('current', 'pending', 'awaiting', 'active', 'completed', 'all')
TEST_API_DEFAULT_LIMIT = 50
TEST_API_MAX_LIMIT = 200

def api_test_payload(request):

    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, 'Request body must be valid UTF-8 JSON'

        if not isinstance(data, dict):
            return None, 'JSON request body must be an object'

    else:
        data = request.POST.dict()

    normalized = {}
    for key, value in data.items():
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None, 'Field %s must be a string or number' % key
        normalized[key] = str(value)

    return normalized, None

def api_test_status(test):

    if test.finished:
        return 'completed'
    if test.awaiting:
        return 'awaiting'
    if not test.approved:
        return 'pending'
    return 'active'

def api_test_engine(test, side):

    engine = getattr(test, side)

    return {
        'engine'       : getattr(test, side + '_engine'),
        'repo'         : getattr(test, side + '_repo'),
        'branch'       : engine.name,
        'sha'          : engine.sha,
        'bench'        : engine.bench,
        'display'      : getattr(test, side + '_display'),
        'network'      : {
            'sha256' : getattr(test, side + '_network'),
            'name'   : getattr(test, side + '_netname'),
        },
        'build'        : {
            'name' : getattr(test, side + '_build_name'),
            'args' : getattr(test, side + '_build_args'),
        },
        'options'      : getattr(test, side + '_options'),
        'time_control' : getattr(test, side + '_time_control'),
        'ponder_mode'  : getattr(test, side + '_ponder_mode'),
    }

def api_test_mode_config(test):

    if test.test_mode == 'SPRT':
        return {
            'elo_bounds' : [test.elolower, test.eloupper],
            'confidence' : {
                'alpha' : test.alpha,
                'beta'  : test.beta,
            },
            'llr' : {
                'lower'   : test.lowerllr,
                'current' : test.currentllr,
                'upper'   : test.upperllr,
            },
        }

    if test.test_mode in ('GAMES', 'DATAGEN'):
        return { 'max_games' : test.max_games }

    if test.test_mode == 'SPSA':
        spsa       = test.spsa or {}
        parameters = spsa.get('parameters', {})
        progress   = spsa.get('progress', {}) or {}

        return {
            'wrapper'         : spsa.get('wrapper'),
            'iterations'      : spsa.get('iterations'),
            'pairs_per'       : spsa.get('pairs_per'),
            'total_pairs'     : spsa.get('total_pairs'),
            'batch_pairs'     : spsa.get('batch_pairs'),
            'parameter_count' : len(parameters) if isinstance(parameters, dict) else 0,
            'progress'        : {
                'completed_pairs'   : progress.get('completed_pairs', 0),
                'completed_batches' : progress.get('completed_batches', 0),
                'updated_at'        : progress.get('updated_at'),
            },
        }

    return {}

def api_test_to_dict(request, test):

    workload_type = test.workload_type_str()

    return {
        'id'            : test.id,
        'url'           : request.build_absolute_uri('/%s/%d/' % (workload_type, test.id)),
        'author'        : test.author,
        'status'        : api_test_status(test),
        'workload_type' : workload_type,
        'test_mode'     : test.test_mode,
        'created_at'    : test.creation.isoformat(),
        'updated_at'    : test.updated.isoformat(),
        'flags'         : {
            'approved' : test.approved,
            'awaiting' : test.awaiting,
            'finished' : test.finished,
            'passed'   : test.passed,
            'failed'   : test.failed,
            'error'    : test.error,
        },
        'engines' : {
            'dev'  : api_test_engine(test, 'dev'),
            'base' : api_test_engine(test, 'base'),
        },
        'settings' : {
            'book_name'     : test.book_name,
            'upload_pgns'   : test.upload_pgns,
            'priority'      : test.priority,
            'throughput'    : test.throughput,
            'workload_size' : test.workload_size,
            'scale_method'  : test.scale_method,
            'scale_nps'     : test.scale_nps,
            'syzygy_wdl'    : test.syzygy_wdl,
            'syzygy_adj'    : test.syzygy_adj,
            'win_adj'       : test.win_adj,
            'draw_adj'      : test.draw_adj,
        },
        'mode_config' : api_test_mode_config(test),
        'results'     : {
            'games'  : test.games,
            'wins'   : test.wins,
            'losses' : test.losses,
            'draws'  : test.draws,
            'pentanomial' : {
                'LL' : test.LL,
                'LD' : test.LD,
                'DD' : test.DD,
                'DW' : test.DW,
                'WW' : test.WW,
            },
        },
    }

def api_get_tests(request):

    user = api_authenticated_user(request)
    if user is None:
        return api_response({ 'error' : 'Invalid API credentials' }, status=401)

    profile = Profile.objects.filter(user=user).first()
    if profile is None or not profile.enabled:
        return api_response({ 'error' : 'Only enabled users can view tests' }, status=403)

    status_filter = request.GET.get('status', 'current').strip().lower()
    if status_filter not in TEST_API_STATUSES:
        return api_response({
            'error'   : 'Invalid query parameter',
            'details' : ['status must be one of: %s' % ', '.join(TEST_API_STATUSES)],
        }, status=400)

    try:
        limit  = int(request.GET.get('limit', TEST_API_DEFAULT_LIMIT))
        offset = int(request.GET.get('offset', 0))
    except (TypeError, ValueError):
        return api_response({
            'error'   : 'Invalid query parameter',
            'details' : ['limit and offset must be integers'],
        }, status=400)

    parameter_errors = []
    if limit < 1 or limit > TEST_API_MAX_LIMIT:
        parameter_errors.append('limit must be between 1 and %d' % TEST_API_MAX_LIMIT)
    if offset < 0:
        parameter_errors.append('offset must be zero or greater')
    if parameter_errors:
        return api_response({
            'error'   : 'Invalid query parameter',
            'details' : parameter_errors,
        }, status=400)

    author = request.GET.get('author', '').strip()
    tests  = Test.objects.exclude(deleted=True)
    if author:
        tests = tests.filter(author=author)

    counts = tests.aggregate(
        all_count       = Count('id'),
        current_count   = Count('id', filter=Q(finished=False)),
        pending_count   = Count('id', filter=Q(finished=False, awaiting=False, approved=False)),
        awaiting_count  = Count('id', filter=Q(finished=False, awaiting=True)),
        active_count    = Count('id', filter=Q(finished=False, awaiting=False, approved=True)),
        completed_count = Count('id', filter=Q(finished=True)),
    )

    if status_filter == 'current':
        filtered = tests.filter(finished=False)
    elif status_filter == 'pending':
        filtered = tests.filter(finished=False, awaiting=False, approved=False)
    elif status_filter == 'awaiting':
        filtered = tests.filter(finished=False, awaiting=True)
    elif status_filter == 'active':
        filtered = tests.filter(finished=False, awaiting=False, approved=True)
    elif status_filter == 'completed':
        filtered = tests.filter(finished=True)
    else:
        filtered = tests

    if status_filter == 'active':
        filtered = filtered.order_by('-priority', '-currentllr', '-creation', '-id')
    elif status_filter == 'completed':
        filtered = filtered.order_by('-updated', '-id')
    else:
        filtered = filtered.order_by('-creation', '-id')

    total     = filtered.count()
    workloads = list(filtered.select_related('dev', 'base')[offset:offset + limit])

    return api_response({
        'query' : {
            'status' : status_filter,
            'author' : author or None,
        },
        'summary' : {
            'all'       : counts['all_count'],
            'current'   : counts['current_count'],
            'pending'   : counts['pending_count'],
            'awaiting'  : counts['awaiting_count'],
            'active'    : counts['active_count'],
            'completed' : counts['completed_count'],
        },
        'pagination' : {
            'total'    : total,
            'limit'    : limit,
            'offset'   : offset,
            'returned' : len(workloads),
            'has_more' : offset + len(workloads) < total,
        },
        'tests' : [api_test_to_dict(request, test) for test in workloads],
    })

@csrf_exempt
def api_tests(request):

    if request.method == 'GET':
        return api_get_tests(request)

    if request.method != 'POST':
        response = api_response({ 'error' : 'GET or POST requests only' }, status=405)
        response['Allow'] = 'GET, POST'
        return response

    data, error = api_test_payload(request)
    if error:
        return api_response({ 'error' : error }, status=400)

    user = api_authenticated_user(request, data)
    if user is None:
        return api_response({ 'error' : 'Invalid API credentials' }, status=401)

    profile = Profile.objects.filter(user=user).first()
    if profile is None or not profile.enabled:
        return api_response({ 'error' : 'Only enabled users can create tests' }, status=403)

    required = list(TEST_API_REQUIRED_FIELDS)
    if data.get('test_mode') == 'SPRT':
        required.extend(['test_bounds', 'test_confidence'])
    elif data.get('test_mode') == 'GAMES':
        required.append('test_max_games')

    missing = sorted(field for field in required if field not in data)
    if missing:
        return api_response({
            'error'   : 'Missing required fields',
            'details' : missing,
        }, status=400)

    api_request = SimpleNamespace(user=user, POST=data)

    with transaction.atomic():
        workload, errors = create_new_test(api_request)
        if errors:
            return api_response({
                'error'   : 'Test validation failed',
                'details' : errors,
            }, status=400)

        warning = finalize_workload_creation(api_request, workload)

    result = {
        'test' : {
            'id'       : workload.id,
            'url'      : request.build_absolute_uri('/test/%d/' % workload.id),
            'author'   : workload.author,
            'approved' : workload.approved,
            'awaiting' : workload.awaiting,
        },
    }

    if warning:
        result['warning'] = warning

    return api_response(result, status=201)

@csrf_exempt
def api_configs(request, engine=None):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    if engine == None:
        engines = list(OPENBENCH_CONFIG['engines'].keys())
        books   = OPENBENCH_CONFIG['books']
        return api_response({ 'engines' : engines, 'books' : books })

    if engine in OPENBENCH_CONFIG['engines'].keys():
        return api_response(OPENBENCH_CONFIG['engines'][engine])

    return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' })

@csrf_exempt
def api_networks(request, engine):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    if engine in OPENBENCH_CONFIG['engines'].keys():

        default = None
        if (network := OpenBench.utils.network_for_engine(engine, default=True)):
            default = OpenBench.model_utils.network_to_dict(network)

        networks = [
            OpenBench.model_utils.network_to_dict(network)
            for network in Network.objects.filter(
                engine__in=OpenBench.utils.build_network_engines(engine))
        ]

        return api_response({ 'default' : default, 'networks' : networks })

    else:
        return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' })

@csrf_exempt
def api_network_download(request, engine, identifier):

    if not api_authenticate(request, require_enabled=True, allow_worker_key=True):
        return api_response({ 'error' : 'API requires authentication for this endpoint' })

    if (network := OpenBench.utils.network_for_engine(engine, sha256=identifier)):
        return OpenBench.utils.network_download(request, engine, network)

    if (network := OpenBench.utils.network_for_engine(engine, name=identifier)):
        return OpenBench.utils.network_download(request, engine, network)

    return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' })

@csrf_exempt
def api_network_download_aux(request, engine, identifier, name):

    if not api_authenticate(request, require_enabled=True, allow_worker_key=True):
        return api_response({ 'error' : 'API requires authentication for this endpoint' })

    network = OpenBench.utils.network_for_engine(engine, name=identifier)
    network = network or OpenBench.utils.network_for_engine(engine, sha256=identifier)
    if not network:
        return api_response({ 'error' : 'Network %s for Engine %s not found' % (identifier, engine) })

    if not (aux := network.aux_files.filter(name=name).first()):
        return api_response({ 'error' : 'Network %s has no auxiliary file %s' % (identifier, name) })

    return OpenBench.utils.network_download_aux(request, engine, aux)

@csrf_exempt
def api_network_delete(request, engine, identifier):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    if not api_authenticate(request, require_enabled=True):
        return api_response({ 'error' : 'API requires authentication for this endpoint' })

    if not (network := OpenBench.utils.network_disambiguate(engine, identifier)):
        return api_response({ 'error' : 'Network %s for Engine %s not found' % (identifier, engine) })

    message, success = OpenBench.model_utils.network_delete(network)
    return api_response({ 'success' if success else 'error' : message })

@csrf_exempt
def api_build_info(request):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    data = {}
    for engine, config in OPENBENCH_CONFIG['engines'].items():
        data[engine] = config

    for network in Network.objects.filter(default=True):

        if network.engine not in data:
            continue

        data[network.engine]['network'] = {
            'sha'     : network.sha256,
            'name'    : network.name,
            'author'  : network.author,
            'created' : str(network.created)
        }

    return api_response(data)

@csrf_exempt
def api_pgns(request, pgn_id):

    # 0. Make sure the request has the correct permissions
    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    # 1. Make sure the workload actually exists for the requested PGN
    try: workload = Test.objects.get(pk=pgn_id)
    except: return api_response({ 'error' : 'Requested Workload Id does not exist' })

    status = OpenBench.pgn_archive.archive_status(workload)
    errors = {
        'disabled'   : 'PGN storage was disabled for Workload #%d' % pgn_id,
        'active'     : 'PGNs cannot be downloaded while the Workload is active',
        'waiting'    : 'Some machines are still on this Workload. Try again shortly',
        'processing' : 'Still processing individual PGNs into the archive. Try again shortly',
        'missing'    : 'No PGNs were received for Workload #%d' % pgn_id,
    }
    if status != 'ready':
        return api_response({ 'error' : errors[status], 'archive_status' : status })

    pgn_path = OpenBench.pgn_archive.archive_path(pgn_id)

    # Craft the download HTML response
    fwrapper = FileWrapper(open(pgn_path, 'rb'), 8192)
    response = FileResponse(fwrapper, content_type='application/octet-stream')

    # Set all headers and return response
    response['Expires'] = -1
    response['Content-Length'] = os.path.getsize(pgn_path)
    response['Content-Disposition'] = 'attachment; filename=%d.pgn.tar' % (pgn_id)
    return response

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                BUSINESS VIEWS                               #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def buyEthereal(request):
    return render(request, 'buyEthereal.html')
