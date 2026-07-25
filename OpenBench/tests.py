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

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

import base64
import bz2
import hashlib
import io
import json
import os
import tarfile
import tempfile
import threading

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from OpenBench.models import BuildVariant, Engine, LogEvent, Machine, Network, NetworkAuxFile, Profile, Test, WorkerKey
from OpenBench.templatetags.mytags import longStatBlock
from OpenBench.utils import merge_required_options
from OpenBench.views import engine_build_variants, normalize_build_command, parse_ssh_target
from OpenBench.workloads.get_workload import game_distribution, valid_hardware_assignment, valid_private_source_assignment, workload_to_dictionary
from OpenBench.workloads.verify_workload import collect_github_info

TEST_SSH_KEY = None

def test_ssh_key():
    # One RSA key shared by the whole test run: generation is not free
    global TEST_SSH_KEY
    if TEST_SSH_KEY is None:
        import io, paramiko
        key = paramiko.RSAKey.generate(2048)
        buffer = io.StringIO()
        key.write_private_key(buffer)
        TEST_SSH_KEY = buffer.getvalue()
    return TEST_SSH_KEY

class WorkerKeyAuthTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'account-password')
        Profile.objects.create(user=self.user, enabled=True)
        self.key = WorkerKey.objects.create(user=self.user, name='vast', token='a' * 48)

    def creds(self, username='alice', password=None):
        return { 'username' : username, 'password' : password or self.key.token }

    def test_worker_key_authenticates_client_endpoint(self):
        response = self.client.post('/clientVersionRef/', self.creds())
        self.assertNotIn('error', response.json())
        self.assertIn('client_version', response.json())

    def test_account_password_still_authenticates(self):
        response = self.client.post('/clientVersionRef/', self.creds(password='account-password'))
        self.assertIn('client_version', response.json())

    def test_bad_token_is_rejected(self):
        response = self.client.post('/clientVersionRef/', self.creds(password='b' * 48))
        self.assertIn('error', response.json())

    def test_wrong_username_is_rejected(self):
        other = User.objects.create_user('bob', 'b@example.com', 'password2')
        Profile.objects.create(user=other, enabled=True)
        response = self.client.post('/clientVersionRef/', self.creds(username='bob'))
        self.assertIn('error', response.json())

    def test_disabled_key_is_rejected(self):
        self.key.enabled = False
        self.key.save()
        response = self.client.post('/clientVersionRef/', self.creds())
        self.assertIn('error', response.json())

    def test_disabled_profile_is_rejected(self):
        profile = Profile.objects.get(user=self.user)
        profile.enabled = False
        profile.save()
        response = self.client.post('/clientVersionRef/', self.creds())
        self.assertIn('error', response.json())

    def test_deleted_key_signals_shutdown(self):

        # 存在しないトークン形状の資格情報 = 削除済みキー。ワーカーが恒久停止
        # してよいことを構造化フラグで伝える (文字列一致に頼らない)
        response = self.client.post('/clientVersionRef/', self.creds(password='b' * 48))
        self.assertTrue(response.json().get('shutdown'))

    def test_disabled_key_signals_shutdown(self):
        self.key.enabled = False
        self.key.save()
        response = self.client.post('/clientVersionRef/', self.creds())
        self.assertTrue(response.json().get('shutdown'))

    def test_disabled_profile_does_not_signal_shutdown(self):

        # アカウントの一時無効化は可逆的なので、キーが有効なままなら
        # ワーカーを恒久停止させない (フラグを立てない)
        profile = Profile.objects.get(user=self.user)
        profile.enabled = False
        profile.save()
        response = self.client.post('/clientVersionRef/', self.creds())
        self.assertIn('error', response.json())
        self.assertNotIn('shutdown', response.json())

    def test_wrong_password_does_not_signal_shutdown(self):

        # パスワード運用 (トークン形状でない) の認証失敗も恒久停止させない
        response = self.client.post('/clientVersionRef/', self.creds(password='wrong-password'))
        self.assertIn('error', response.json())
        self.assertNotIn('shutdown', response.json())

    def test_username_match_is_case_insensitive(self):
        response = self.client.post('/clientVersionRef/', self.creds(username='Alice'))
        self.assertIn('client_version', response.json())

    def test_token_whitespace_is_forgiven(self):
        response = self.client.post('/clientVersionRef/', self.creds(password=self.key.token + '\n'))
        self.assertIn('client_version', response.json())

    def test_key_use_updates_last_used(self):
        self.assertIsNone(self.key.last_used)
        self.client.post('/clientVersionRef/', self.creds())
        self.key.refresh_from_db()
        self.assertIsNotNone(self.key.last_used)

    def test_worker_key_cannot_login_to_website(self):
        response = self.client.post('/login/', self.creds())
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_worker_info_accepts_worker_key(self):
        import json
        from OpenBench.models import Machine
        info = {
            'compilers'   : {}, 'cpu_flags' : [], 'tokens' : {},
            'os_name'     : 'Linux', 'concurrency' : 1,
            'client_ver'  : 0, 'mac_address' : '00:00:00:00:00:00',
            'machine_name': 'test',
        }
        response = self.client.post('/clientWorkerInfo/', {
            'system_info' : json.dumps(info), **self.creds() })
        data = response.json()
        self.assertIn('machine_id', data)
        self.assertIn('secret', data)

        # The session remembers which key opened it, for later revocation
        machine = Machine.objects.get(id=data['machine_id'])
        self.assertEqual(machine.info['worker_key_id'], self.key.id)

    def test_revoked_key_cuts_off_the_session(self):
        from OpenBench.models import Machine
        from OpenBench.utils import machine_key_revoked
        from OpenBench.workloads.get_workload import get_workload, SHUTDOWN_ERROR

        machine = Machine.objects.create(
            user=self.user, info={ 'worker_key_id' : self.key.id })
        self.assertFalse(machine_key_revoked(machine))

        # Disabling the key revokes the session: the worker is told
        # explicitly, so it can shut itself down (wrapper loop included)
        self.key.enabled = False
        self.key.save()
        self.assertTrue(machine_key_revoked(machine))
        self.assertEqual(get_workload(None, machine),
                         { 'error' : SHUTDOWN_ERROR, 'shutdown' : True })

        # Deleting it likewise
        self.key.delete()
        self.assertTrue(machine_key_revoked(machine))
        self.assertEqual(get_workload(None, machine),
                         { 'error' : SHUTDOWN_ERROR, 'shutdown' : True })

        # Password-opened sessions record no key and never revoke this way
        legacy = Machine.objects.create(user=self.user, info={})
        self.assertFalse(machine_key_revoked(legacy))

    def test_stopped_machine_idles_without_shutdown(self):

        # /workers/ の一時停止は復帰前提なので、仕事を配らないだけで
        # ワーカーを終了させない (エラーではなく空を返す)
        from OpenBench.models import Machine
        from OpenBench.workloads.get_workload import get_workload

        machine = Machine.objects.create(
            user=self.user, info={ 'worker_key_id' : self.key.id, 'stop_requested' : True })
        self.assertEqual(get_workload(None, machine), {})

    def test_stopped_machine_still_hears_revocation(self):

        # 停止 → キー無効化 (UI が長期停止で推奨する手順) でも失効通知が届く。
        # stop_requested を先に判定すると {} を返し続けて exit 66 が永遠に
        # 届かず、借りたインスタンスが止まらない
        from OpenBench.models import Machine
        from OpenBench.workloads.get_workload import get_workload, SHUTDOWN_ERROR

        machine = Machine.objects.create(
            user=self.user, info={ 'worker_key_id' : self.key.id, 'stop_requested' : True })

        self.key.enabled = False
        self.key.save()
        self.assertEqual(get_workload(None, machine),
                         { 'error' : SHUTDOWN_ERROR, 'shutdown' : True })

    def register(self, token=None, name='test'):
        import json
        info = {
            'compilers'   : {}, 'cpu_flags' : [], 'tokens' : {},
            'os_name'     : 'Linux', 'concurrency' : 1,
            'client_ver'  : 0, 'mac_address' : 'AA:BB',
            'machine_name': name,
        }
        if token:
            info['machine_token'] = token
        return self.client.post('/clientWorkerInfo/', {
            'system_info' : json.dumps(info), **self.creds() }).json()

    def test_reregistration_reuses_machine_row(self):

        # クラッシュループやクライアント更新で再登録が繰り返されても、
        # 同じ machine_token なら Machine 行は増えない (マシン一覧の無限増殖対策)
        from OpenBench.models import Machine

        first  = self.register(token='a' * 32)
        second = self.register(token='a' * 32)
        third  = self.register(token='a' * 32)

        self.assertEqual(first['machine_id'], second['machine_id'])
        self.assertEqual(first['machine_id'], third['machine_id'])
        self.assertEqual(Machine.objects.filter(user=self.user).count(), 1)

        # secret はセッションごとに更新される
        self.assertNotEqual(first['secret'], second['secret'])

        # 別のインスタンス (別トークン) は別の行になる
        other = self.register(token='b' * 32)
        self.assertNotEqual(first['machine_id'], other['machine_id'])
        self.assertEqual(Machine.objects.filter(user=self.user).count(), 2)

    def test_old_clients_without_token_still_register(self):

        from OpenBench.models import Machine
        first  = self.register()
        second = self.register()
        self.assertNotEqual(first['machine_id'], second['machine_id'])
        self.assertEqual(Machine.objects.filter(user=self.user).count(), 2)

    def test_reregistration_clears_stale_workload(self):

        # 再利用した行に前セッションの workload が残ると、「そのテストを
        # まだ抱えている最近のマシン」に見えて SPSA の再割当や PGN API を
        # ブロックするので、登録時に必ずクリアする
        from OpenBench.models import Machine

        first   = self.register(token='c' * 32)
        machine = Machine.objects.get(id=first['machine_id'])
        machine.workload = 42
        machine.save()

        self.register(token='c' * 32)
        machine.refresh_from_db()
        self.assertEqual(machine.workload, 0)

    def test_token_is_stored_on_the_column(self):

        # machine_token は実カラムに載る (JSON 全行走査や重複行を防ぐ)
        from OpenBench.models import Machine

        data    = self.register(token='d' * 32)
        machine = Machine.objects.get(id=data['machine_id'])
        self.assertEqual(machine.machine_token, 'd' * 32)

    def test_legacy_json_token_rows_are_adopted(self):

        # 旧サーバ時代の行 (カラム空、JSON にだけトークン) も再利用され、
        # 以後はカラムに引き継がれる
        from OpenBench.models import Machine

        legacy = Machine.objects.create(
            user=self.user, info={ 'machine_token' : 'e' * 32 })
        data = self.register(token='e' * 32)

        self.assertEqual(data['machine_id'], legacy.id)
        legacy.refresh_from_db()
        self.assertEqual(legacy.machine_token, 'e' * 32)

    def test_duplicate_token_rows_are_rejected_by_the_database(self):

        # 同時登録の競合は DB の部分 unique 制約が最後の砦になる
        from django.db import IntegrityError, transaction
        from OpenBench.models import Machine

        Machine.objects.create(user=self.user, info={}, machine_token='f' * 32)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Machine.objects.create(user=self.user, info={}, machine_token='f' * 32)

        # カラムが空の行 (旧クライアント) は何行あってもよい
        Machine.objects.create(user=self.user, info={})
        Machine.objects.create(user=self.user, info={})

class GithubLookupTests(TestCase):

    class FakeRequest:
        POST = {
            'dev_branch' : 'suisho11-tuned',
            'dev_bench'  : '0',
            'dev_engine' : 'YaneuraOu-nagisa',
            'dev_repo'   : 'https://github.com/keinoda/YaneuraOu',
        }

    class FakeResponse:
        def __init__(self, status_code, data, headers=None):
            self.status_code = status_code
            self._data = data
            self.headers = headers or {}

        def json(self):
            return self._data

    @patch.dict(os.environ, { 'OPENBENCH_GITHUB_TOKEN' : 'test-token' })
    @patch('OpenBench.workloads.verify_workload.requests.get')
    def test_public_engine_uses_configured_github_token(self, mock_get):
        mock_get.side_effect = [
            self.FakeResponse(200, {
                'private' : False,
            }),
            self.FakeResponse(200, {
                'commit' : {
                    'sha'    : 'a' * 40,
                    'commit' : {
                        'message' : 'bench not required',
                        'tree'    : { 'sha' : 'b' * 40 },
                    },
                },
            }),
        ]

        errors = []
        info, has_all = collect_github_info(errors, self.FakeRequest(), 'dev')

        self.assertEqual(errors, [])
        self.assertTrue(has_all)
        self.assertEqual(info[1], 'suisho11-tuned')
        self.assertEqual(mock_get.call_args.kwargs['headers'], {
            'Authorization' : 'Bearer test-token',
        })

    @patch.dict(os.environ, { 'OPENBENCH_GITHUB_TOKEN' : 'test-token' })
    @patch('OpenBench.workloads.verify_workload.requests.get')
    def test_allowed_private_source_uses_server_proxy_marker(self, mock_get):
        request = SimpleNamespace(POST=dict(
            self.FakeRequest.POST,
            dev_repo='https://github.com/keinoda/YaneuraOu-private',
            dev_branch='master',
        ))
        mock_get.side_effect = [
            self.FakeResponse(200, {
                'private' : True,
            }),
            self.FakeResponse(200, {
                'commit' : {
                    'sha'    : 'a' * 40,
                    'commit' : {
                        'message' : 'bench not required',
                        'tree'    : { 'sha' : 'b' * 40 },
                    },
                },
            }),
        ]

        errors = []
        info, has_all = collect_github_info(errors, request, 'dev')

        self.assertEqual(errors, [])
        self.assertTrue(has_all)
        self.assertEqual(
            info[0],
            'openbench://github/keinoda/YaneuraOu-private/%s.zip' % ('a' * 40))
        self.assertNotIn('test-token', info[0])

    @patch.dict(os.environ, {}, clear=True)
    @patch('OpenBench.workloads.verify_workload.requests.get')
    def test_allowed_private_source_requires_server_token(self, mock_get):
        request = SimpleNamespace(POST=dict(
            self.FakeRequest.POST,
            dev_repo='https://github.com/keinoda/YaneuraOu-private',
            dev_branch='master',
        ))

        errors = []
        info = collect_github_info(errors, request, 'dev')

        self.assertEqual(info, (None, None))
        self.assertIn('access tokens', errors[0])
        mock_get.assert_not_called()

    @patch.dict(os.environ, { 'OPENBENCH_GITHUB_TOKEN' : 'test-token' })
    @patch('OpenBench.workloads.verify_workload.requests.get')
    def test_other_private_source_is_rejected(self, mock_get):
        request = SimpleNamespace(POST=dict(
            self.FakeRequest.POST,
            dev_repo='https://github.com/keinoda/another-private',
            dev_branch='master',
        ))
        mock_get.return_value = self.FakeResponse(200, {
            'private' : True,
        })

        errors = []
        info = collect_github_info(errors, request, 'dev')

        self.assertEqual(info, (None, None))
        self.assertIn('not allowed', errors[0])

    @patch('OpenBench.workloads.verify_workload.requests.get')
    def test_github_rate_limit_is_reported_directly(self, mock_get):
        mock_get.return_value = self.FakeResponse(
            403,
            { 'message' : 'API rate limit exceeded for 79.127.159.112.' },
            { 'x-ratelimit-remaining' : '0', 'x-ratelimit-reset' : '1783495254' },
        )

        errors = []
        info = collect_github_info(errors, self.FakeRequest(), 'dev')

        self.assertEqual(info, (None, None))
        self.assertEqual(len(errors), 1)
        self.assertIn('GitHub API rate limit exceeded', errors[0])
        self.assertIn('OPENBENCH_GITHUB_TOKEN', errors[0])

class GithubBranchListTests(TestCase):

    class FakeResponse:
        def __init__(self, status_code, data, headers=None):
            self.status_code = status_code
            self._data = data
            self.headers = headers or {}

        def json(self):
            return self._data

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'account-password')
        Profile.objects.create(user=self.user, enabled=True)
        self.client.login(username='alice', password='account-password')

    def test_endpoint_requires_login(self):
        response = Client().get('/api/branches/', {
            'engine' : 'YaneuraOu-nagisa',
            'repo'   : 'https://github.com/keinoda/YaneuraOu',
        })

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error'], 'ログインが必要です')

    def test_invalid_repository_is_rejected_before_github_request(self):
        with patch('OpenBench.workloads.verify_workload.requests.get') as mock_get:
            response = self.client.get('/api/branches/', {
                'engine' : 'YaneuraOu-nagisa',
                'repo'   : 'https://example.com/keinoda/YaneuraOu',
            })

        self.assertEqual(response.status_code, 400)
        self.assertIn('GitHubリポジトリURL', response.json()['error'])
        mock_get.assert_not_called()

    @patch.dict(os.environ, { 'OPENBENCH_GITHUB_TOKEN' : 'test-token' })
    @patch('OpenBench.workloads.verify_workload.requests.get')
    def test_endpoint_uses_token_and_collects_all_pages(self, mock_get):
        first_page = [ { 'name' : 'branch-%03d' % index } for index in range(100) ]
        mock_get.side_effect = [
            self.FakeResponse(200, { 'default_branch' : 'master' }),
            self.FakeResponse(200, first_page),
            self.FakeResponse(200, [ { 'name' : 'master' } ]),
        ]

        response = self.client.get('/api/branches/', {
            'engine' : 'YaneuraOu-nagisa',
            'repo'   : 'https://github.com/keinoda/YaneuraOu',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['default_branch'], 'master')
        self.assertEqual(len(response.json()['branches']), 101)
        self.assertEqual(response.json()['branches'], sorted(
            response.json()['branches'], key=str.casefold))
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(mock_get.call_args_list[0].kwargs['headers'], {
            'Authorization' : 'Bearer test-token',
            'Accept'        : 'application/vnd.github+json',
        })
        self.assertEqual(mock_get.call_args_list[1].kwargs['params'], {
            'per_page' : 100, 'page' : 1,
        })
        self.assertEqual(mock_get.call_args_list[2].kwargs['params'], {
            'per_page' : 100, 'page' : 2,
        })

    @patch.dict(os.environ, { 'OPENBENCH_GITHUB_TOKEN' : 'test-token' })
    @patch('OpenBench.workloads.verify_workload.requests.get')
    def test_endpoint_lists_only_the_allowed_private_repository(self, mock_get):
        mock_get.side_effect = [
            self.FakeResponse(200, {
                'default_branch' : 'master',
                'private'        : True,
            }),
            self.FakeResponse(200, [
                { 'name' : 'master' },
                { 'name' : 'local-backup/nagisa_v3' },
            ]),
        ]

        response = self.client.get('/api/branches/', {
            'engine' : 'YaneuraOu-nagisa',
            'repo'   : 'https://github.com/keinoda/YaneuraOu-private',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()['branches'],
            ['local-backup/nagisa_v3', 'master'])

    @patch.dict(os.environ, { 'OPENBENCH_GITHUB_TOKEN' : 'test-token' })
    @patch('OpenBench.workloads.verify_workload.requests.get')
    def test_dedicated_private_engine_lists_its_repository(self, mock_get):
        mock_get.side_effect = [
            self.FakeResponse(200, {
                'default_branch' : 'master',
                'private'        : True,
            }),
            self.FakeResponse(200, [
                { 'name' : 'master' },
                { 'name' : 'nagisa_v3' },
            ]),
        ]

        response = self.client.get('/api/branches/', {
            'engine' : 'YaneuraOu-private',
            'repo'   : 'https://github.com/keinoda/YaneuraOu-private',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['default_branch'], 'master')
        self.assertEqual(response.json()['branches'], ['master', 'nagisa_v3'])

    @patch.dict(os.environ, { 'OPENBENCH_GITHUB_TOKEN' : 'test-token' })
    @patch('OpenBench.workloads.verify_workload.requests.get')
    def test_endpoint_rejects_other_private_repositories(self, mock_get):
        mock_get.return_value = self.FakeResponse(200, {
            'default_branch' : 'main',
            'private'        : True,
        })

        response = self.client.get('/api/branches/', {
            'engine' : 'YaneuraOu-nagisa',
            'repo'   : 'https://github.com/keinoda/another-private',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('許可されていません', response.json()['error'])

    @patch('OpenBench.workloads.verify_workload.requests.get')
    def test_github_error_is_returned_to_the_form(self, mock_get):
        mock_get.return_value = self.FakeResponse(
            403,
            { 'message' : 'API rate limit exceeded' },
            { 'x-ratelimit-remaining' : '0' },
        )

        response = self.client.get('/api/branches/', {
            'engine' : 'YaneuraOu-nagisa',
            'repo'   : 'https://github.com/keinoda/YaneuraOu',
        })

        self.assertEqual(response.status_code, 502)
        self.assertIn('GitHub API rate limit exceeded', response.json()['error'])
        self.assertIn('OPENBENCH_GITHUB_TOKEN', response.json()['error'])

    def test_workload_form_uses_branch_selectors(self):
        response = self.client.get('/test/new/')

        self.assertContains(response, 'id="dev_branch"')
        self.assertContains(response, 'id="base_branch"')
        self.assertContains(response, 'GitHubから取得中...')
        self.assertNotContains(response, '<input id="dev_branch"')
        self.assertNotContains(response, '<input id="base_branch"')

    def test_workload_form_lists_dedicated_private_engine(self):
        response = self.client.get('/test/new/')

        self.assertContains(
            response,
            '<option value="YaneuraOu-private">YaneuraOu-private</option>',
            count=2,
            html=True,
        )
        engine = response.context['config']['engines']['YaneuraOu-private']
        self.assertEqual(
            engine['source'],
            'https://github.com/keinoda/YaneuraOu-private',
        )
        self.assertEqual(engine['private_sources'], [
            'https://github.com/keinoda/YaneuraOu-private',
        ])


class PrivateGithubArchiveTests(TestCase):

    class FakeArchiveResponse:
        status_code = 200

        def __init__(self, content=b'private-source-zip'):
            self.content = content
            self.closed = False

        def iter_content(self, chunk_size):
            yield self.content

        def close(self):
            self.closed = True

    def setUp(self):
        import OpenBench.config
        import OpenBench.utils

        self.user = User.objects.create_user('alice', 'a@example.com', 'pw')
        Profile.objects.create(user=self.user, enabled=True)
        self.sha = 'a' * 40
        self.repo = 'https://github.com/keinoda/YaneuraOu-private'
        self.engine = Engine.objects.create(
            name='master',
            source=OpenBench.utils.private_source_archive(self.repo, self.sha),
            sha=self.sha,
            bench=0,
        )
        self.test = Test.objects.create(
            author='alice',
            dev=self.engine,
            base=self.engine,
            dev_repo=self.repo,
            base_repo=self.repo,
            dev_engine='YaneuraOu-nagisa',
            base_engine='YaneuraOu-nagisa',
        )
        self.machine = Machine.objects.create(
            user=self.user,
            workload=self.test.id,
            secret='machine-secret',
            info={
                'client_ver' : OpenBench.config.OPENBENCH_CONFIG['client_version'],
                'OPENBENCH_CONFIG_CHECKSUM' : OpenBench.config.OPENBENCH_CONFIG_CHECKSUM,
            },
        )

    def request_data(self, machine=None):
        machine = machine or self.machine
        return {
            'machine_id' : machine.id,
            'secret'     : machine.secret,
            'test_id'    : self.test.id,
            'side'       : 'dev',
        }

    @patch.dict(os.environ, { 'OPENBENCH_GITHUB_TOKEN' : 'test-token' })
    @patch('OpenBench.views.requests.get')
    def test_assigned_owner_worker_receives_streamed_archive(self, mock_get):
        upstream = self.FakeArchiveResponse()
        mock_get.return_value = upstream

        response = self.client.post(
            '/clientGetGitHubArchive/', self.request_data())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'private-source-zip')
        self.assertTrue(upstream.closed)
        self.assertEqual(
            mock_get.call_args.args[0],
            'https://api.github.com/repos/keinoda/YaneuraOu-private/zipball/%s' % self.sha)
        self.assertEqual(mock_get.call_args.kwargs['headers'], {
            'Authorization' : 'Bearer test-token',
        })

    @patch('OpenBench.views.requests.get')
    def test_other_users_worker_cannot_receive_private_source(self, mock_get):
        import OpenBench.config

        other = User.objects.create_user('bob', 'b@example.com', 'pw')
        Profile.objects.create(user=other, enabled=True)
        machine = Machine.objects.create(
            user=other,
            workload=self.test.id,
            secret='other-secret',
            info={
                'client_ver' : OpenBench.config.OPENBENCH_CONFIG['client_version'],
                'OPENBENCH_CONFIG_CHECKSUM' : OpenBench.config.OPENBENCH_CONFIG_CHECKSUM,
            },
        )

        response = self.client.post(
            '/clientGetGitHubArchive/', self.request_data(machine))

        self.assertEqual(response.status_code, 403)
        mock_get.assert_not_called()

    def test_scheduler_keeps_private_source_on_authors_workers(self):
        own_machine = SimpleNamespace(user=self.user)
        other_user = User.objects.create_user('bob', 'b@example.com', 'pw')
        other_machine = SimpleNamespace(user=other_user)

        self.assertTrue(valid_private_source_assignment(self.test, own_machine))
        self.assertFalse(valid_private_source_assignment(self.test, other_machine))

        self.test.dev_repo = self.test.base_repo = 'https://github.com/keinoda/YaneuraOu'
        self.assertTrue(valid_private_source_assignment(self.test, other_machine))


class InviteOnlyRegistrationTests(TestCase):

    def test_register_get_redirects_to_login(self):
        response = self.client.get('/register/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_register_post_creates_no_user(self):
        self.client.post('/register/', {
            'username' : 'mallory', 'email' : 'm@example.com',
            'password1' : 'hunter22', 'password2' : 'hunter22' })
        self.assertFalse(User.objects.filter(username='mallory').exists())

class InviteCommandTests(TestCase):

    def test_invite_accepts_hyphenated_django_username(self):
        output = io.StringIO()
        call_command(
            'invite',
            'Agent-AI',
            password='test-invite-password',
            stdout=output,
        )

        user = User.objects.get(username='Agent-AI')
        profile = Profile.objects.get(user=user)
        self.assertTrue(user.check_password('test-invite-password'))
        self.assertTrue(profile.enabled)
        self.assertFalse(profile.approver)
        self.assertIn('Created user "Agent-AI"', output.getvalue())

    def test_invite_rejects_username_outside_django_rules(self):
        with self.assertRaises(CommandError):
            call_command(
                'invite',
                'Agent/AI',
                password='test-invite-password',
            )

        self.assertFalse(User.objects.filter(username='Agent/AI').exists())

class ProfileConfigTests(TestCase):

    def test_approver_can_add_engine_repository(self):
        user = User.objects.create_user('approver', 'a@example.com', 'account-password')
        Profile.objects.create(user=user, enabled=True, approver=True)
        self.client.login(username='approver', password='account-password')

        self.client.post('/profileConfig/', {
            'new-engine-name' : 'YaneuraOu',
            'new-engine-repo' : 'https://github.com/example/YaneuraOu',
            'deleted-repos'   : '[]',
        })

        profile = Profile.objects.get(user=user)
        self.assertEqual(profile.engine, 'YaneuraOu')
        self.assertEqual(profile.repos, {
            'YaneuraOu' : 'https://github.com/example/YaneuraOu',
        })

    def test_disabled_user_cannot_add_engine_repository(self):
        user = User.objects.create_user('disabled', 'd@example.com', 'account-password')
        Profile.objects.create(user=user, enabled=False, approver=True)
        self.client.login(username='disabled', password='account-password')

        self.client.post('/profileConfig/', {
            'new-engine-name' : 'YaneuraOu',
            'new-engine-repo' : 'https://github.com/example/YaneuraOu',
            'deleted-repos'   : '[]',
        })

        profile = Profile.objects.get(user=user)
        self.assertEqual(profile.engine, '')
        self.assertEqual(profile.repos, {})

class WorkerKeyPageTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'account-password')
        Profile.objects.create(user=self.user, enabled=True)
        self.client.login(username='alice', password='account-password')

    def test_page_requires_login(self):
        response = Client().get('/workers/')
        self.assertEqual(response.status_code, 302)

    def test_create_and_delete_key(self):
        self.client.post('/workers/', { 'action' : 'create', 'name' : 'box1' })
        key = WorkerKey.objects.get(user=self.user, name='box1')
        self.assertEqual(len(key.token), 48)

        self.client.post('/workers/', { 'action' : 'delete', 'key_id' : key.id })
        self.assertFalse(WorkerKey.objects.filter(id=key.id).exists())

    def test_disable_and_enable_key(self):
        self.client.post('/workers/', { 'action' : 'create', 'name' : 'box1' })
        key = WorkerKey.objects.get(user=self.user, name='box1')

        self.client.post('/workers/', { 'action' : 'disable', 'key_id' : key.id })
        key.refresh_from_db()
        self.assertFalse(key.enabled)

        self.client.post('/workers/', { 'action' : 'enable', 'key_id' : key.id })
        key.refresh_from_db()
        self.assertTrue(key.enabled)

    def test_cannot_touch_other_users_key(self):
        other = User.objects.create_user('bob', 'b@example.com', 'password2')
        Profile.objects.create(user=other, enabled=True)
        key = WorkerKey.objects.create(user=other, name='bobkey', token='c' * 48)

        self.client.post('/workers/', { 'action' : 'delete', 'key_id' : key.id })
        self.assertTrue(WorkerKey.objects.filter(id=key.id).exists())

    def test_page_renders_with_keys(self):
        self.client.post('/workers/', { 'action' : 'create', 'name' : 'box1' })
        response = self.client.get('/workers/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'box1')

    def test_owner_can_stop_and_resume_machine(self):
        from OpenBench.models import Machine
        machine = Machine.objects.create(user=self.user, info={})

        self.client.post('/workers/', { 'action' : 'stop_machine', 'machine_id' : machine.id })
        machine.refresh_from_db()
        self.assertTrue(machine.info['stop_requested'])

        self.client.post('/workers/', { 'action' : 'resume_machine', 'machine_id' : machine.id })
        machine.refresh_from_db()
        self.assertFalse(machine.info['stop_requested'])

    def test_non_owner_cannot_stop_machine(self):
        from OpenBench.models import Machine
        machine = Machine.objects.create(user=self.user, info={})

        other = User.objects.create_user('mallory', 'm@example.com', 'password3')
        Profile.objects.create(user=other, enabled=True, approver=False)
        client = Client()
        client.login(username='mallory', password='password3')

        client.post('/workers/', { 'action' : 'stop_machine', 'machine_id' : machine.id })
        machine.refresh_from_db()
        self.assertNotIn('stop_requested', machine.info)

class NetworkUploadTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'account-password')
        Profile.objects.create(user=self.user, enabled=True, approver=True)
        self.client.login(username='alice', password='account-password')
        self.media = tempfile.mkdtemp()

    def test_upload_hashes_in_chunks_and_saves(self):
        content  = b'\x00\x01\x02\x03' * 100_000  # ~400KB, forces multiple chunks
        expected = hashlib.sha256(content).hexdigest()[:8].upper()

        with override_settings(MEDIA_ROOT=self.media):
            response = self.client.post('/networks/YaneuraOu-nagisa/UPLOAD/mynet.bin/', {
                'netfile' : SimpleUploadedFile('mynet.bin', content) })

        network = Network.objects.filter(engine='YaneuraOu-nagisa', name='mynet.bin').first()
        self.assertIsNotNone(network)
        self.assertEqual(network.sha256, expected)

    def test_upload_with_aux_files(self):
        content = b'\x10\x20' * 50_000
        aux     = b'\x30\x40' * 25_000
        opts    = b'FV_SCALE=24\nBucketSelect=k3k3\n'
        aux_sha = hashlib.sha256(aux).hexdigest()[:8].upper()

        with override_settings(MEDIA_ROOT=self.media), \
             patch('OpenBench.utils.MEDIA_ROOT', self.media):
            self.client.post('/networks/YaneuraOu-nagisa/UPLOAD/withaux.bin/', {
                'netfile'  : SimpleUploadedFile('nn.bin', content),
                'auxfiles' : [SimpleUploadedFile('progress.bin', aux),
                              SimpleUploadedFile('eval_options.txt', opts)] })

            network = Network.objects.filter(engine='YaneuraOu-nagisa', name='withaux.bin').first()
            self.assertIsNotNone(network)
            self.assertEqual(network.aux_files.count(), 2)
            self.assertEqual(network.aux_files.get(name='progress.bin').sha256, aux_sha)

            # Each aux file is retrievable through the api endpoint by name
            response = self.client.post('/api/networks/YaneuraOu-nagisa/%s/aux/progress.bin/' % (network.sha256))
            self.assertEqual(b''.join(response.streaming_content), aux)

            response = self.client.post('/api/networks/YaneuraOu-nagisa/%s/aux/eval_options.txt/' % (network.sha256))
            self.assertEqual(b''.join(response.streaming_content), opts)

    def test_upload_rejects_eval_options_managed_path_option(self):
        with override_settings(MEDIA_ROOT=self.media), \
             patch('OpenBench.utils.MEDIA_ROOT', self.media):
            response = self.client.post('/networks/YaneuraOu-nagisa/UPLOAD/badopts.bin/', {
                'netfile'  : SimpleUploadedFile('nn.bin', b'net-content'),
                'auxfiles' : [SimpleUploadedFile(
                    'eval_options.txt', b'LS_PROGRESS_COEFF ./progress.bin\n')] }, follow=True)

            self.assertFalse(Network.objects.filter(engine='YaneuraOu-nagisa', name='badopts.bin').exists())
            self.assertContains(response, 'eval_options.txt may not set LS_PROGRESS_COEFF')

    def test_upload_allows_non_path_eval_options(self):
        opts = b'LS_BUCKET_MODE progress8kpabs\nFV_SCALE=28\n'

        with override_settings(MEDIA_ROOT=self.media), \
             patch('OpenBench.utils.MEDIA_ROOT', self.media):
            self.client.post('/networks/YaneuraOu-nagisa/UPLOAD/goodopts.bin/', {
                'netfile'  : SimpleUploadedFile('nn.bin', b'net-content'),
                'auxfiles' : [SimpleUploadedFile('eval_options.txt', opts)] })

            network = Network.objects.get(engine='YaneuraOu-nagisa', name='goodopts.bin')
            self.assertEqual(network.aux_files.get(name='eval_options.txt').sha256,
                             hashlib.sha256(opts).hexdigest()[:8].upper())

    def test_aux_add_and_delete_on_edit_page(self):
        with override_settings(MEDIA_ROOT=self.media), \
             patch('OpenBench.utils.MEDIA_ROOT', self.media):
            self.client.post('/networks/YaneuraOu-nagisa/UPLOAD/editme.bin/', {
                'netfile' : SimpleUploadedFile('nn.bin', b'net-content') })
            network = Network.objects.get(engine='YaneuraOu-nagisa', name='editme.bin')

            # Add two aux files after the fact
            self.client.post('/networks/YaneuraOu-nagisa/EDIT/%s/' % (network.sha256), {
                'action'   : 'aux_add',
                'auxfiles' : [SimpleUploadedFile('progress.bin', b'prog'),
                              SimpleUploadedFile('eval_options.txt', b'FV_SCALE=28\n')] })
            self.assertEqual(network.aux_files.count(), 2)

            # Duplicate names are rejected
            response = self.client.post('/networks/YaneuraOu-nagisa/EDIT/%s/' % (network.sha256), {
                'action'   : 'aux_add',
                'auxfiles' : [SimpleUploadedFile('progress.bin', b'other')] }, follow=True)
            self.assertEqual(network.aux_files.count(), 2)

            # Delete one and confirm the row disappears
            aux = network.aux_files.get(name='progress.bin')
            self.client.post('/networks/YaneuraOu-nagisa/EDIT/%s/' % (network.sha256), {
                'action' : 'aux_delete', 'aux_id' : aux.id })
            self.assertEqual(network.aux_files.count(), 1)
            self.assertFalse(network.aux_files.filter(name='progress.bin').exists())

            # The edit page renders the remaining aux file
            response = self.client.get('/networks/YaneuraOu-nagisa/EDIT/%s/' % (network.sha256))
            self.assertContains(response, 'eval_options.txt')

    def test_aux_add_rejects_eval_options_managed_path_option(self):
        with override_settings(MEDIA_ROOT=self.media), \
             patch('OpenBench.utils.MEDIA_ROOT', self.media):
            self.client.post('/networks/YaneuraOu-nagisa/UPLOAD/rejectedit.bin/', {
                'netfile' : SimpleUploadedFile('nn.bin', b'net-content') })
            network = Network.objects.get(engine='YaneuraOu-nagisa', name='rejectedit.bin')

            response = self.client.post('/networks/YaneuraOu-nagisa/EDIT/%s/' % (network.sha256), {
                'action'   : 'aux_add',
                'auxfiles' : [SimpleUploadedFile(
                    'eval_options.txt', b'EvalDir=./eval\n')] }, follow=True)

            self.assertFalse(NetworkAuxFile.objects.filter(network=network, name='eval_options.txt').exists())
            self.assertContains(response, 'eval_options.txt may not set EvalDir')

            response = self.client.post('/networks/YaneuraOu-nagisa/EDIT/%s/' % (network.sha256), {
                'action'   : 'aux_add',
                'auxfiles' : [SimpleUploadedFile(
                    'eval_options.txt', b'ProgressFilePath ./progress.bin\n')] }, follow=True)

            self.assertFalse(NetworkAuxFile.objects.filter(network=network, name='eval_options.txt').exists())
            self.assertContains(response, 'eval_options.txt may not set ProgressFilePath')

    def test_upload_with_legacy_single_aux_field(self):
        with override_settings(MEDIA_ROOT=self.media), \
             patch('OpenBench.utils.MEDIA_ROOT', self.media):
            self.client.post('/networks/YaneuraOu-nagisa/UPLOAD/oldstyle.bin/', {
                'netfile' : SimpleUploadedFile('nn.bin', b'net'),
                'auxfile' : SimpleUploadedFile('progress.bin', b'prog') })
            network = Network.objects.get(engine='YaneuraOu-nagisa', name='oldstyle.bin')
            self.assertEqual(network.aux_files.get(name='progress.bin').sha256,
                             hashlib.sha256(b'prog').hexdigest()[:8].upper())

    def test_worker_key_can_download_network(self):
        content = b'\x0a\x0b' * 10_000
        key = WorkerKey.objects.create(user=self.user, name='dl', token='d' * 48)

        with override_settings(MEDIA_ROOT=self.media), \
             patch('OpenBench.utils.MEDIA_ROOT', self.media):
            self.client.post('/networks/YaneuraOu-nagisa/UPLOAD/dlnet.bin/', {
                'netfile' : SimpleUploadedFile('nn.bin', content) })
            network = Network.objects.get(engine='YaneuraOu-nagisa', name='dlnet.bin')

            # A fresh, unauthenticated client using the worker key as password
            worker = Client()
            response = worker.post('/api/networks/YaneuraOu-nagisa/%s/' % (network.sha256), {
                'username' : 'alice', 'password' : key.token })
            body = b''.join(response.streaming_content)
            self.assertEqual(body, content)

    def test_worker_key_cannot_delete_network(self):
        key = WorkerKey.objects.create(user=self.user, name='dl2', token='e' * 48)

        with override_settings(MEDIA_ROOT=self.media), \
             patch('OpenBench.utils.MEDIA_ROOT', self.media):
            self.client.post('/networks/YaneuraOu-nagisa/UPLOAD/keepme.bin/', {
                'netfile' : SimpleUploadedFile('nn.bin', b'keep') })
            network = Network.objects.get(engine='YaneuraOu-nagisa', name='keepme.bin')

            worker = Client()
            worker.post('/api/networks/YaneuraOu-nagisa/%s/delete/' % (network.sha256), {
                'username' : 'alice', 'password' : key.token })
            self.assertTrue(Network.objects.filter(id=network.id).exists())

    def test_aux_endpoint_without_aux_errors(self):
        with override_settings(MEDIA_ROOT=self.media):
            self.client.post('/networks/YaneuraOu-nagisa/UPLOAD/noaux.bin/', {
                'netfile' : SimpleUploadedFile('nn.bin', b'plain') })
            network = Network.objects.get(engine='YaneuraOu-nagisa', name='noaux.bin')
            response = self.client.post('/api/networks/YaneuraOu-nagisa/%s/aux/progress.bin/' % (network.sha256))
            self.assertIn('error', response.json())

    def test_upload_requires_approver(self):
        other = User.objects.create_user('bob', 'b@example.com', 'password2')
        Profile.objects.create(user=other, enabled=True, approver=False)
        client = Client()
        client.login(username='bob', password='password2')

        with override_settings(MEDIA_ROOT=self.media):
            client.post('/networks/YaneuraOu-nagisa/UPLOAD/theirs.bin/', {
                'netfile' : SimpleUploadedFile('theirs.bin', b'data') })

        self.assertFalse(Network.objects.filter(name='theirs.bin').exists())

class SharedBuildVariantTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'account-password')
        Profile.objects.create(user=self.user, enabled=True)
        self.client.login(username='alice', password='account-password')

    def test_shared_variant_appears_for_every_engine(self):
        self.client.post('/builds/', {
            'action'  : 'create',
            'engine'  : '*',
            'name'    : 'shared-avx512',
            'command' : 'make -j tournament COMPILER=clang++ TARGET_CPU=AVX512VNNI' })

        variant = BuildVariant.objects.get(engine='*', name='shared-avx512')
        self.assertIn('TARGET_CPU=AVX512VNNI', variant.args)

        self.assertIn('shared-avx512', engine_build_variants('YaneuraOu-nagisa'))
        self.assertIn('shared-avx512', engine_build_variants('YaneuraOu'))
        self.assertIn('shared-avx512', engine_build_variants('YaneuraOu-souyuukou'))

    def test_engine_specific_wins_over_shared(self):
        BuildVariant.objects.create(engine='*', name='dup', args='shared', author='alice')
        BuildVariant.objects.create(engine='YaneuraOu-nagisa', name='dup', args='specific', author='alice')
        self.assertEqual(engine_build_variants('YaneuraOu-nagisa')['dup'], 'specific')
        self.assertEqual(engine_build_variants('YaneuraOu')['dup'], 'shared')

class DisplayNameTests(TestCase):

    def make_test(self, dev_display='', base_display=''):
        from OpenBench.models import Engine, Test
        engine = Engine.objects.create(name='master', source='s', sha='a' * 40, bench=1)
        return Test.objects.create(
            author='alice', dev=engine, base=engine,
            dev_engine='YaneuraOu-nagisa', base_engine='YaneuraOu-nagisa',
            dev_display=dev_display, base_display=base_display)

    def test_display_names_take_priority(self):
        from OpenBench.templatetags.mytags import git_diff_text, prettyDevName
        test = self.make_test(dev_display='新探索', base_display='旧探索')
        self.assertEqual(git_diff_text(test), '新探索 vs 旧探索')
        self.assertEqual(prettyDevName(test), '新探索')

    def test_stat_block_carries_display_names(self):
        from OpenBench.templatetags.mytags import longStatBlock
        test = self.make_test(dev_display='新探索', base_display='旧探索')
        test.dev_options = test.base_options = 'Threads=1 Hash=64'
        test.dev_time_control = test.base_time_control = '8.0+0.08'
        block = longStatBlock(test)
        self.assertIn('新探索-dev vs 旧探索-base', block)
        self.assertIn('Score for: 新探索-dev', block)

    def test_stat_block_does_not_duplicate_role_suffixes(self):
        from OpenBench.templatetags.mytags import longStatBlock
        test = self.make_test(dev_display='新探索-dev', base_display='旧探索-base')
        test.dev_options = test.base_options = 'Threads=1 Hash=64'
        test.dev_time_control = test.base_time_control = '8.0+0.08'
        block = longStatBlock(test)

        self.assertIn('新探索-dev vs 旧探索-base', block)
        self.assertNotIn('-dev-dev', block)
        self.assertNotIn('-base-base', block)

    def test_fallback_without_display_names(self):
        from OpenBench.templatetags.mytags import git_diff_text, prettyDevName
        test = self.make_test()
        self.assertEqual(git_diff_text(test), 'master vs master')
        self.assertEqual(prettyDevName(test), 'master')

class WorkloadPermissionTests(TestCase):

    def setUp(self):
        from OpenBench.models import Engine, Test
        self.alice = User.objects.create_user('alice', 'a@example.com', 'pw-alice')
        Profile.objects.create(user=self.alice, enabled=True, approver=False)
        self.boss = User.objects.create_user('boss', 'b@example.com', 'pw-boss')
        Profile.objects.create(user=self.boss, enabled=True, approver=True)

        engine    = Engine.objects.create(name='e', source='s', sha='a' * 40, bench=1)
        self.test = Test.objects.create(author='alice', dev=engine, base=engine)
        self.client.login(username='alice', password='pw-alice')

    def refresh(self):
        self.test.refresh_from_db()
        return self.test

    def test_author_cannot_approve_own_test(self):
        self.client.post('/test/%d/APPROVE/' % (self.test.id))
        self.assertFalse(self.refresh().approved)

    def test_author_can_stop_and_delete_own_test(self):
        self.client.post('/test/%d/STOP/' % (self.test.id))
        self.assertTrue(self.refresh().finished)
        self.client.post('/test/%d/DELETE/' % (self.test.id))
        self.assertTrue(self.refresh().deleted)

    def test_other_user_cannot_stop_or_delete(self):
        other = User.objects.create_user('mallory', 'm@example.com', 'pw-m')
        Profile.objects.create(user=other, enabled=True, approver=False)
        client = Client()
        client.login(username='mallory', password='pw-m')

        client.post('/test/%d/STOP/' % (self.test.id))
        self.assertFalse(self.refresh().finished)
        client.post('/test/%d/DELETE/' % (self.test.id))
        self.assertFalse(self.refresh().deleted)
        client.post('/test/%d/APPROVE/' % (self.test.id))
        self.assertFalse(self.refresh().approved)

    def test_approver_can_approve_and_stop_any_test(self):
        client = Client()
        client.login(username='boss', password='pw-boss')
        client.post('/test/%d/APPROVE/' % (self.test.id))
        self.assertTrue(self.refresh().approved)
        client.post('/test/%d/STOP/' % (self.test.id))
        self.assertTrue(self.refresh().finished)

    def test_author_can_edit_display_names_without_changing_test_conditions(self):
        self.test.dev_display      = '変更前 Dev'
        self.test.base_display     = '変更前 Base'
        self.test.dev_options      = 'Threads=1 Hash=64'
        self.test.base_options     = 'Threads=1 Hash=64'
        self.test.book_name        = 'original.epd'
        self.test.upload_pgns      = 'COMPACT'
        self.test.dev_time_control = '8.0+0.08'
        self.test.save()

        self.client.post('/test/%d/MODIFY/' % self.test.id, {
            'dev_display'     : '  新しい Dev 表示名  ',
            'base_display'    : '新しい Base 表示名',
            'priority'        : '7',
            'throughput'      : '128',
            'workload_size'   : '16',
            # 実行条件を送っても、MODIFYでは受け付けない。
            'dev_options'     : 'Threads=99 Hash=1',
            'book_name'       : 'different.epd',
            'upload_pgns'     : 'FALSE',
            'dev_time_control': '1+0.01',
        })

        test = self.refresh()
        self.assertEqual(test.dev_display, '新しい Dev 表示名')
        self.assertEqual(test.base_display, '新しい Base 表示名')
        self.assertEqual(test.priority, 7)
        self.assertEqual(test.throughput, 128)
        self.assertEqual(test.workload_size, 16)
        self.assertEqual(test.dev_options, 'Threads=1 Hash=64')
        self.assertEqual(test.book_name, 'original.epd')
        self.assertEqual(test.upload_pgns, 'COMPACT')
        self.assertEqual(test.dev_time_control, '8.0+0.08')

    def test_other_user_cannot_edit_display_names(self):
        other = User.objects.create_user('mallory', 'm@example.com', 'pw-m')
        Profile.objects.create(user=other, enabled=True, approver=False)
        client = Client()
        client.login(username='mallory', password='pw-m')

        client.post('/test/%d/MODIFY/' % self.test.id, {
            'dev_display'  : '変更してはいけない',
            'base_display' : '変更してはいけない',
        })

        self.assertEqual(self.refresh().dev_display, '')
        self.assertEqual(self.test.base_display, '')

    def test_workload_page_shows_display_name_edit_fields(self):
        response = self.client.get('/test/%d/' % self.test.id)
        self.assertContains(response, 'name="dev_display"')
        self.assertContains(response, 'name="base_display"')
        self.assertContains(response, '表示設定')


class PGNArchiveTests(TestCase):

    def setUp(self):
        from OpenBench.config import OPENBENCH_CONFIG, OPENBENCH_CONFIG_CHECKSUM
        from OpenBench.models import Engine, Machine, Result

        self.user = User.objects.create_user('alice', 'a@example.com', 'pw-alice')
        Profile.objects.create(user=self.user, enabled=True)
        engine = Engine.objects.create(name='archive', source='s', sha='a' * 40, bench=1)
        self.test = Test.objects.create(
            author='alice', dev=engine, base=engine,
            dev_engine='YaneuraOu-nagisa', base_engine='YaneuraOu-nagisa',
            finished=True, upload_pgns='COMPACT')
        self.machine = Machine.objects.create(
            user=self.user, secret='worker-secret', workload=0, info={
                'client_ver' : OPENBENCH_CONFIG['client_version'],
                'OPENBENCH_CONFIG_CHECKSUM' : OPENBENCH_CONFIG_CHECKSUM,
            })
        self.result = Result.objects.create(test=self.test, machine=self.machine)

    def upload(self, part, content):
        return self.client.post('/clientSubmitPGN/', {
            'machine_id' : self.machine.id,
            'secret'     : self.machine.secret,
            'test_id'    : self.test.id,
            'result_id'  : self.result.id,
            'book_index' : 10,
            'part'       : part,
            'file'       : SimpleUploadedFile(
                'games.pgn.bz2', bz2.compress(content)),
        })

    def test_parts_are_archived_once_and_downloadable(self):
        from django.core.files.base import ContentFile
        from django.core.files.storage import FileSystemStorage
        from OpenBench.models import PGN
        from OpenBench.pgn_archive import archive_path, archive_status
        from OpenBench.pgn_watcher import PGNWatcher

        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            self.assertEqual(self.upload(0, b'first pgn').json(), {})
            first = PGN.objects.get(part=0)
            watcher = PGNWatcher(threading.Event())
            watcher.process_pgn(first)

            # tar追記後・DB更新前の停止を模しても、同じメンバーを増やさない。
            first.processed = False
            first.save()
            FileSystemStorage().save(
                first.filename(), ContentFile(bz2.compress(b'first pgn')))
            watcher.process_pgn(first)

            # 応答消失を模した同じpartの再送も、新しい行を作らない。
            self.assertEqual(self.upload(0, b'duplicate pgn').json(), {})
            self.assertEqual(PGN.objects.count(), 1)

            self.assertEqual(self.upload(1, b'second pgn').json(), {})
            second = PGN.objects.get(part=1)
            watcher.process_pgn(second)
            self.assertEqual(archive_status(self.test), 'ready')

            with tarfile.open(archive_path(self.test.id), 'r') as archive:
                members = archive.getmembers()
                self.assertEqual([member.name for member in members], [
                    first.filename(), second.filename(),
                ])
                contents = [
                    bz2.decompress(archive.extractfile(member).read())
                    for member in members
                ]
                self.assertEqual(contents, [b'first pgn', b'second pgn'])

            self.client.login(username='alice', password='pw-alice')
            response = self.client.get('/api/pgns/%d/' % self.test.id)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response['Content-Disposition'],
                'attachment; filename=%d.pgn.tar' % self.test.id)
            self.assertGreater(len(b''.join(response.streaming_content)), 0)

    def test_missing_archive_is_reported_in_api_and_page(self):
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            self.client.login(username='alice', password='pw-alice')
            response = self.client.get('/api/pgns/%d/' % self.test.id)
            self.assertEqual(response.json()['archive_status'], 'missing')
            self.assertIn('No PGNs were received', response.json()['error'])

            response = self.client.get('/test/%d/' % self.test.id)
            self.assertContains(response, '棋譜アーカイブなし')
            self.assertNotContains(
                response, 'href="/api/pgns/%d/"' % self.test.id)

class BuildCommandNormalizationTests(TestCase):

    def test_strips_make_and_managed_arguments(self):
        args, dropped = normalize_build_command(
            'make -j8 normal COMPILER=clang++ TARGET_CPU=AVX2 '
            'YANEURAOU_EDITION=YANEURAOU_ENGINE_NNUE EXE=out EVALFILE=x.bin')
        self.assertEqual(args,
            'normal COMPILER=clang++ TARGET_CPU=AVX2 YANEURAOU_EDITION=YANEURAOU_ENGINE_NNUE')
        self.assertIn('make', dropped)
        self.assertIn('-j8', dropped)
        self.assertIn('EXE=out', dropped)
        self.assertIn('EVALFILE=x.bin', dropped)

    def test_bare_arguments_pass_through(self):
        args, dropped = normalize_build_command('normal TARGET_CPU=ZEN3')
        self.assertEqual(args, 'normal TARGET_CPU=ZEN3')
        self.assertEqual(dropped, [])

    def test_drops_cxx(self):
        args, dropped = normalize_build_command('make CXX=g++ EXTRA=1')
        self.assertEqual(args, 'EXTRA=1')

    def test_full_yaneuraou_paste(self):
        import shlex
        pasted = '''cd YaneuraOu/source

make clean YANEURAOU_EDITION=YANEURAOU_ENGINE_NNUE_HALFKP_768X2_16_64

make -j"$(nproc)" tournament \\

  COMPILER=clang++ \\

  YANEURAOU_EDITION=YANEURAOU_ENGINE_NNUE_HALFKP_768X2_16_64 \\

  ENGINE_NAME="Suisho10beta2" \\

  TARGET_CPU=AVX2 \\

  EXTRA_CPPFLAGS='-DHASH_KEY_BITS=128 -DTT_CLUSTER_SIZE=4'
'''
        args, dropped = normalize_build_command(pasted)

        # The worker re-splits with shlex: quoting must round-trip exactly
        self.assertEqual(shlex.split(args), [
            'tournament',
            'COMPILER=clang++',
            'YANEURAOU_EDITION=YANEURAOU_ENGINE_NNUE_HALFKP_768X2_16_64',
            'ENGINE_NAME=Suisho10beta2',
            'TARGET_CPU=AVX2',
            'EXTRA_CPPFLAGS=-DHASH_KEY_BITS=128 -DTT_CLUSTER_SIZE=4',
        ])

        # cd and clean lines are ignored, make/-j are managed by the worker
        self.assertIn('make', dropped)
        self.assertIn('-j$(nproc)', dropped)

    def test_bare_j_with_separate_count(self):
        args, dropped = normalize_build_command('make -j 8 normal FOO=1')
        self.assertEqual(args, 'normal FOO=1')
        self.assertIn('8', dropped)

    def test_suisho11_paste_with_target_and_cd_chain(self):
        import shlex
        pasted = '''cd /root/YaneuraOu/source && \\
    make -j"$(nproc)" \\
      YANEURAOU_EDITION=YANEURAOU_ENGINE_SFNN_halfka2_1024_7_64_k3k3 \\
      PYTHON=python3 \\
      TARGET_CPU=AVX512VNNI \\
      COMPILER=clang++ \\
      TARGET=/usr/local/bin/Suisho11-YaneuraOu-tournament-avx512vnni \\
      tournament'''
        args, dropped = normalize_build_command(pasted)

        self.assertEqual(shlex.split(args), [
            'YANEURAOU_EDITION=YANEURAOU_ENGINE_SFNN_halfka2_1024_7_64_k3k3',
            'PYTHON=python3',
            'TARGET_CPU=AVX512VNNI',
            'COMPILER=clang++',
            'tournament',
        ])

        # TARGET= (the output path) is managed by the worker; TARGET_CPU stays
        self.assertIn('TARGET=/usr/local/bin/Suisho11-YaneuraOu-tournament-avx512vnni', dropped)
        self.assertIn('-j$(nproc)', dropped)

class BuildVariantPageTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'account-password')
        Profile.objects.create(user=self.user, enabled=True)
        self.client.login(username='alice', password='account-password')

    def test_page_requires_login(self):
        response = Client().get('/builds/')
        self.assertEqual(response.status_code, 302)

    def test_create_normalizes_command(self):
        self.client.post('/builds/', {
            'action'  : 'create',
            'engine'  : 'YaneuraOu-nagisa',
            'name'    : 'NNUE-custom',
            'command' : 'make -j normal COMPILER=clang++ YANEURAOU_EDITION=FOO',
        })
        variant = BuildVariant.objects.get(engine='YaneuraOu-nagisa', name='NNUE-custom')
        self.assertEqual(variant.args, 'normal COMPILER=clang++ YANEURAOU_EDITION=FOO')
        self.assertEqual(variant.author, 'alice')

        # And it shows up in the merged variant list used by the form
        self.assertIn('NNUE-custom', engine_build_variants('YaneuraOu-nagisa'))

    def test_cannot_shadow_predefined_variant(self):
        response = self.client.post('/builds/', {
            'action'  : 'create',
            'engine'  : 'YaneuraOu-nagisa',
            'name'    : 'default',
            'command' : 'make whatever',
        }, follow=True)
        self.assertContains(response, 'predefined')
        self.assertFalse(BuildVariant.objects.filter(name='default').exists())

    def test_delete_requires_ownership(self):
        other = User.objects.create_user('bob', 'b@example.com', 'password2')
        Profile.objects.create(user=other, enabled=True)
        variant = BuildVariant.objects.create(
            engine='YaneuraOu-nagisa', name='bobsbuild', args='normal', author='bob')

        self.client.post('/builds/', { 'action' : 'delete', 'variant_id' : variant.id })
        self.assertTrue(BuildVariant.objects.filter(id=variant.id).exists())

    def test_owner_can_delete(self):
        variant = BuildVariant.objects.create(
            engine='YaneuraOu-nagisa', name='mine', args='normal', author='alice')
        self.client.post('/builds/', { 'action' : 'delete', 'variant_id' : variant.id })
        self.assertFalse(BuildVariant.objects.filter(id=variant.id).exists())

    def test_page_renders_static_and_db_variants(self):
        BuildVariant.objects.create(
            engine='YaneuraOu-nagisa', name='mine', args='normal FOO=1', author='alice')
        self.assertEqual(set(engine_build_variants('YaneuraOu-nagisa')), {'default', 'mine'})

        response = self.client.get('/builds/')
        self.assertContains(response, 'mine')
        self.assertContains(response, 'default')
        self.assertNotContains(response, 'NNUE-KP256')

class PonderModeWorkloadTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'account-password')
        Profile.objects.create(user=self.user, enabled=True, approver=True)
        self.client.login(username='alice', password='account-password')

    def test_form_exposes_modes_for_both_engines(self):
        response = self.client.get('/test/new/')

        self.assertContains(response, 'name="dev_ponder_mode"')
        self.assertContains(response, 'name="base_ponder_mode"')
        self.assertContains(response, 'value="standard"')
        self.assertContains(response, 'value="early"')

    def test_modes_are_saved_displayed_and_sent_to_worker(self):
        response = self.create_test()
        self.assertEqual(response.status_code, 302)
        self.assertIn('/index/', response.url)

        test = Test.objects.get(test_mode='GAMES')
        self.assertEqual(test.dev_ponder_mode, Test.PonderMode.STANDARD)
        self.assertEqual(test.base_ponder_mode, Test.PonderMode.EARLY)

        distribution = {
            'runner-count'     : 1,
            'concurrency-per'  : 1,
            'games-per-runner' : 2,
        }
        with patch('OpenBench.workloads.get_workload.game_distribution', return_value=distribution):
            workload = workload_to_dictionary(test, SimpleNamespace(id=7), None)

        self.assertEqual(workload['test']['dev']['ponder_mode'], 'standard')
        self.assertEqual(workload['test']['base']['ponder_mode'], 'early')

        detail = self.client.get('/test/%d/' % test.id)
        self.assertContains(detail, 'Dev Ponder方式')
        self.assertContains(detail, '通常 Ponder')
        self.assertContains(detail, 'Base Ponder方式')
        self.assertContains(detail, '早期 Ponder')

    def test_required_engine_options_are_merged_on_form_creation(self):
        response = self.create_test(
            dev_options='Threads=1 Hash=16 NetworkDelay=999 CustomOption=dev',
            base_options='Threads=1 Hash=16 networkdelay2=999 CustomOption=base')

        self.assertEqual(response.status_code, 302)
        test = Test.objects.get(test_mode='GAMES')
        required = (
            'USI_OwnBook=false NetworkDelay=0 NetworkDelay2=0 '
            'MinimumThinkingTime=100 RoundUpToFullSecond=false')
        self.assertEqual(
            test.dev_options,
            'Threads=1 Hash=16 CustomOption=dev ' + required)
        self.assertEqual(
            test.base_options,
            'Threads=1 Hash=16 CustomOption=base ' + required)

    def test_required_option_merge_preserves_quoted_values(self):
        self.assertEqual(
            merge_required_options(
                'Threads=1 networkdelay=999 Label="value with spaces"',
                'NetworkDelay=0 NetworkDelay2=0'),
            'Threads=1 Label="value with spaces" NetworkDelay=0 NetworkDelay2=0')

    def test_ponder_distribution_reserves_both_engines_on_physical_cores(self):
        self.create_test()
        test = Test.objects.get(test_mode='GAMES')
        machine = SimpleNamespace(info={
            'concurrency'    : 16,
            'physical_cores' : 8,
            'hard_cpu_affinity': True,
            'sockets'        : 2,
            'os_name'        : 'Linux',
        })

        distribution = game_distribution(test, machine)

        self.assertEqual(distribution['threads-per-game'], 2)
        self.assertEqual(distribution['concurrency-per'], 4)
        self.assertEqual(distribution['runner-count'], 1)
        self.assertTrue(distribution['cpu-affinity'])

    def test_non_ponder_distribution_keeps_the_existing_max_thread_budget(self):
        self.create_test(dev_ponder_mode='off', base_ponder_mode='off')
        test = Test.objects.get(test_mode='GAMES')
        machine = SimpleNamespace(info={
            'concurrency'    : 8,
            'physical_cores' : 8,
            'hard_cpu_affinity': True,
            'sockets'        : 1,
            'os_name'        : 'Linux',
        })

        distribution = game_distribution(test, machine)

        self.assertEqual(distribution['threads-per-game'], 1)
        self.assertEqual(distribution['concurrency-per'], 8)
        self.assertFalse(distribution['cpu-affinity'])

    def test_ponder_distribution_sums_unequal_engine_thread_counts(self):
        self.create_test(
            dev_options='Threads=2 Hash=16',
            base_options='Threads=1 Hash=16')
        test = Test.objects.get(test_mode='GAMES')
        machine = SimpleNamespace(info={
            'concurrency'      : 12,
            'physical_cores'   : 12,
            'hard_cpu_affinity': True,
            'sockets'          : 1,
            'os_name'          : 'Linux',
        })

        distribution = game_distribution(test, machine)

        self.assertEqual(distribution['threads-per-game'], 3)
        self.assertEqual(distribution['concurrency-per'], 4)

    def test_ponder_work_is_not_assigned_without_linux_hard_affinity(self):
        self.create_test()
        test = Test.objects.get(test_mode='GAMES')
        machine = SimpleNamespace(info={
            'concurrency'    : 8,
            'physical_cores' : 8,
            'hard_cpu_affinity': False,
            'sockets'        : 1,
            'os_name'        : 'Linux',
        })

        self.assertFalse(valid_hardware_assignment(test, machine))

    def test_invalid_mode_is_rejected(self):
        response = self.create_test(dev_ponder_mode='unexpected')
        self.assertIn('/test/new/', response.url)
        self.assertFalse(Test.objects.exists())

    def test_early_mode_rejects_clockless_time_control(self):
        response = self.create_test(dev_ponder_mode='early', dev_time_control='N=1000')
        self.assertIn('/test/new/', response.url)
        self.assertFalse(Test.objects.exists())

    def test_missing_modes_keep_existing_off_behavior(self):
        form = self.test_form()
        del form['dev_ponder_mode']
        del form['base_ponder_mode']

        response = self.post_test(form)
        self.assertEqual(response.status_code, 302)

        test = Test.objects.get(test_mode='GAMES')
        self.assertEqual(test.dev_ponder_mode, Test.PonderMode.OFF)
        self.assertEqual(test.base_ponder_mode, Test.PonderMode.OFF)

    def test_form(self, **overrides):
        form = {
            'dev_engine'         : 'YaneuraOu-nagisa',
            'dev_repo'           : 'https://github.com/keinoda/YaneuraOu',
            'dev_branch'         : 'master',
            'dev_bench'          : '',
            'dev_network'        : '',
            'dev_build'          : 'default',
            'dev_options'        : 'Threads=1 Hash=16',
            'dev_time_control'   : '10+0.1',
            'dev_ponder_mode'    : 'standard',
            'base_engine'        : 'YaneuraOu-nagisa',
            'base_repo'          : 'https://github.com/keinoda/YaneuraOu',
            'base_branch'        : 'master',
            'base_bench'         : '',
            'base_network'       : '',
            'base_build'         : 'default',
            'base_options'       : 'Threads=1 Hash=16',
            'base_time_control'  : '10+0.1',
            'base_ponder_mode'   : 'early',
            'book_name'          : 'yaneuraou2025_ply24_shogi_sfen.epd',
            'upload_pgns'        : 'FALSE',
            'test_mode'          : 'GAMES',
            'test_bounds'        : 'N/A',
            'test_confidence'    : 'N/A',
            'test_max_games'     : '2',
            'priority'           : '0',
            'throughput'         : '1000',
            'workload_size'      : '1',
            'scale_method'       : 'BASE',
            'scale_nps'          : '1000000',
            'syzygy_wdl'         : 'DISABLED',
            'syzygy_adj'         : 'DISABLED',
            'win_adj'            : 'None',
            'draw_adj'           : 'None',
        }
        form.update(overrides)
        return form

    def post_test(self, form):
        github_info = (
            'https://github.com/keinoda/YaneuraOu/archive/' + 'b' * 40 + '.zip',
            'master',
            'a' * 40,
            0,
        )
        with patch('OpenBench.workloads.verify_workload.collect_github_info',
                   return_value=(github_info, True)):
            return self.client.post('/test/new/', form)

    def create_test(self, **overrides):
        return self.post_test(self.test_form(**overrides))

class TestCreationAPITests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            'Agent-AI', 'agent@example.com', 'test-api-password')
        Profile.objects.create(
            user=self.user, enabled=True, approver=False)
        self.worker_key = WorkerKey.objects.create(
            user=self.user, name='agent-worker', token='w' * 48)
        self.dev = Engine.objects.create(
            name='feature-branch',
            source='https://github.com/keinoda/YaneuraOu/archive/' + 'b' * 40 + '.zip',
            sha='b' * 40,
            bench=1234567,
        )
        self.base = Engine.objects.create(
            name='master',
            source='https://github.com/keinoda/YaneuraOu/archive/' + 'a' * 40 + '.zip',
            sha='a' * 40,
            bench=1234567,
        )

        self.payload = {
            'dev_engine'        : 'YaneuraOu-nagisa',
            'dev_repo'          : 'https://github.com/keinoda/YaneuraOu',
            'dev_branch'        : 'master',
            'dev_bench'         : '',
            'dev_network'       : '',
            'dev_build'         : 'default',
            'dev_options'       : 'Threads=1 Hash=16',
            'dev_time_control'  : '10+0.1',
            'dev_ponder_mode'   : 'off',
            'base_engine'       : 'YaneuraOu-nagisa',
            'base_repo'         : 'https://github.com/keinoda/YaneuraOu',
            'base_branch'       : 'master',
            'base_bench'        : '',
            'base_network'      : '',
            'base_build'        : 'default',
            'base_options'      : 'Threads=1 Hash=16',
            'base_time_control' : '10+0.1',
            'base_ponder_mode'  : 'off',
            'book_name'         : 'yaneuraou2025_ply24_shogi_sfen.epd',
            'upload_pgns'       : 'FALSE',
            'test_mode'         : 'GAMES',
            'test_max_games'    : '2',
            'priority'          : '0',
            'throughput'        : '1000',
            'workload_size'     : '1',
            'scale_method'      : 'BASE',
            'scale_nps'         : '1000000',
            'syzygy_wdl'        : 'DISABLED',
            'syzygy_adj'        : 'DISABLED',
            'win_adj'           : 'None',
            'draw_adj'          : 'None',
        }

    def basic_auth(self, password='test-api-password'):
        token = base64.b64encode(
            ('Agent-AI:%s' % password).encode('utf-8')).decode('ascii')
        return 'Basic %s' % token

    def post_json(self, payload=None, password='test-api-password'):
        return self.client.post(
            '/api/tests/',
            data=json.dumps(payload or self.payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.basic_auth(password),
        )

    def get_tests(self, params=None, password='test-api-password'):
        return self.client.get(
            '/api/tests/',
            data=params or {},
            HTTP_AUTHORIZATION=self.basic_auth(password),
        )

    def make_workload(self, **overrides):
        data = {
            'author'            : 'Agent-AI',
            'upload_pgns'       : 'FALSE',
            'book_name'         : 'yaneuraou2025_ply24_shogi_sfen.epd',
            'dev'               : self.dev,
            'dev_repo'          : 'https://github.com/keinoda/YaneuraOu',
            'dev_engine'        : 'YaneuraOu-nagisa',
            'dev_options'       : 'Threads=1 Hash=16',
            'dev_network'       : '',
            'dev_netname'       : '',
            'dev_time_control'  : '10.0+0.10',
            'base'              : self.base,
            'base_repo'         : 'https://github.com/keinoda/YaneuraOu',
            'base_engine'       : 'YaneuraOu-nagisa',
            'base_options'      : 'Threads=1 Hash=16',
            'base_network'      : '',
            'base_netname'      : '',
            'base_time_control' : '10.0+0.10',
            'test_mode'         : 'GAMES',
            'max_games'         : 200,
        }
        data.update(overrides)
        return Test.objects.create(**data)

    def github_info(self):
        return (
            'https://github.com/keinoda/YaneuraOu/archive/' + 'b' * 40 + '.zip',
            'master',
            'a' * 40,
            0,
        )

    def test_non_approver_can_create_pending_test_with_json(self):
        with patch(
                'OpenBench.workloads.verify_workload.collect_github_info',
                return_value=(self.github_info(), True)):
            response = self.post_json()

        self.assertEqual(response.status_code, 201)
        workload = Test.objects.get()
        self.assertEqual(response.json()['test']['id'], workload.id)
        self.assertEqual(response.json()['test']['author'], 'Agent-AI')
        self.assertFalse(response.json()['test']['approved'])
        self.assertFalse(workload.approved)
        self.assertEqual(workload.author, 'Agent-AI')

        profile = Profile.objects.get(user=self.user)
        self.assertEqual(profile.tests, 1)
        self.assertTrue(LogEvent.objects.filter(
            author='Agent-AI', test_id=workload.id).exists())

        status_response = self.get_tests({ 'author': 'Agent-AI' })
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()['summary']['pending'], 1)
        self.assertEqual(status_response.json()['tests'][0]['id'], workload.id)
        self.assertEqual(status_response.json()['tests'][0]['status'], 'pending')

    def test_required_engine_options_are_merged_on_api_creation(self):
        payload = dict(
            self.payload,
            dev_options='Threads=1 Hash=16 NetworkDelay2=500',
            base_options='Threads=1 Hash=16 RoundUpToFullSecond=true')
        with patch(
                'OpenBench.workloads.verify_workload.collect_github_info',
                return_value=(self.github_info(), True)):
            response = self.post_json(payload)

        self.assertEqual(response.status_code, 201)
        test = Test.objects.get()
        required = (
            'USI_OwnBook=false NetworkDelay=0 NetworkDelay2=0 '
            'MinimumThinkingTime=100 RoundUpToFullSecond=false')
        self.assertEqual(test.dev_options, 'Threads=1 Hash=16 ' + required)
        self.assertEqual(test.base_options, 'Threads=1 Hash=16 ' + required)

    def test_form_credentials_can_create_test(self):
        data = dict(
            self.payload,
            username='Agent-AI',
            password='test-api-password',
        )
        with patch(
                'OpenBench.workloads.verify_workload.collect_github_info',
                return_value=(self.github_info(), True)):
            response = self.client.post('/api/tests/', data)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Test.objects.get().author, 'Agent-AI')

    def test_invalid_credentials_are_rejected(self):
        response = self.post_json(password='wrong-password')
        self.assertEqual(response.status_code, 401)
        self.assertFalse(Test.objects.exists())

    def test_disabled_profile_is_rejected(self):
        profile = Profile.objects.get(user=self.user)
        profile.enabled = False
        profile.save(update_fields=['enabled'])

        response = self.post_json()
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Test.objects.exists())

    def test_worker_key_cannot_create_test(self):
        response = self.post_json(password=self.worker_key.token)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(Test.objects.exists())

    def test_missing_fields_return_structured_error(self):
        payload = dict(self.payload)
        del payload['dev_repo']

        response = self.post_json(payload)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'Missing required fields')
        self.assertIn('dev_repo', response.json()['details'])
        self.assertFalse(Test.objects.exists())

    def test_ui_validation_errors_are_returned_as_json(self):
        payload = dict(self.payload, dev_ponder_mode='invalid')
        with patch(
                'OpenBench.workloads.verify_workload.collect_github_info',
                return_value=(self.github_info(), True)):
            response = self.post_json(payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'Test validation failed')
        self.assertTrue(any(
            'Ponder' in detail for detail in response.json()['details']))
        self.assertFalse(Test.objects.exists())

    def test_get_returns_current_test_status_and_results(self):
        pending = self.make_workload()
        awaiting = self.make_workload(awaiting=True)
        active = self.make_workload(
            approved=True,
            test_mode='SPRT',
            elolower=0.0,
            eloupper=5.0,
            lowerllr=-2.94,
            currentllr=0.75,
            upperllr=2.94,
            games=20,
            wins=8,
            losses=6,
            draws=6,
            LL=2,
            LD=3,
            DD=4,
            DW=1,
            WW=0,
        )
        completed = self.make_workload(approved=True, finished=True, passed=True)
        deleted = self.make_workload(deleted=True)

        response = self.get_tests()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['query'], { 'status': 'current', 'author': None })
        self.assertEqual(body['summary'], {
            'all'       : 4,
            'current'   : 3,
            'pending'   : 1,
            'awaiting'  : 1,
            'active'    : 1,
            'completed' : 1,
        })
        self.assertEqual(body['pagination']['total'], 3)
        self.assertFalse(body['pagination']['has_more'])
        self.assertEqual(
            {test['id'] for test in body['tests']},
            {pending.id, awaiting.id, active.id},
        )
        self.assertNotIn(completed.id, {test['id'] for test in body['tests']})
        self.assertNotIn(deleted.id, {test['id'] for test in body['tests']})

        by_id = {test['id']: test for test in body['tests']}
        self.assertEqual(by_id[pending.id]['status'], 'pending')
        self.assertEqual(by_id[awaiting.id]['status'], 'awaiting')
        self.assertEqual(by_id[active.id]['status'], 'active')
        self.assertEqual(by_id[active.id]['workload_type'], 'test')
        self.assertEqual(by_id[active.id]['engines']['dev']['branch'], 'feature-branch')
        self.assertEqual(by_id[active.id]['mode_config']['llr']['current'], 0.75)
        self.assertEqual(by_id[active.id]['results']['games'], 20)
        self.assertEqual(by_id[active.id]['results']['pentanomial']['DD'], 4)

    def test_get_can_filter_completed_tests_by_author_and_page(self):
        first = self.make_workload(approved=True, finished=True)
        second = self.make_workload(approved=True, finished=True)
        self.make_workload(author='someone-else', approved=True, finished=True)

        first_page = self.get_tests({
            'status' : 'completed',
            'author' : 'Agent-AI',
            'limit'  : '1',
            'offset' : '0',
        })
        second_page = self.get_tests({
            'status' : 'completed',
            'author' : 'Agent-AI',
            'limit'  : '1',
            'offset' : '1',
        })

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(first_page.json()['summary']['all'], 2)
        self.assertEqual(first_page.json()['pagination']['total'], 2)
        self.assertTrue(first_page.json()['pagination']['has_more'])
        self.assertFalse(second_page.json()['pagination']['has_more'])
        returned_ids = {
            first_page.json()['tests'][0]['id'],
            second_page.json()['tests'][0]['id'],
        }
        self.assertEqual(returned_ids, {first.id, second.id})
        self.assertTrue(all(
            test['author'] == 'Agent-AI'
            for test in first_page.json()['tests'] + second_page.json()['tests']))

    def test_get_rejects_invalid_query_parameters(self):
        invalid_queries = (
            { 'status': 'running' },
            { 'limit': 'not-a-number' },
            { 'limit': '0' },
            { 'limit': '201' },
            { 'offset': '-1' },
        )

        for params in invalid_queries:
            with self.subTest(params=params):
                response = self.get_tests(params)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['error'], 'Invalid query parameter')

    def test_get_requires_enabled_account_password(self):
        response = self.client.get('/api/tests/')
        self.assertEqual(response.status_code, 401)

        response = self.get_tests(password='wrong-password')
        self.assertEqual(response.status_code, 401)

        response = self.get_tests(password=self.worker_key.token)
        self.assertEqual(response.status_code, 401)

        profile = Profile.objects.get(user=self.user)
        profile.enabled = False
        profile.save(update_fields=['enabled'])
        response = self.get_tests()
        self.assertEqual(response.status_code, 403)

    def test_get_accepts_authenticated_session(self):
        self.client.login(username='Agent-AI', password='test-api-password')
        response = self.client.get('/api/tests/')
        self.assertEqual(response.status_code, 200)

    def test_unsupported_method_returns_allow_header(self):
        response = self.client.put('/api/tests/')
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response['Allow'], 'GET, POST')

    def test_basic_auth_works_for_existing_config_api(self):
        response = self.client.get(
            '/api/config/', HTTP_AUTHORIZATION=self.basic_auth())
        self.assertEqual(response.status_code, 200)
        self.assertIn('engines', response.json())

class StatBlockTests(TestCase):

    def make_test(self, **overrides):
        dev = Engine(
            name='master',
            source='https://github.com/keinoda/YaneuraOu',
            sha='527a083a89c1f2e4413ab3b8664c0f2fb01769c0',
            bench=0)
        base = Engine(
            name='master',
            source='https://github.com/keinoda/YaneuraOu',
            sha='527a083a89c1f2e4413ab3b8664c0f2fb01769c0',
            bench=0)
        data = {
            'author'           : 'Nagisa',
            'book_name'        : 'yaneuraou2025_ply24_shogi_sfen.epd',
            'dev'              : dev,
            'dev_repo'         : 'https://github.com/keinoda/YaneuraOu',
            'dev_engine'       : 'YaneuraOu-nagisa',
            'dev_options'      : 'Threads=4 Hash=256',
            'dev_network'      : '642047FD',
            'dev_netname'      : 'Suisho11',
            'dev_time_control' : '10.0+0.10',
            'dev_build_name'   : 'Suisho11',
            'dev_build_args'   : 'tournament TARGET_CPU=AVX512VNNI',
            'base'             : base,
            'base_repo'        : 'https://github.com/keinoda/YaneuraOu',
            'base_engine'      : 'YaneuraOu-nagisa',
            'base_options'     : 'Threads=4 Hash=256',
            'base_network'     : 'C7FFBD17',
            'base_netname'     : 'fuuppi-v3',
            'base_time_control': '10.0+0.10',
            'base_build_name'  : 'fuuppi-v3',
            'base_build_args'  : 'YANEURAOU_EDITION=YANEURAOU_ENGINE_SFNN_halfkahm2_768_7_64_ls9',
            'scale_method'     : 'BASE',
            'scale_nps'        : 1000000,
            'syzygy_wdl'       : 'DISABLED',
            'syzygy_adj'       : 'OPTIONAL',
            'win_adj'          : 'movecount=3 score=2000',
            'draw_adj'         : 'movenumber=40 movecount=8 score=10',
            'test_mode'        : 'SPRT',
            'elolower'         : 0.0,
            'eloupper'         : 4.0,
            'lowerllr'         : -2.25,
            'currentllr'       : -0.12,
            'upperllr'         : 2.89,
            'games'            : 48,
            'losses'           : 27,
            'draws'            : 3,
            'wins'             : 18,
            'LL'               : 7,
            'LD'               : 1,
            'DD'               : 12,
            'DW'               : 2,
            'WW'               : 2,
            'use_penta'        : True,
        }
        data.update(overrides)
        return Test(**data)

    def test_long_statblock_includes_conditions_and_dev_perspective(self):
        block = longStatBlock(self.make_test())

        self.assertTrue(block.startswith('```text\nSuisho11-dev vs fuuppi-v3-base'))
        self.assertTrue(block.endswith('\n```'))
        self.assertIn('Score for: Suisho11-dev', block)
        self.assertNotIn('STRONGER', block)
        self.assertIn('SPRT     : 10.0+0.10s, Threads=4, Hash=256MB', block)
        self.assertIn('Book     : yaneuraou2025_ply24_shogi_sfen.epd', block)
        self.assertIn('Games    : N=48 W=18 L=27 D=3', block)
        self.assertIn('Ptnml    : [7, 1, 12, 2, 2]', block)
        self.assertNotIn('Dev    |', block)
        self.assertNotIn('Base   |', block)
        self.assertNotIn('Options|', block)
        self.assertNotIn('Adjud  |', block)
        self.assertNotIn('Syzygy |', block)
        self.assertNotIn('BASE 1000000 NPS', block)
        self.assertNotIn('Scale  |', block)

    def test_long_statblock_reports_time_or_thread_odds(self):
        block = longStatBlock(self.make_test(
            base_time_control='5.0+0.05',
            base_options='Threads=2 Hash=128'))

        self.assertIn(
            'Dev 10.0+0.10s T=4 H=256MB / Base 5.0+0.05s T=2 H=128MB',
            block)

    def test_long_statblock_omits_pentanomial_for_trinomial_test(self):
        block = longStatBlock(self.make_test(use_penta=False, use_tri=True))

        self.assertNotIn('Ptnml', block)

    def test_long_statblock_expands_peta_book_parameters(self):
        block = longStatBlock(self.make_test(
            book_name='peta1204_d8d10_32to80_shogi.epd'))

        self.assertIn(
            'Book     : peta1204_depth8_diff10_32to80_shogi.epd', block)
        self.assertNotIn('Book     : peta1204_d8d10_32to80_shogi.epd', block)

class SSHTargetParsingTests(TestCase):

    def test_vastai_connect_string(self):
        self.assertEqual(parse_ssh_target('ssh -p 12345 root@ssh4.vast.ai'),
                         ('root', 'ssh4.vast.ai', 12345))

    def test_trailing_port_flag(self):
        self.assertEqual(parse_ssh_target('ssh root@ssh4.vast.ai -p 12345'),
                         ('root', 'ssh4.vast.ai', 12345))

    def test_user_host_port(self):
        self.assertEqual(parse_ssh_target('ubuntu@203.0.113.7:2222'),
                         ('ubuntu', '203.0.113.7', 2222))

    def test_aws_user_host_defaults_to_port_22(self):
        self.assertEqual(parse_ssh_target('ec2-user@203.0.113.7'),
                         ('ec2-user', '203.0.113.7', 22))

    def test_host_port(self):
        self.assertEqual(parse_ssh_target('ssh4.vast.ai:12345'),
                         ('root', 'ssh4.vast.ai', 12345))

    def test_bare_host_defaults(self):
        self.assertEqual(parse_ssh_target('203.0.113.7'), ('root', '203.0.113.7', 22))

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_ssh_target('this is not an address at all !!!')

class WorkerConnectTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'account-password')
        Profile.objects.create(user=self.user, enabled=True)
        self.key = WorkerKey.objects.create(user=self.user, name='vast', token='a' * 48)
        self.client.login(username='alice', password='account-password')

    def test_page_shows_setup_instructions_without_key(self):
        with override_settings(SSH_PRIVATE_KEY='', SSH_PRIVATE_KEY_FILE='/nonexistent'):
            response = self.client.get('/workers/')
        self.assertContains(response, 'OPENBENCH_SSH_PRIVATE_KEY')

    def test_page_shows_public_key_and_fingerprint_with_key(self):
        with override_settings(SSH_PRIVATE_KEY=test_ssh_key()):
            response = self.client.get('/workers/')
        self.assertContains(response, 'SHA256:')
        self.assertContains(response, 'ssh-rsa')
        self.assertContains(response, 'vast.ai に貼る鍵ではありません')

    @patch('OpenBench.views.paramiko.SSHClient')
    def test_connect_launches_worker(self, mock_ssh_client):
        connection = mock_ssh_client.return_value
        stdout = MagicMock(); stdout.readline.return_value = 'LAUNCHED\n'
        connection.exec_command.return_value = (MagicMock(), stdout, MagicMock())

        with override_settings(SSH_PRIVATE_KEY=test_ssh_key()):
            response = self.client.post('/workers/connect/', {
                'ssh_target' : 'ssh -p 12345 root@ssh4.vast.ai',
                'key_id'     : self.key.id,
                'threads'    : '',
            })

        self.assertEqual(response.status_code, 302)
        connection.connect.assert_called_once()
        self.assertEqual(connection.connect.call_args.args[0], 'ssh4.vast.ai')
        self.assertEqual(connection.connect.call_args.kwargs['port'], 12345)

        command = connection.exec_command.call_args.args[0]
        self.assertIn(self.key.token, command)
        self.assertIn('OPENBENCH_USERNAME=alice', command)
        self.assertIn('SHOGIBENCH_PROTECTED_PIDS="$$ $PPID"', command)
        self.assertIn('shogibench_setup.sh', command)

        # スクリプトは一時名にアップロードし mv で原子的に設置する (二重POSTが
        # 実行中のスクリプトを truncate しないように)
        self.assertIn('mv -f /tmp/shogibench_setup.sh.', command)

    @patch('OpenBench.views.paramiko.SSHClient')
    def test_connect_falls_back_to_exec_upload_when_sftp_unavailable(self, mock_ssh_client):
        connection = mock_ssh_client.return_value
        connection.open_sftp.side_effect = Exception('EOF during negotiation')

        upload_stdin = MagicMock()
        upload_stdout = MagicMock()
        upload_stdout.channel.recv_exit_status.return_value = 0

        launch_stdout = MagicMock()
        launch_stdout.readline.return_value = 'LAUNCHED\n'
        connection.exec_command.side_effect = [
            (upload_stdin, upload_stdout, MagicMock()),
            (MagicMock(), launch_stdout, MagicMock()),
        ]

        with override_settings(SSH_PRIVATE_KEY=test_ssh_key()):
            response = self.client.post('/workers/connect/', {
                'ssh_target' : 'root@203.0.113.7:22',
                'key_id'     : self.key.id,
                'threads'    : '',
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(connection.exec_command.call_count, 2)

        upload_command = connection.exec_command.call_args_list[0].args[0]
        self.assertRegex(upload_command, r'^cat > /tmp/shogibench_setup\.sh\.[0-9a-f]{16}$')
        upload_stdin.write.assert_called_once()
        self.assertIsInstance(upload_stdin.write.call_args.args[0], bytes)
        upload_stdin.flush.assert_called_once()
        upload_stdin.channel.shutdown_write.assert_called_once()

        launch_command = connection.exec_command.call_args_list[1].args[0]
        self.assertIn(self.key.token, launch_command)
        self.assertIn('SHOGIBENCH_PROTECTED_PIDS="$$ $PPID"', launch_command)
        self.assertIn('nohup /tmp/shogibench_setup.sh', launch_command)

        # アップロード先の一時名と launch コマンドの mv 元が一致する
        remote_tmp = upload_command.split(' > ', 1)[1]
        self.assertIn('mv -f %s /tmp/shogibench_setup.sh' % (remote_tmp), launch_command)

    @patch('OpenBench.views.paramiko.SSHClient')
    def test_connect_auto_creates_worker_key(self, mock_ssh_client):
        self.key.delete()

        connection = mock_ssh_client.return_value
        stdout = MagicMock(); stdout.readline.return_value = 'LAUNCHED\n'
        connection.exec_command.return_value = (MagicMock(), stdout, MagicMock())

        with override_settings(SSH_PRIVATE_KEY=test_ssh_key()):
            response = self.client.post('/workers/connect/', {
                'ssh_target' : 'ssh4.vast.ai:12345',
            })

        self.assertEqual(response.status_code, 302)
        auto_key = WorkerKey.objects.get(user=self.user, name='auto')
        self.assertIn(auto_key.token, connection.exec_command.call_args.args[0])

    def test_connect_without_server_key_errors(self):
        with override_settings(SSH_PRIVATE_KEY='', SSH_PRIVATE_KEY_FILE='/nonexistent'):
            response = self.client.post('/workers/connect/', {
                'ssh_target' : 'ssh4.vast.ai:12345',
                'key_id'     : self.key.id,
            }, follow=True)
        self.assertContains(response, '秘密鍵が設定されていません')

    @override_settings(PUBLIC_URL='https://bench.example.com')
    @patch('OpenBench.views.paramiko.SSHClient')
    def test_connect_uses_configured_public_url(self, mock_ssh_client):
        connection = mock_ssh_client.return_value
        stdout = MagicMock(); stdout.readline.return_value = 'LAUNCHED\n'
        connection.exec_command.return_value = (MagicMock(), stdout, MagicMock())

        with override_settings(SSH_PRIVATE_KEY=test_ssh_key()):
            self.client.post('/workers/connect/', {
                'ssh_target' : 'ssh4.vast.ai:12345',
                'key_id'     : self.key.id,
            })

        command = connection.exec_command.call_args.args[0]
        self.assertIn('OPENBENCH_SERVER=https://bench.example.com/', command)

    @patch('OpenBench.views.paramiko.SSHClient')
    def test_connect_failure_reports_error(self, mock_ssh_client):
        mock_ssh_client.return_value.connect.side_effect = Exception('Connection refused')

        with override_settings(SSH_PRIVATE_KEY=test_ssh_key()):
            response = self.client.post('/workers/connect/', {
                'ssh_target' : 'ssh4.vast.ai:12345',
                'key_id'     : self.key.id,
            }, follow=True)

        self.assertContains(response, 'SSH connection failed')

class SpsaRshogiLifecycleTests(TestCase):

    ## SPSA (rshogi ラッパー) の一連の流れ:
    ## GUI で作成 -> 承認 -> ワーカー割り当て -> 進捗報告 -> 完了 -> 表示

    PARAMS_TEXT = (
        'SPSA_LMR_BASE_QUIET, int, 181, 90, 362, 14, 0.0020\n'
        'SPSA_NMP_MARGIN_OFFSET, int, -390, -780, -195, 30, 0.0020 // sign_flip\n'
        'DeadParam, int, 10, 0, 20, 1, 0.0020 [[NOT USED]]\n')

    def setUp(self):
        self.alice = User.objects.create_user('alice', 'a@example.com', 'pw-alice')
        Profile.objects.create(user=self.alice, enabled=True, approver=True)
        self.client.login(username='alice', password='pw-alice')

    def tune_form(self, **overrides):
        form = {
            'dev_engine'        : 'YaneuraOu-nagisa',
            'dev_repo'          : 'https://github.com/keinoda/YaneuraOu',
            'dev_branch'        : 'master',
            'dev_bench'         : '',
            'dev_network'       : '',
            'dev_build'         : 'default',
            'dev_options'       : 'Threads=1 Hash=16 USI_OwnBook=false',
            'dev_time_control'  : '2+0.02',
            'book_name'         : 'taya36_shogi_sfen.epd',
            'priority'          : '0',
            'throughput'        : '1000',
            'scale_method'      : 'DEV',
            'scale_nps'         : '1000000',
            'spsa_inputs'       : self.PARAMS_TEXT,
            'spsa_alpha'        : '0.602',
            'spsa_gamma'        : '0.101',
            'spsa_a_ratio'      : '0.1',
            'spsa_total_pairs'  : '51200',
            'spsa_batch_pairs'  : '96',
            'spsa_seed'         : '1',
            'spsa_active_regex' : '^SPSA_',
            'spsa_mapping'      : 'YO',
        }
        form.update(overrides)
        return form

    def github_info(self):
        return ('https://github.com/keinoda/YaneuraOu/archive/' + 'b' * 40 + '.zip',
                'master', 'a' * 40, 0)

    def create_tune(self, **overrides):
        with patch('OpenBench.workloads.verify_workload.collect_github_info',
                   return_value=(self.github_info(), True)):
            response = self.client.post('/tune/new/', self.tune_form(**overrides))
        return response

    def make_machine(self, threads=192):
        from OpenBench.config import OPENBENCH_CONFIG, OPENBENCH_CONFIG_CHECKSUM
        from OpenBench.models import Machine
        return Machine.objects.create(user=self.alice, secret='s3cret', info={
            'concurrency'    : threads,
            'physical_cores' : threads,
            'hard_cpu_affinity': True,
            'sockets'        : 1,
            'supported'      : ['YaneuraOu-nagisa', 'YaneuraOu', 'YaneuraOu-souyuukou'],
            'syzygy_max'     : 0,
            'os_name'        : 'Linux',
            'mac_address'    : '00:00:00:00:00:00',
            'client_ver'     : OPENBENCH_CONFIG['client_version'],
            'OPENBENCH_CONFIG_CHECKSUM' : OPENBENCH_CONFIG_CHECKSUM,
        })

    class FakeRequest:
        def __init__(self):
            from django.http import QueryDict
            self.POST = QueryDict('')

    def assign(self, machine):
        from OpenBench.workloads.get_workload import get_workload
        return get_workload(self.FakeRequest(), machine)

    def test_create_tune_builds_rshogi_schema(self):

        # 成功すると index へ、失敗するとフォームへ戻される
        response = self.create_tune()
        self.assertEqual(response.status_code, 302)
        self.assertIn('/index/', response.url)

        test = Test.objects.get(test_mode='SPSA')
        spsa = test.spsa

        self.assertEqual(spsa['wrapper'], 'RSHOGI')
        self.assertEqual(spsa['total_pairs'], 51200)
        self.assertEqual(spsa['batch_pairs'], 96)
        self.assertEqual(spsa['seed'], 1)
        self.assertEqual(spsa['mapping'], 'YO')
        self.assertEqual(spsa['active_regex'], '^SPSA_')
        self.assertEqual(spsa['early_stop']['patience'], 0)

        # 原文が (改行整理だけされて) そのまま保持される
        self.assertIn('SPSA_NMP_MARGIN_OFFSET, int, -390, -780, -195, 30, 0.0020 // sign_flip',
                      spsa['params_text'])

        # 表示用ビュー
        self.assertEqual(spsa['parameters']['SPSA_LMR_BASE_QUIET']['start'], 181.0)
        self.assertTrue(spsa['parameters']['DeadParam']['not_used'])

        # rshogi が使わない設定は固定される
        self.assertEqual(test.upload_pgns, 'FALSE')
        self.assertEqual(test.win_adj, 'None')
        self.assertEqual(test.workload_size, 96)

    def test_create_tune_rejects_bad_inputs(self):

        # 7カラムでない
        response = self.create_tune(spsa_inputs='Foo, int, 1, 0, 10, 1\n')
        self.assertIn('/tune/new/', response.url)

        # SPSA で使えない持ち時間 (秒読みサイクル)
        response = self.create_tune(dev_time_control='40/60+0.6')
        self.assertIn('/tune/new/', response.url)

        # 早期停止の閾値不足
        response = self.create_tune(spsa_early_patience='5')
        self.assertIn('/tune/new/', response.url)

        # バッチペア数 > 総ペア数
        response = self.create_tune(spsa_total_pairs='10', spsa_batch_pairs='96')
        self.assertIn('/tune/new/', response.url)

        self.assertFalse(Test.objects.filter(test_mode='SPSA').exists())

    def test_assignment_and_progress_lifecycle(self):

        self.create_tune()
        test = Test.objects.get(test_mode='SPSA')
        test.approved = True
        test.save()

        machine  = self.make_machine(threads=192)
        response = self.assign(machine)
        workload = response['workload']

        # rshogi 用のペイロード
        self.assertEqual(workload['test']['type'], 'SPSA')
        self.assertEqual(workload['spsa']['wrapper'], 'RSHOGI')
        self.assertEqual(workload['spsa']['params_text'], test.spsa['params_text'])
        self.assertEqual(workload['spsa']['carry']['pairs'], 0)

        # 並列度は min(スレッド数/エンジンスレッド, 2×バッチペア数)
        self.assertEqual(workload['spsa']['concurrency'], 192)

        # SPSA は開始局面インデックスを消費しない
        test.refresh_from_db()
        self.assertEqual(test.book_index, 1)

        # --- ワーカーからの進捗報告 ---
        state = ('SPSA_LMR_BASE_QUIET,int,190.500000,90,362,14,0.002\n'
                 'SPSA_NMP_MARGIN_OFFSET,int,-400.000000,-780,-195,30,0.002\n'
                 'DeadParam,int,10.000000,0,20,1,0.002\n')
        trajectory_names = [
            'SPSA_LMR_BASE_QUIET', 'SPSA_NMP_MARGIN_OFFSET', 'DeadParam']

        response = self.client.post('/clientSubmitSpsa/', {
            'machine_id'          : machine.id,
            'secret'              : 's3cret',
            'test_id'             : test.id,
            'result_id'           : workload['result']['id'],
            'completed_pairs'     : '192',
            'completed_batches'   : '2',
            'total_games'         : '384',
            'wins'                : '150',
            'losses'              : '140',
            'draws'               : '94',
            'last_raw_result'     : '+2.000',
            'last_avg_abs_update' : '0.0125',
            'state_params'        : state,
            'trajectory_names'    : json.dumps(trajectory_names),
            'trajectory_stats'    : json.dumps([
                [1, 96, 4.0, 0.02, 1.0], [2, 96, 2.0, 0.0125, 0.8]]),
            'trajectory_values'   : json.dumps([
                [0, [180.0, -390.0, 10.0]],
                [1, [185.0, -395.0, 10.0]],
                [2, [190.5, -400.0, 10.0]]]),
            'finished'            : '0',
        }).json()

        self.assertEqual(response, { 'trajectory_batch' : 2 })

        test.refresh_from_db()
        self.assertEqual(test.games, 384)
        self.assertEqual(test.wins, 150)
        self.assertEqual(test.spsa['progress']['completed_pairs'], 192)
        self.assertEqual(test.spsa['progress']['machine_id'], machine.id)
        self.assertEqual(test.spsa['parameters']['SPSA_LMR_BASE_QUIET']['value'], 190.5)
        self.assertEqual(test.spsa['state_params'], state)
        self.assertEqual(test.spsa['trajectory']['names'], trajectory_names)
        self.assertEqual(len(test.spsa['trajectory']['stats']), 2)
        self.assertEqual([row[0] for row in test.spsa['trajectory']['values']], [0, 1, 2])

        # Result / Profile は寄与分だけ加算される
        from OpenBench.models import Result
        result = Result.objects.get(id=workload['result']['id'])
        self.assertEqual(result.games, 384)
        self.assertEqual(result.wins, 150)
        self.assertEqual(result.losses, 140)
        self.assertEqual(result.draws, 94)

        # --- 完了報告 ---
        final = state.replace('190.500000', '195')
        response = self.client.post('/clientSubmitSpsa/', {
            'machine_id'      : machine.id,
            'secret'          : 's3cret',
            'test_id'         : test.id,
            'result_id'       : workload['result']['id'],
            'completed_pairs' : '51200',
            'completed_batches' : '534',
            'total_games'     : '102400',
            'wins'            : '40000',
            'losses'          : '39000',
            'draws'           : '23400',
            'state_params'    : final,
            'finished'        : '1',
            'final_params'    : final,
        }).json()

        self.assertEqual(response, { 'trajectory_batch' : 534, 'stop' : True })

        test.refresh_from_db()
        self.assertTrue(test.finished)
        self.assertTrue(test.passed)
        self.assertEqual(test.spsa['final_params'], final)
        self.assertEqual(test.games, 102400)

    def test_spsa_workload_is_exclusive_to_one_machine(self):

        self.create_tune()
        test = Test.objects.get(test_mode='SPSA')
        test.approved = True
        test.save()

        first = self.make_machine()
        self.assertIn('workload', self.assign(first))

        # 稼働中 (updated が新しい) の間、他のマシンには配られない
        second = self.make_machine()
        self.assertEqual(self.assign(second), {})

    def test_spsa_requires_linux_worker(self):

        self.create_tune()
        test = Test.objects.get(test_mode='SPSA')
        test.approved = True
        test.save()

        machine = self.make_machine()
        machine.info['os_name'] = 'Windows'
        machine.save()
        self.assertEqual(self.assign(machine), {})

    def test_legacy_spsa_records_are_not_assigned(self):

        from OpenBench.models import Engine
        engine = Engine.objects.create(name='master', source='s', sha='a' * 40, bench=1)
        Test.objects.create(
            author='alice', dev=engine, base=engine,
            dev_engine='YaneuraOu-nagisa', base_engine='YaneuraOu-nagisa',
            dev_options='Threads=1 Hash=16', base_options='Threads=1 Hash=16',
            dev_time_control='2.0+0.02', base_time_control='2.0+0.02',
            book_name='taya36_shogi_sfen.epd',
            test_mode='SPSA', approved=True,
            spsa={ 'parameters' : {}, 'iterations' : 100, 'pairs_per' : 8,
                   'Alpha' : 0.602, 'Gamma' : 0.101, 'A' : 10,
                   'reporting_type' : 'BATCHED', 'distribution_type' : 'SINGLE' })

        machine = self.make_machine()
        self.assertEqual(self.assign(machine), {})

    def test_game_results_endpoint_rejects_spsa(self):

        self.create_tune()
        test = Test.objects.get(test_mode='SPSA')
        test.approved = True
        test.save()

        machine  = self.make_machine()
        workload = self.assign(machine)['workload']

        response = self.client.post('/clientSubmitResults/', {
            'machine_id' : machine.id,  'secret'     : 's3cret',
            'test_id'    : test.id,     'result_id'  : workload['result']['id'],
            'crashes'    : '0', 'timelosses' : '0', 'illegals' : '0',
            'trinomial'  : '1 0 1', 'pentanomial' : '0 0 1 0 0',
        }).json()

        self.assertEqual(response, { 'stop' : True })
        test.refresh_from_db()
        self.assertEqual(test.games, 0)

    def test_stopped_tune_tells_worker_to_stop(self):

        self.create_tune()
        test = Test.objects.get(test_mode='SPSA')
        test.approved = True
        test.save()

        machine  = self.make_machine()
        workload = self.assign(machine)['workload']

        # GUI から停止
        self.client.post('/tune/%d/STOP/' % (test.id))

        response = self.client.post('/clientSubmitSpsa/', {
            'machine_id' : machine.id, 'secret' : 's3cret',
            'test_id'    : test.id, 'result_id' : workload['result']['id'],
            'completed_pairs' : '10', 'total_games' : '20',
        }).json()

        self.assertEqual(response, { 'stop' : True })

    def test_workload_page_renders_rshogi_tune(self):

        self.create_tune()
        test = Test.objects.get(test_mode='SPSA')

        response = self.client.get('/tune/%d/' % (test.id))
        self.assertContains(response, 'rshogi spsa')
        self.assertContains(response, 'SPSA_LMR_BASE_QUIET')
        self.assertContains(response, '51200')

        # 進捗報告後も描画できる (現在値・進捗ブロック)
        test.approved = True
        test.save()
        machine  = self.make_machine()
        workload = self.assign(machine)['workload']

        self.client.post('/clientSubmitSpsa/', {
            'machine_id' : machine.id, 'secret' : 's3cret',
            'test_id'    : test.id, 'result_id' : workload['result']['id'],
            'completed_pairs' : '192', 'completed_batches' : '2', 'total_games' : '384',
            'wins' : '150', 'losses' : '140', 'draws' : '94',
            'last_raw_result' : '2.0', 'last_avg_abs_update' : '0.01',
            'state_params' : 'SPSA_LMR_BASE_QUIET,int,190.500000,90,362,14,0.002\n',
        })

        response = self.client.get('/tune/%d/' % (test.id))
        self.assertContains(response, '190.5000')
        self.assertContains(response, '消化 384 局')
        self.assertContains(response, 'SPSA 進行方向')
        self.assertContains(response, 'spsa-trajectory-data')
        self.assertContains(response, 'spsa_trajectory.js')

        # 一覧ページも壊れない
        response = self.client.get('/index/')
        self.assertContains(response, 'Tuning 2 Parameters (rshogi)')

    def test_create_tune_page_renders(self):

        response = self.client.get('/tune/new/')
        self.assertContains(response, 'spsa_total_pairs')
        self.assertContains(response, 'spsa_batch_pairs')
        self.assertContains(response, 'spsa_mapping')
        self.assertContains(response, 'spsa_tune_kit')
        self.assertNotContains(response, 'spsa_reporting_type')


MINI_TUNE = '''#set file engine\\search.cpp
#set declaration %%TUNE_DECLARATION%%
#set options %%TUNE_OPTIONS%%
#context futility

if (eval >= beta + 100@ * depth - 25@2)
    return eval;

#context razoring

if (eval < alpha - 500@)
    continue;
'''

MINI_SOURCE = '''
if (eval >= beta + 100 * depth - 25)
    return eval;

if (eval < alpha - 500)
    continue;

// %%TUNE_DECLARATION%%
// %%TUNE_OPTIONS%%
'''

# futility の定数がドリフトした現行ソース
MINI_SOURCE_DRIFT = MINI_SOURCE.replace('100 * depth', '120 * depth')

class TuneKitTests(TestCase):

    ## .tune キットの GUI 管理: 作成 (params 自動生成)、照合、自動追随、同期、権限

    def setUp(self):
        self.alice = User.objects.create_user('alice', 'a@example.com', 'pw-alice')
        Profile.objects.create(user=self.alice, enabled=True, approver=False)
        self.client.login(username='alice', password='pw-alice')

    def create_kit(self, **overrides):
        form = {
            'action'    : 'create',
            'engine'    : 'YaneuraOu-nagisa',
            'name'      : 'mini',
            'tune_text' : MINI_TUNE,
        }
        form.update(overrides)
        return self.client.post('/tunekits/', form)

    def test_create_generates_params(self):

        response = self.create_kit()
        self.assertEqual(response.status_code, 302)

        from OpenBench.models import TuneKit
        kit = TuneKit.objects.get(engine='YaneuraOu-nagisa', name='mini')

        # .params が .tune から自動生成される (tune.py と同じ既定レンジ)
        self.assertIn('futility_1, int, 100, 0, 200, 10, 0.002', kit.params_text)
        self.assertIn('futility_2, int, 25, 0, 50', kit.params_text)
        self.assertIn('razoring_1, int, 500, 0, 1000, 50, 0.002', kit.params_text)

        # 一覧・詳細ページが描画できる
        self.assertContains(self.client.get('/tunekits/'), 'mini')
        detail = self.client.get('/tunekits/%d/' % (kit.id))
        self.assertContains(detail, 'futility_1')
        self.assertContains(detail, 'id="kit_branch"')
        self.assertContains(detail, 'GitHubから取得中...')
        self.assertNotContains(detail, '<input id="branch"')

    def test_create_rejects_bad_tune(self):

        self.create_kit(tune_text='no directives here')
        from OpenBench.models import TuneKit
        self.assertFalse(TuneKit.objects.exists())

    def test_existing_params_are_kept_on_create(self):

        pasted = 'futility_1, int, 150, 60, 300, 12, 0.002 // 前回到達値\n'
        self.create_kit(params_text=pasted)

        from OpenBench.models import TuneKit
        kit = TuneKit.objects.get(name='mini')
        self.assertIn('futility_1, int, 150, 60, 300, 12, 0.002', kit.params_text)
        self.assertIn('前回到達値', kit.params_text)
        self.assertIn('futility_2', kit.params_text) # 足りない分は自動追加

    @patch('OpenBench.tune_kits.fetch_tune_sources')
    def test_check_action_classifies_contexts(self, mock_fetch):

        self.create_kit()
        from OpenBench.models import TuneKit
        kit = TuneKit.objects.get(name='mini')

        mock_fetch.return_value = { 'engine/search.cpp' : MINI_SOURCE_DRIFT }
        response = self.client.post('/tunekits/%d/' % (kit.id), {
            'action' : 'check', 'repo' : 'https://github.com/keinoda/YaneuraOu', 'branch' : 'master' })

        self.assertContains(response, 'NUMDRIFT')
        self.assertContains(response, '100-&gt;120')
        # マーカーも照合される
        self.assertContains(response, 'marker %%TUNE_OPTIONS%%')

    @patch('OpenBench.tune_kits.fetch_tune_sources')
    def test_retune_action_updates_tune_text(self, mock_fetch):

        self.create_kit()
        from OpenBench.models import TuneKit
        kit = TuneKit.objects.get(name='mini')

        mock_fetch.return_value = { 'engine/search.cpp' : MINI_SOURCE_DRIFT }
        self.client.post('/tunekits/%d/' % (kit.id), {
            'action' : 'retune', 'repo' : 'https://github.com/keinoda/YaneuraOu', 'branch' : 'master' })

        kit.refresh_from_db()
        self.assertIn('120@ * depth', kit.tune_text)

        # 追随後に照合すると全 EXACT
        import OpenBench.tune_kits
        results, counts = OpenBench.tune_kits.check_kit(
            kit.tune_text, kit.engine, 'https://github.com/keinoda/YaneuraOu', 'master')
        self.assertEqual(counts['NUMDRIFT'], 0)
        self.assertEqual(counts['MISSING'], 0)

    def test_sync_params_after_tune_edit(self):

        self.create_kit()
        from OpenBench.models import TuneKit
        kit = TuneKit.objects.get(name='mini')

        # razoring を .tune から外して保存 → 同期で NOT USED になる
        edited = MINI_TUNE.split('#context razoring')[0]
        self.client.post('/tunekits/%d/' % (kit.id), {
            'action' : 'save', 'tune_text' : edited, 'params_text' : kit.params_text })
        self.client.post('/tunekits/%d/' % (kit.id), { 'action' : 'sync_params' })

        kit.refresh_from_db()
        self.assertIn('razoring_1, int, 500, 0, 1000, 50, 0.002 [[NOT USED]]', kit.params_text)

    def test_edit_requires_author_or_approver(self):

        self.create_kit()
        from OpenBench.models import TuneKit
        kit = TuneKit.objects.get(name='mini')

        mallory = User.objects.create_user('mallory', 'm@example.com', 'pw-m')
        Profile.objects.create(user=mallory, enabled=True, approver=False)
        client = Client()
        client.login(username='mallory', password='pw-m')

        client.post('/tunekits/%d/' % (kit.id), {
            'action' : 'save', 'tune_text' : 'hijacked', 'params_text' : '' })
        kit.refresh_from_db()
        self.assertNotEqual(kit.tune_text, 'hijacked')

        client.post('/tunekits/%d/' % (kit.id), { 'action' : 'delete' })
        self.assertTrue(TuneKit.objects.filter(id=kit.id).exists())

        # 本人は削除できる
        self.client.post('/tunekits/%d/' % (kit.id), { 'action' : 'delete' })
        self.assertFalse(TuneKit.objects.filter(id=kit.id).exists())

class SpsaTuneKitIntegrationTests(SpsaRshogiLifecycleTests):

    ## SPSA 作成 ⇔ .tune キットの統合: キット選択で TUNE ビルドがワークロードに
    ## 載ること、作成時の自動照合ゲート、名前integrityの検査

    KIT_PARAMS = ('futility_1, int, 100, 0, 200, 10, 0.002\n'
                  'futility_2, int, 25, 0, 50, 2.5, 0.002\n'
                  'razoring_1, int, 500, 0, 1000, 50, 0.002\n')

    def setUp(self):
        super().setUp()
        from OpenBench.models import TuneKit
        self.kit = TuneKit.objects.create(
            engine='YaneuraOu-nagisa', name='mini', author='alice',
            tune_text=MINI_TUNE, params_text=self.KIT_PARAMS)

    def kit_form(self, **overrides):
        form = self.tune_form(
            spsa_tune_kit=str(self.kit.id),
            spsa_inputs=self.KIT_PARAMS,
            spsa_active_regex='',
            spsa_mapping='NONE')
        form.update(overrides)
        return form

    def create_kit_tune(self, sources=None, **overrides):
        with patch('OpenBench.tune_kits.fetch_tune_sources',
                   return_value=(sources if sources is not None else { 'engine/search.cpp' : MINI_SOURCE })), \
             patch('OpenBench.workloads.verify_workload.collect_github_info',
                   return_value=(self.github_info(), True)):
            return self.client.post('/tune/new/', self.kit_form(**overrides))

    def test_kit_tune_carries_tune_build_payload(self):

        response = self.create_kit_tune()
        self.assertIn('/index/', response.url)

        test = Test.objects.get(test_mode='SPSA')
        snapshot = test.spsa['tune_kit']
        self.assertEqual(snapshot['name'], 'mini')
        self.assertEqual(snapshot['sha'], self.kit.content_sha())
        self.assertIn('futility', snapshot['tune_text'])

        # ワークロードの dev/base に TUNE ビルド指示が載る
        test.approved = True
        test.save()
        machine  = self.make_machine()
        workload = self.assign(machine)['workload']

        self.assertEqual(workload['test']['dev']['tune']['name'], 'mini')
        self.assertEqual(workload['test']['dev']['tune']['sha'], self.kit.content_sha())
        self.assertIn('100@', workload['test']['dev']['tune']['tune_text'])
        self.assertEqual(workload['test']['base']['tune'], workload['test']['dev']['tune'])

        # キット表示がチューニングページに出る
        self.assertContains(self.client.get('/tune/%d/' % (test.id)), '.tuneキット')

    def test_creation_blocked_when_kit_drifted(self):

        response = self.create_kit_tune(sources={ 'engine/search.cpp' : MINI_SOURCE_DRIFT })
        self.assertIn('/tune/new/', response.url)
        self.assertFalse(Test.objects.filter(test_mode='SPSA').exists())

    def test_creation_blocked_when_markers_missing(self):

        # 上流の素の YaneuraOu (マーカー無し) を指してしまったケース
        response = self.create_kit_tune(sources={ 'engine/search.cpp' :
            MINI_SOURCE.replace('%%TUNE_DECLARATION%%', '').replace('%%TUNE_OPTIONS%%', '') })
        self.assertIn('/tune/new/', response.url)

    def test_creation_blocked_on_param_name_mismatch(self):

        # .tune のパラメータが SPSA 入力に無い (黙って残すと死にパラメータになる)
        dropped = self.KIT_PARAMS.replace('razoring_1, int, 500, 0, 1000, 50, 0.002\n', '')
        response = self.create_kit_tune(spsa_inputs=dropped)
        self.assertIn('/tune/new/', response.url)

        # [[NOT USED]] で明示的に外すのは許される
        excluded = self.KIT_PARAMS.replace(
            'razoring_1, int, 500, 0, 1000, 50, 0.002',
            'razoring_1, int, 500, 0, 1000, 50, 0.002 [[NOT USED]]')
        response = self.create_kit_tune(spsa_inputs=excluded)
        self.assertIn('/index/', response.url)

    def test_creation_blocked_with_yo_mapping(self):

        response = self.create_kit_tune(spsa_mapping='YO')
        self.assertIn('/tune/new/', response.url)

    def test_creation_blocked_when_range_widened_inline(self):

        # TUNE ビルドの USI レンジはキットの .params から作られるので、
        # SPSA 入力側でレンジを広げると setoption が弾かれる → 作成時に拒否
        widened = self.KIT_PARAMS.replace(
            'futility_1, int, 100, 0, 200, 10, 0.002',
            'futility_1, int, 100, 0, 400, 10, 0.002')
        response = self.create_kit_tune(spsa_inputs=widened)
        self.assertIn('/tune/new/', response.url)

        # 狭める分には問題ない
        narrowed = self.KIT_PARAMS.replace(
            'futility_1, int, 100, 0, 200, 10, 0.002',
            'futility_1, int, 100, 50, 150, 10, 0.002')
        response = self.create_kit_tune(spsa_inputs=narrowed)
        self.assertIn('/index/', response.url)

    def test_kit_engine_must_match(self):

        response = self.create_kit_tune(dev_engine='YaneuraOu')
        self.assertIn('/tune/new/', response.url)
