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

from django.contrib.auth.models import User
from django.test import Client, TestCase

from OpenBench.models import Profile, WorkerKey

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
