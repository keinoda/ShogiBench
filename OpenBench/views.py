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

import io, os, hashlib, datetime, json, secrets, shlex, sys, re

import paramiko

import django.http
import django.shortcuts
import django.contrib.auth

import OpenBench.config
import OpenBench.utils
import OpenBench.model_utils

from OpenBench.workloads.create_workload import create_workload
from OpenBench.workloads.get_workload import get_workload
from OpenBench.workloads.modify_workload import modify_workload
from OpenBench.workloads.verify_workload import verify_workload
from OpenBench.workloads.view_workload import view_workload

from OpenBench.config import OPENBENCH_CONFIG, OPENBENCH_CONFIG_CHECKSUM, OPENBENCH_STATIC_VERSION
from OpenSite.settings import PROJECT_PATH

from OpenBench.models import *
from django.contrib.auth.models import User
from OpenSite.settings import MEDIA_ROOT

from django.db import transaction
from django.db.models import F, Q
from django.http import HttpResponse, JsonResponse, FileResponse
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
    ## engine-specific variants, then variants shared across all engines
    ## (registered under the pseudo-engine '*')

    variants = dict(OPENBENCH_CONFIG['engines'][engine]['build']['variants'])

    for variant in BuildVariant.objects.filter(engine=engine).order_by('name'):
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

            static_scope = OPENBENCH_CONFIG['engines'].keys() if engine == '*' else [engine]
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

def upload_worker_bootstrap(client, script_data):

    try:
        with client.open_sftp() as sftp:
            sftp.putfo(io.BytesIO(script_data), '/tmp/shogibench_setup.sh')
        return
    except Exception as sftp_error:
        try:
            stdin, stdout, stderr = client.exec_command(
                'cat > /tmp/shogibench_setup.sh', timeout=20)
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
        with open(script, 'rb') as fin:
            upload_worker_bootstrap(client, fin.read())

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
            'chmod +x /tmp/shogibench_setup.sh && '
            '( export %s ; nohup /tmp/shogibench_setup.sh '
            '> "$HOME/shogibench-worker.log" 2>&1 < /dev/null & ) && '
            'echo LAUNCHED'
        ) % (env_line)

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
        return JsonResponse({ 'error' : 'Bad Credentials' })

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
        return JsonResponse({ 'error' : 'Bad Credentials' })

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
        return JsonResponse({ 'error' : 'Bad Credentials' })

    # Create a new Machine for this session
    info    = json.loads(request.POST['system_info'])
    machine = OpenBench.utils.get_machine('None', user, info)

    # Save the machine's latest information and Secret Token for this session
    machine.info   = info
    machine.secret = secrets.token_hex(32)

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

    # Finish up
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

        # Format: test.result.book-index.pgn.bz2
        pgn            = PGN()
        pgn.test_id    = int(request.POST['test_id']   )
        pgn.result_id  = int(request.POST['result_id'] )
        pgn.book_index = int(request.POST['book_index'])
        pgn.save()

        # Save the .pgn.bz2 to /Media/
        FileSystemStorage().save(pgn.filename(), ContentFile(request.FILES['file'].read()))

    return JsonResponse({})

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def api_response(data):
    return HttpResponse(json.dumps(data, indent=4), content_type='application/json')

@csrf_exempt
def api_authenticate(request, require_enabled=False, allow_worker_key=False):

    try:

        # Force requiring an enabled user when require_login_to_view is set
        require_enabled = require_enabled or OPENBENCH_CONFIG['require_login_to_view']

        # Don't require a login for Public frameworks
        if not require_enabled:
            return True

        # Request is made from a browser, and is already logged in
        if request.user.is_authenticated:
            return Profile.objects.get(user=request.user).enabled

        # Request might be made from the command line. Check the headers
        user = django.contrib.auth.authenticate(
            username=request.POST['username'], password=request.POST['password'])

        # Workers download Networks with their Worker Key as the password
        if user is None and allow_worker_key:
            return authenticate_worker_key(
                request.POST['username'], request.POST['password']) is not None

        return Profile.objects.get(user=user).enabled

    except Exception:
        import traceback
        traceback.print_exc()
        return False

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
        if (network := Network.objects.filter(engine=engine, default=True).first()):
            default = OpenBench.model_utils.network_to_dict(network)

        networks = [
            OpenBench.model_utils.network_to_dict(network)
            for network in Network.objects.filter(engine=engine)
        ]

        return api_response({ 'default' : default, 'networks' : networks })

    else:
        return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' })

@csrf_exempt
def api_network_download(request, engine, identifier):

    if not api_authenticate(request, require_enabled=True, allow_worker_key=True):
        return api_response({ 'error' : 'API requires authentication for this endpoint' })

    if (network := Network.objects.filter(engine=engine, sha256=identifier).first()):
        return OpenBench.utils.network_download(request, engine, network)

    if (network := Network.objects.filter(engine=engine, name=identifier).first()):
        return OpenBench.utils.network_download(request, engine, network)

    return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' })

@csrf_exempt
def api_network_download_aux(request, engine, identifier, name):

    if not api_authenticate(request, require_enabled=True, allow_worker_key=True):
        return api_response({ 'error' : 'API requires authentication for this endpoint' })

    if not (network := OpenBench.utils.network_disambiguate(engine, identifier)):
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

    # 2. Make sure there actually is a PGN attached to the Workload
    pgn_path = FileSystemStorage(os.path.join(MEDIA_ROOT, 'PGNs')).path('%d.pgn.tar' % (pgn_id))
    if not os.path.exists(pgn_path):
        return api_response({ 'error' : 'Unable to find PGN for Workload #%d' % (pgn_id) })

    # 3. Make sure the workload is not currently running
    if not workload.finished:
        return api_response({ 'error' : 'PGNs cannot be downloaded while the Workload is active' })

    # 4. Make sure no active workers are still on this workload
    if OpenBench.utils.getRecentMachines().filter(workload=pgn_id):
        return api_response({ 'error' : 'Some machines are still on this Workload. Try again shortly' })

    # 5. Make sure there are no pending .pgn.bz2 files to be processed
    if PGN.objects.filter(test_id=pgn_id).filter(processed=False):
        return api_response({ 'error' : 'Still processing individual PGNs into the archive. Try again shortly' })

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
