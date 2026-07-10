#!/bin/python3

# ワーカーの「二重起動防止」と「ワーカーキー失効時の自己終了」のテスト。
#
# - 同じディレクトリで2つ目のワーカーが起動したら exit 65 (再起動しない)
# - サーバが認証拒否 (キーの無効化/削除) を返したら exit 66 (再起動しない)
# setup_worker.sh のループはこれらの終了コードを見て停止する

import importlib
import os
import sys
import tempfile
import types
import unittest

PARENT     = os.path.join(os.path.dirname(__file__), os.path.pardir)
CLIENT_DIR = os.path.abspath(os.path.join(PARENT, 'Client'))


def import_worker():
    sys.path.insert(0, CLIENT_DIR)

    cpuinfo = types.ModuleType('cpuinfo')
    cpuinfo.get_cpu_info = lambda: {'brand_raw': 'test cpu'}
    sys.modules.setdefault('cpuinfo', cpuinfo)

    psutil = types.ModuleType('psutil')
    psutil.cpu_count = lambda logical=True: 2
    psutil.virtual_memory = lambda: types.SimpleNamespace(total=8 * 1024 * 1024 * 1024)
    sys.modules.setdefault('psutil', psutil)

    return importlib.import_module('worker')


class ShutdownOnRevocationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.worker = import_worker()

    def test_structured_shutdown_flag_exits_66(self):

        # サーバがキー失効を構造化フラグで通知したときだけ終了する
        with self.assertRaises(SystemExit) as ctx:
            self.worker.shutdown_if_revoked({ 'error' : 'Bad Credentials', 'shutdown' : True })
        self.assertEqual(ctx.exception.code, self.worker.EXIT_SHUTDOWN)

    def test_plain_bad_credentials_does_not_exit(self):

        # フラグの無い 'Bad Credentials' は可逆的な失敗 (アカウントの一時
        # 無効化、パスワード変更など) かもしれないので、恒久停止しない
        self.assertIsNone(self.worker.shutdown_if_revoked('Bad Credentials'))
        self.assertIsNone(self.worker.shutdown_if_revoked({ 'error' : 'Bad Credentials' }))

    def test_legacy_revoked_key_error_exits_66(self):

        # 旧サーバ (フラグ未対応) 互換: get_workload.SHUTDOWN_ERROR の文言だけは見る
        with self.assertRaises(SystemExit) as ctx:
            self.worker.shutdown_if_revoked('Worker key disabled or deleted. Shut down.')
        self.assertEqual(ctx.exception.code, 66)

    def test_other_errors_do_not_exit(self):

        # サーバ設定変更などの一時的なエラーは従来どおりの再初期化に任せる
        self.assertIsNone(self.worker.shutdown_if_revoked('Server Configuration Changed'))
        self.assertIsNone(self.worker.shutdown_if_revoked('No such Workload'))

    def test_shutdown_stops_detached_spsa_first(self):

        # 恒久停止の前に detached の spsa を止める (孤児チューナー防止)
        import spsa_rshogi
        calls    = []
        original = spsa_rshogi.stop_all_running_spsa
        spsa_rshogi.stop_all_running_spsa = lambda: calls.append(True) or 0
        try:
            with self.assertRaises(SystemExit):
                self.worker.shutdown_if_revoked({ 'error' : 'x', 'shutdown' : True })
            self.assertEqual(calls, [True])
        finally:
            spsa_rshogi.stop_all_running_spsa = original

    def test_workload_request_with_revoked_key_exits(self):

        # サーバが {'error': ..., 'shutdown': True} を返すケースの結合確認
        config = types.SimpleNamespace(
            machine_id=1, secret_token='s', blacklist=[], server='http://example',
            workload=None)

        class FakeResponse:
            def json(self):
                return { 'error' : 'Worker key disabled or deleted. Shut down.',
                         'shutdown' : True }

        original = self.worker.requests.post
        self.worker.requests.post = lambda *a, **k: FakeResponse()
        try:
            with self.assertRaises(SystemExit) as ctx:
                self.worker.server_request_workload(config)
            self.assertEqual(ctx.exception.code, 66)
        finally:
            self.worker.requests.post = original

    def test_match_runner_configure_with_revoked_key_exits(self):

        # 起動順で最初の認証付き通信 (fastchess/shogitest の configure) でも
        # キー失効なら try_forever に握られる前に exit 66 する
        config = types.SimpleNamespace(
            server='http://example', username='u', password='p')

        class FakeResponse:
            def json(self):
                return { 'error' : 'Bad Credentials', 'shutdown' : True }

        original = self.worker.requests.post
        self.worker.requests.post = lambda *a, **k: FakeResponse()
        try:
            with self.assertRaises(SystemExit) as ctx:
                self.worker.server_configure_match_runner(config, 'fastchess', None)
            self.assertEqual(ctx.exception.code, 66)
        finally:
            self.worker.requests.post = original


class VersionGateTests(unittest.TestCase):

    ## サーバとクライアントの client_version が食い違うときの登録前ゲート。
    ## サーバが古い間は「登録せず待つ」(以前はここで登録→拒否→再登録が
    ## 数秒周期で無限ループし、Machine 行が際限なく増えた)

    @classmethod
    def setUpClass(cls):
        cls.worker = import_worker()

    def make_config(self):
        return types.SimpleNamespace(
            server='http://example', username='u', password='p')

    def with_server_version(self, version, body):
        class FakeResponse:
            def json(self):
                return { 'client_version' : version }
        original_post  = self.worker.requests.post
        original_sleep = self.worker.time.sleep
        self.worker.requests.post = lambda *a, **k: FakeResponse()
        try:
            return body()
        finally:
            self.worker.requests.post  = original_post
            self.worker.time.sleep     = original_sleep

    def test_matching_version_proceeds(self):
        result = self.with_server_version(
            self.worker.CLIENT_VERSION,
            lambda: self.worker.wait_for_server_version(self.make_config()))
        self.assertIsNone(result)

    def test_newer_server_triggers_client_update(self):
        from client import BadVersionException
        def body():
            with self.assertRaises(BadVersionException):
                self.worker.wait_for_server_version(self.make_config())
        self.with_server_version(self.worker.CLIENT_VERSION + 1, body)

    def test_older_server_waits_without_registering(self):

        # サーバのデプロイ待ち: 登録もクライアント更新もせず、待機を続ける
        class Waited(Exception):
            pass
        def fake_sleep(seconds):
            raise Waited()
        def body():
            self.worker.time.sleep = fake_sleep
            with self.assertRaises(Waited):
                self.worker.wait_for_server_version(self.make_config())
        self.with_server_version(self.worker.CLIENT_VERSION - 1, body)

    def test_network_failure_skips_the_gate(self):

        # 照合できないだけなら従来フローに任せる (起動を止めない)
        def boom(*a, **k):
            raise OSError('no route to host')
        original = self.worker.requests.post
        self.worker.requests.post = boom
        try:
            self.assertIsNone(self.worker.wait_for_server_version(self.make_config()))
        finally:
            self.worker.requests.post = original

    def test_older_server_eventually_refreshes_client(self):

        # サーバがいつまでも古い = ロールバックされた可能性。10分相当
        # 待ったら BadVersionException でクライアント側を合わせにいく
        # (以前はここで永久に待ち続け、ロールバック時に全ワーカーが沈黙した)
        from client import BadVersionException
        def body():
            self.worker.time.sleep = lambda seconds: None
            with self.assertRaises(BadVersionException):
                self.worker.wait_for_server_version(self.make_config())
        self.with_server_version(self.worker.CLIENT_VERSION - 1, body)

    def test_non_dict_response_skips_the_gate(self):

        # プロキシ等が dict 以外の JSON を 200 で返してもクラッシュしない
        class FakeResponse:
            def json(self):
                return 'maintenance'
        original = self.worker.requests.post
        self.worker.requests.post = lambda *a, **k: FakeResponse()
        try:
            self.assertIsNone(self.worker.wait_for_server_version(self.make_config()))
        finally:
            self.worker.requests.post = original

    def test_non_numeric_version_skips_the_gate(self):

        # client_version が数値でない応答もフェイルオープンで通常フローへ
        class FakeResponse:
            def json(self):
                return { 'client_version' : 'not-a-number' }
        original = self.worker.requests.post
        self.worker.requests.post = lambda *a, **k: FakeResponse()
        try:
            self.assertIsNone(self.worker.wait_for_server_version(self.make_config()))
        finally:
            self.worker.requests.post = original


class MachineTokenTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.worker = import_worker()

    def test_token_is_stable_and_recreated_when_corrupt(self):

        with tempfile.TemporaryDirectory() as workdir:
            cwd = os.getcwd()
            os.chdir(workdir)
            try:
                token = self.worker.load_machine_token()
                self.assertRegex(token, r'^[0-9a-f]{32}$')

                # 再起動しても同じトークン (= 同じ Machine 行に再登録される)
                self.assertEqual(self.worker.load_machine_token(), token)

                # 壊れたファイルは作り直す
                with open('.machine_token', 'w') as fout:
                    fout.write('garbage!!')
                fresh = self.worker.load_machine_token()
                self.assertRegex(fresh, r'^[0-9a-f]{32}$')
                self.assertNotEqual(fresh, 'garbage!!')
            finally:
                os.chdir(cwd)


class SingleInstanceLockTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.worker = import_worker()

    def test_second_acquire_exits_65(self):

        if self.worker.IS_WINDOWS:
            self.skipTest('flock is Linux-only')

        with tempfile.TemporaryDirectory() as workdir:
            cwd = os.getcwd()
            os.chdir(workdir)
            try:
                self.worker.acquire_single_instance_lock()
                first_lock = self.worker.WORKER_LOCK
                self.assertIsNotNone(first_lock)

                # 2つ目 (別の open file description) はロックを取れず 65 で終了
                with self.assertRaises(SystemExit) as ctx:
                    self.worker.acquire_single_instance_lock()
                self.assertEqual(ctx.exception.code, self.worker.EXIT_DUPLICATE)

                # 1つ目を手放せば取り直せる (ワーカー再起動の想定)
                first_lock.close()
                self.worker.acquire_single_instance_lock()
                self.assertIsNotNone(self.worker.WORKER_LOCK)
            finally:
                if self.worker.WORKER_LOCK:
                    self.worker.WORKER_LOCK.close()
                    self.worker.WORKER_LOCK = None
                os.chdir(cwd)

    def test_environment_lock_failure_proceeds_without_lock(self):

        # flock が使えない環境 (NFS の ENOLCK 等) を「多重起動」と誤判定して
        # exit 65 (ラッパーごと恒久停止) しない。ロック無しで続行する
        if self.worker.IS_WINDOWS:
            self.skipTest('flock is Linux-only')

        import errno
        import fcntl

        def no_lock_support(*args, **kwargs):
            raise OSError(errno.ENOLCK, 'No locks available')

        original = fcntl.flock
        fcntl.flock = no_lock_support

        with tempfile.TemporaryDirectory() as workdir:
            cwd = os.getcwd()
            os.chdir(workdir)
            try:
                self.assertIsNone(self.worker.acquire_single_instance_lock())
                self.assertIsNone(self.worker.WORKER_LOCK)
            finally:
                fcntl.flock = original
                os.chdir(cwd)


if __name__ == '__main__':
    unittest.main()
