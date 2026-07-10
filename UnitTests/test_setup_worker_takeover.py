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


if __name__ == '__main__':
    unittest.main()
