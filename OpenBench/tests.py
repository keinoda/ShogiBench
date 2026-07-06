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
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from OpenBench.models import BuildVariant, Network, Profile, SSHCredential, WorkerKey
from OpenBench.views import engine_build_variants, normalize_build_command, parse_ssh_target

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

    def test_upload_with_aux_file(self):
        content = b'\x10\x20' * 50_000
        aux     = b'\x30\x40' * 25_000
        aux_sha = hashlib.sha256(aux).hexdigest()[:8].upper()

        with override_settings(MEDIA_ROOT=self.media), \
             patch('OpenBench.utils.MEDIA_ROOT', self.media):
            self.client.post('/networks/YaneuraOu-nagisa/UPLOAD/withaux.bin/', {
                'netfile' : SimpleUploadedFile('nn.bin', content),
                'auxfile' : SimpleUploadedFile('progress.bin', aux) })

            network = Network.objects.filter(engine='YaneuraOu-nagisa', name='withaux.bin').first()
            self.assertIsNotNone(network)
            self.assertEqual(network.aux_sha256, aux_sha)

            # The aux file is retrievable through the api endpoint
            response = self.client.post('/api/networks/YaneuraOu-nagisa/%s/aux/' % (network.sha256))
            body = b''.join(response.streaming_content)
            self.assertEqual(body, aux)

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
            response = self.client.post('/api/networks/YaneuraOu-nagisa/%s/aux/' % (network.sha256))
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
        response = self.client.get('/builds/')
        self.assertContains(response, 'mine')
        self.assertContains(response, 'NNUE-KP256')

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

    def test_page_creates_and_shows_ssh_public_key(self):
        response = self.client.get('/workers/')
        credential = SSHCredential.objects.get(user=self.user)
        self.assertIn('ssh-rsa ', credential.public_key)
        self.assertContains(response, credential.public_key)

        # Keypair is generated once, then reused
        again = self.client.get('/workers/')
        self.assertEqual(SSHCredential.objects.filter(user=self.user).count(), 1)

    @patch('OpenBench.views.paramiko.SSHClient')
    def test_connect_launches_worker(self, mock_ssh_client):
        self.client.get('/workers/')  # generate the keypair

        connection = mock_ssh_client.return_value
        stdout = MagicMock(); stdout.readline.return_value = 'LAUNCHED\n'
        connection.exec_command.return_value = (MagicMock(), stdout, MagicMock())

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
        self.assertIn('shogibench_setup.sh', command)

    @override_settings(PUBLIC_URL='https://bench.example.com')
    @patch('OpenBench.views.paramiko.SSHClient')
    def test_connect_uses_configured_public_url(self, mock_ssh_client):
        self.client.get('/workers/')

        connection = mock_ssh_client.return_value
        stdout = MagicMock(); stdout.readline.return_value = 'LAUNCHED\n'
        connection.exec_command.return_value = (MagicMock(), stdout, MagicMock())

        self.client.post('/workers/connect/', {
            'ssh_target' : 'ssh4.vast.ai:12345',
            'key_id'     : self.key.id,
        })

        command = connection.exec_command.call_args.args[0]
        self.assertIn('OPENBENCH_SERVER=https://bench.example.com/', command)

    @patch('OpenBench.views.paramiko.SSHClient')
    def test_connect_failure_reports_error(self, mock_ssh_client):
        self.client.get('/workers/')

        mock_ssh_client.return_value.connect.side_effect = Exception('Connection refused')

        response = self.client.post('/workers/connect/', {
            'ssh_target' : 'ssh4.vast.ai:12345',
            'key_id'     : self.key.id,
        }, follow=True)

        self.assertContains(response, 'SSH connection failed')

    def test_connect_requires_enabled_key(self):
        self.key.enabled = False
        self.key.save()
        response = self.client.post('/workers/connect/', {
            'ssh_target' : 'ssh4.vast.ai:12345',
            'key_id'     : self.key.id,
        }, follow=True)
        self.assertContains(response, 'Select an enabled Worker Key')
