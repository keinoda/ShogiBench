#!/bin/python3

# setup_worker.sh の「前回の起動ループの掃除 (stop_previous_workers)」を
# 実プロセスで検証する。3種類の旧ワーカーを模擬して、再実行が全部を
# 止められることを確認する:
#
#   A. スクリプト名で見えるループ (nohup shogibench_setup.sh 起動)
#   B. `curl | bash` 起動でプロセス名が bash になった不可視ループ
#      (client.py の親を辿って止める。過去に「リトライのたびに積み上がり、
#       成功した瞬間に全部起動する」事故を起こした形)
#   C. pgid ファイルに記録された近代版 (プロセスグループごと止める)

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir))
SCRIPT = os.path.join(PARENT, 'Deploy', 'worker', 'setup_worker.sh')


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def sandbox_can_group_kill():

    ## この環境で「兄弟プロセスツリーのバックグラウンド子」へシグナルが届くかを
    ## 実測する。一部のサンドボックス (CI 等) は別ツリーへの kill(-pgid) や
    ## pkill -g を制限するため、その場合は孤児掃除のアサーションだけスキップする
    ## (本番の素の Linux では常に届く)

    sleeper = '"%s" -c "import time; time.sleep(60)"' % (sys.executable)
    proc = subprocess.Popen(['bash', '-c', '%s & wait' % (sleeper)],
                            start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    pgid = os.getpgid(proc.pid)

    subprocess.run(['bash', '-c',
                    'kill -TERM -- "-%d" 2>/dev/null; pkill -TERM -g %d 2>/dev/null; true'
                    % (pgid, pgid)], start_new_session=True)
    time.sleep(1.0)
    proc.poll()

    members = subprocess.run(['pgrep', '-g', str(pgid)],
                             capture_output=True, text=True).stdout.split()

    try: os.killpg(pgid, signal.SIGKILL)
    except OSError: pass
    for pid in members:
        try: os.kill(int(pid), signal.SIGKILL)
        except OSError: pass

    return members == []


CAN_GROUP_KILL = None

def can_group_kill():
    global CAN_GROUP_KILL
    if CAN_GROUP_KILL is None:
        CAN_GROUP_KILL = sandbox_can_group_kill()
    return CAN_GROUP_KILL


@unittest.skipUnless(shutil.which('pgrep') and shutil.which('bash'),
                     'requires bash and procps')
class SetupWorkerTakeoverTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='shogibench-takeover-')
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError: pass
            proc.poll()
        subprocess.run(['pkill', '-KILL', '-f', self.tmp],
                       capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, argv):
        # 模擬プロセスは自分のプロセスグループで起動する (テスト本体を巻き込まない)
        proc = subprocess.Popen(
            argv, cwd=self.tmp, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def sleeper(self, seconds=300):
        # 子プロセスは python 製にする (このテスト環境のサンドボックスは
        # sleep コマンドを特別扱いしていて TERM の挙動が実環境と異なる)
        return '"%s" -c "import time; time.sleep(%d)"' % (sys.executable, seconds)

    def run_takeover(self):

        # 本物のスクリプトを SOURCE_ONLY で読み込み、掃除関数だけを実行する
        harness = (
            'export SHOGIBENCH_SOURCE_ONLY=1\n'
            'source "%s"\n'
            'stop_previous_workers\n'
        ) % (SCRIPT)

        # 掃除の実行も独立セッションで (万一の誤爆がテスト本体へ波及しないように)
        return subprocess.run(
            ['bash', '-c', harness], env={ **os.environ, 'HOME' : self.tmp },
            capture_output=True, text=True, timeout=60, start_new_session=True)

    def wait_dead(self, procs, timeout=20):
        # 自分の子プロセスは kill 後もゾンビとして os.kill(pid, 0) に応答する
        # ため、poll() (= 回収) で判定する
        deadline = time.time() + timeout
        while time.time() < deadline:
            if all(proc.poll() is not None for proc in procs):
                return True
            time.sleep(0.25)
        return False

    def assert_no_group_leftovers(self, pgid, message):
        # 孤児 (グループの生き残り) の検査は、別ツリーへのグループシグナルが
        # 届く環境でのみ行う (本番 Linux では常に届く)
        if not can_group_kill():
            return
        time.sleep(0.5)
        leftovers = subprocess.run(
            ['pgrep', '-g', str(pgid)], capture_output=True, text=True).stdout.strip()
        self.assertEqual(leftovers, '', message)

    def test_named_script_loop_is_stopped_with_children(self):

        # A: 旧ワーカー = shogibench_setup.sh という名前のループ + 子プロセス
        fake = os.path.join(self.tmp, 'shogibench_setup.sh')
        with open(fake, 'w') as fout:
            fout.write('#!/bin/bash\n%s &\nwait\n' % (self.sleeper()))
        os.chmod(fake, 0o755)

        proc = self.spawn(['bash', fake])
        time.sleep(0.5)
        self.assertTrue(pid_alive(proc.pid))

        result = self.run_takeover()
        self.assertEqual(result.returncode, 0, result.stderr)

        # 肝心なのは「再起動ループが止まる」こと (これが多重起動の原因だった)
        self.assertTrue(self.wait_dead([proc]), 'named loop survived takeover')
        self.assert_no_group_leftovers(proc.pid, 'children of the old loop survived')

    def test_invisible_pipe_bash_loop_is_stopped(self):

        # B: `curl | bash` 相当 — cmdline が 'bash -c ...' で、スクリプト名では
        # 見つけられない再起動ループ。client.py の親として発見・停止される
        client = os.path.join(self.tmp, 'client.py')
        with open(client, 'w') as fout:
            fout.write('import time\ntime.sleep(300)\n')

        loop = 'while true; do %s "%s"; sleep 1; done' % (sys.executable, client)
        proc = self.spawn(['bash', '-c', loop])
        time.sleep(1.0)
        self.assertTrue(pid_alive(proc.pid))

        result = self.run_takeover()
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertTrue(self.wait_dead([proc]), 'invisible bash loop survived takeover')

        # client.py 自体も残っていない (名前ベースの掃除はどの環境でも届く)
        time.sleep(0.5)
        survivors = subprocess.run(
            ['pgrep', '-f', '[c]lient.py'], capture_output=True, text=True).stdout.strip()
        self.assertEqual(survivors, '', 'client.py survived takeover')

    def test_recorded_process_group_is_stopped(self):

        # C: 近代版 — pgid ファイルに記録されたプロセスグループを丸ごと止める
        proc = self.spawn(['bash', '-c', '%s & %s & wait' % (self.sleeper(), self.sleeper())])
        time.sleep(0.5)

        with open(os.path.join(self.tmp, '.shogibench-worker.pgid'), 'w') as fout:
            fout.write(str(os.getpgid(proc.pid)))

        result = self.run_takeover()
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertTrue(self.wait_dead([proc]), 'recorded process group survived takeover')
        self.assert_no_group_leftovers(proc.pid, 'group members survived takeover')

    def test_takeover_is_idempotent_with_nothing_running(self):

        result = self.run_takeover()
        self.assertEqual(result.returncode, 0, result.stderr)

    def neutral_python(self):

        # 「無関係なプロセス」役は、パスに shogibench を含まないインタプリタで
        # 起動する (テスト実行環境の venv パスには ShogiBench が含まれ得るため)
        for path in ['/usr/bin/python3', '/bin/python3']:
            if os.path.exists(path):
                return path
        self.skipTest('no system python3 outside the venv')

    def test_unrelated_client_py_survives_takeover(self):

        # 無関係なプロジェクトのたまたま同名の client.py (cmdline にも cwd にも
        # shogibench を含まない) は巻き込まない
        python = self.neutral_python()
        other  = tempfile.mkdtemp(prefix='otherproj-')
        try:
            client = os.path.join(other, 'client.py')
            with open(client, 'w') as fout:
                fout.write('import time\ntime.sleep(300)\n')

            proc = subprocess.Popen(
                [python, client], cwd=other, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.procs.append(proc)
            time.sleep(0.5)
            self.assertTrue(pid_alive(proc.pid))

            result = self.run_takeover()
            self.assertEqual(result.returncode, 0, result.stderr)

            time.sleep(1.0)
            self.assertIsNone(proc.poll(), 'unrelated client.py was killed by takeover')
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_stale_pidfile_of_recycled_pgid_is_ignored(self):

        # 再起動後の pid 再利用を模擬: pidfile が無関係なプロセス群を指して
        # いたら、殺さずに stale な記録として捨てる。グループ内にたまたま
        # client.py という名前のスクリプトがいても worker とは見なさない
        python = self.neutral_python()
        other  = tempfile.mkdtemp(prefix='otherproj-')
        try:
            with open(os.path.join(other, 'client.py'), 'w') as fout:
                fout.write('import time\ntime.sleep(300)\n')

            proc = subprocess.Popen(
                ['bash', '-c', '"%s" client.py & wait' % (python)],
                cwd=other, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.procs.append(proc)
            time.sleep(0.5)

            pidfile = os.path.join(self.tmp, '.shogibench-worker.pgid')
            with open(pidfile, 'w') as fout:
                fout.write(str(os.getpgid(proc.pid)))

            result = self.run_takeover()
            self.assertEqual(result.returncode, 0, result.stderr)

            time.sleep(1.0)
            self.assertIsNone(proc.poll(), 'unrelated process group was killed via stale pidfile')
            self.assertFalse(os.path.exists(pidfile), 'stale pidfile should be discarded')
        finally:
            shutil.rmtree(other, ignore_errors=True)


def run_sourced(home, body, extra_env=None, timeout=30):

    ## 本物のスクリプトを SOURCE_ONLY で読み込んでから body を実行する。
    ## HOME を差し替えて PIDFILE / BOOTLOCK をテスト用ディレクトリに向ける
    harness = 'export SHOGIBENCH_SOURCE_ONLY=1\nsource "%s"\n%s' % (SCRIPT, body)
    env = { **os.environ, 'HOME' : home, **(extra_env or {}) }
    return subprocess.run(['bash', '-c', harness], env=env,
                          capture_output=True, text=True, timeout=timeout,
                          start_new_session=True)


@unittest.skipUnless(shutil.which('flock') and shutil.which('bash'),
                     'requires bash and flock')
class BootstrapStartupLockTests(unittest.TestCase):

    # 接続 POST が同時に2つ届いて bootstrap が並走し、互いを「前回の起動」と
    # 見なして殺し合った事故 (両方が0バイトログのまま死ぬ) の再発防止を検証する

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='shogibench-bootlock-')
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError: pass
            proc.poll()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start_holder(self, body, marker):

        ## acquire までを実行した bootstrap 役を起動し、marker の出力まで待つ
        harness = 'export SHOGIBENCH_SOURCE_ONLY=1\nsource "%s"\n%s' % (SCRIPT, body)
        proc = subprocess.Popen(
            ['bash', '-c', harness], env={ **os.environ, 'HOME' : self.tmp },
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        self.procs.append(proc)
        line = proc.stdout.readline()
        self.assertIn(marker, line)
        return proc

    def sleeper(self, seconds):
        return '"%s" -c "import time; time.sleep(%d)"' % (sys.executable, seconds)

    def test_second_bootstrap_exits_while_first_holds_the_lock(self):

        # 1つ目がロック保持中に来た2つ目は、掃除に入らず「起動中」として即終了
        self.start_holder('acquire_boot_lock\necho HOLDING\n%s\n' % (self.sleeper(20)),
                          'HOLDING')

        second = run_sourced(self.tmp, 'acquire_boot_lock\necho WON\n')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn('WON', second.stdout)
        self.assertIn('another bootstrap is starting', second.stdout)

    def test_lock_is_released_for_later_takeover(self):

        # 起動処理を終えてロックを手放した後は、稼働中でも再接続 takeover を通す
        self.start_holder(
            'acquire_boot_lock\nrelease_boot_lock\necho RELEASED\n%s\n'
            % (self.sleeper(20)), 'RELEASED')

        second = run_sourced(self.tmp, 'acquire_boot_lock\necho WON\n')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('WON', second.stdout)

    def test_lock_dies_with_its_holder(self):

        # 保持者が死ねば flock は自動解放され、stale ロックで詰まらない
        first = run_sourced(self.tmp, 'acquire_boot_lock\necho WON\n')
        self.assertIn('WON', first.stdout)

        second = run_sourced(self.tmp, 'acquire_boot_lock\necho WON\n')
        self.assertIn('WON', second.stdout)


@unittest.skipUnless(shutil.which('pgrep') and shutil.which('bash'),
                     'requires bash and procps')
class TakeoverRobustnessTests(unittest.TestCase):

    # 本番の掃除は `set -euo pipefail` 下で走る。pgrep で拾った pid がその直後に
    # 消えると ps が失敗するが、それで bootstrap 全体が silent に死んではいけない
    # (二重POST事故で双方が0バイトログのまま死んだ主因)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='shogibench-robust-')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dead_pid(self):
        proc = subprocess.Popen([sys.executable, '-c', 'pass'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait()
        return proc.pid

    def test_kill_group_or_pid_survives_a_vanished_pid(self):

        result = run_sourced(self.tmp, (
            'set -euo pipefail\n'
            'PROTECTED_PIDS=""\n'
            'kill_group_or_pid %d\n'
            'echo SURVIVED\n'
        ) % (self.dead_pid()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SURVIVED', result.stdout)

    def test_stop_previous_workers_survives_dead_pidfile_and_protected_pids(self):

        # pidfile の pgid も SHOGIBENCH_PROTECTED_PIDS の pid も既に消えている
        # (SSH ランチャーは起動直後に消えるのが常) 状態でも掃除は完走する
        with open(os.path.join(self.tmp, '.shogibench-worker.pgid'), 'w') as fout:
            fout.write(str(self.dead_pid()))

        result = run_sourced(self.tmp, (
            'set -euo pipefail\n'
            'stop_previous_workers\n'
            'echo SURVIVED\n'
        ), extra_env={ 'SHOGIBENCH_PROTECTED_PIDS' : str(self.dead_pid()) })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SURVIVED', result.stdout)

    def test_cleanup_pidfile_removes_only_its_own_record(self):

        pidfile = os.path.join(self.tmp, '.shogibench-worker.pgid')

        # 自分の pid を記録した場合だけ消す
        result = run_sourced(self.tmp, (
            'echo "$$" > "$PIDFILE"\n'
            'cleanup_pidfile\n'
        ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(pidfile), 'own pidfile should be removed')

        # takeover された側のトラップが、新しい bootstrap の記録を消さない
        result = run_sourced(self.tmp, (
            'echo 99999999 > "$PIDFILE"\n'
            'cleanup_pidfile\n'
        ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(pidfile), 'foreign pidfile must survive')


if __name__ == '__main__':
    unittest.main()
