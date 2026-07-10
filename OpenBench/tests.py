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

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

import hashlib
import os
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from OpenBench.models import BuildVariant, Engine, Network, NetworkAuxFile, Profile, Test, WorkerKey
from OpenBench.templatetags.mytags import longStatBlock
from OpenBench.views import engine_build_variants, normalize_build_command, parse_ssh_target
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
        self.assertEqual(get_workload(None, machine), { 'error' : SHUTDOWN_ERROR })

        # Deleting it likewise
        self.key.delete()
        self.assertTrue(machine_key_revoked(machine))
        self.assertEqual(get_workload(None, machine), { 'error' : SHUTDOWN_ERROR })

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
        mock_get.return_value = self.FakeResponse(200, {
            'commit' : {
                'sha'    : 'a' * 40,
                'commit' : {
                    'message' : 'bench not required',
                    'tree'    : { 'sha' : 'b' * 40 },
                },
            },
        })

        errors = []
        info, has_all = collect_github_info(errors, self.FakeRequest(), 'dev')

        self.assertEqual(errors, [])
        self.assertTrue(has_all)
        self.assertEqual(info[1], 'suisho11-tuned')
        self.assertEqual(mock_get.call_args.kwargs['headers'], {
            'Authorization' : 'Bearer test-token',
        })

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
        self.assertIn('新探索 vs 旧探索', block)
        self.assertIn('Score for: 新探索', block)

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

        self.assertTrue(block.startswith('```text\nSuisho11 vs fuuppi-v3'))
        self.assertTrue(block.endswith('\n```'))
        self.assertIn('Score for: Suisho11', block)
        self.assertIn('STRONGER : fuuppi-v3 (+65.92 Elo)', block)
        self.assertIn('SPRT     : 10.0+0.10s, Threads=4, Hash=256MB', block)
        self.assertIn('Book     : yaneuraou2025_ply24_shogi_sfen.epd', block)
        self.assertIn('Games    : N=48 W=18 L=27 D=3', block)
        self.assertNotIn('Penta |', block)
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
        self.assertEqual(upload_command, 'cat > /tmp/shogibench_setup.sh')
        upload_stdin.write.assert_called_once()
        self.assertIsInstance(upload_stdin.write.call_args.args[0], bytes)
        upload_stdin.flush.assert_called_once()
        upload_stdin.channel.shutdown_write.assert_called_once()

        launch_command = connection.exec_command.call_args_list[1].args[0]
        self.assertIn(self.key.token, launch_command)
        self.assertIn('SHOGIBENCH_PROTECTED_PIDS="$$ $PPID"', launch_command)
        self.assertIn('nohup /tmp/shogibench_setup.sh', launch_command)

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
            'finished'            : '0',
        }).json()

        self.assertEqual(response, {})

        test.refresh_from_db()
        self.assertEqual(test.games, 384)
        self.assertEqual(test.wins, 150)
        self.assertEqual(test.spsa['progress']['completed_pairs'], 192)
        self.assertEqual(test.spsa['progress']['machine_id'], machine.id)
        self.assertEqual(test.spsa['parameters']['SPSA_LMR_BASE_QUIET']['value'], 190.5)
        self.assertEqual(test.spsa['state_params'], state)

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

        self.assertEqual(response, { 'stop' : True })

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
        self.assertContains(self.client.get('/tunekits/%d/' % (kit.id)), 'futility_1')

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
