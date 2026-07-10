#!/bin/python3

# ワーカーの SPSA 実行ループ (起動 -> 監視 -> 進捗報告 -> 完走報告) の結合テスト。
# 本物の rshogi の代わりに、run dir へ meta.json / stats.csv / state.params /
# final.params を書いて終了する偽 spsa (シェルスクリプト) を使う

import importlib
import json
import os
import sys
import tempfile
import types
import unittest

PARENT     = os.path.join(os.path.dirname(__file__), os.path.pardir)
CLIENT_DIR = os.path.abspath(os.path.join(PARENT, 'Client'))

FAKE_SPSA = r'''#!/bin/bash
# 偽 rshogi spsa: 引数から --run-dir を拾い、2 バッチ分の成果物を書いて完走する
RUN_DIR=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "--run-dir" ]; then RUN_DIR="$arg"; fi
    prev="$arg"
done
echo "$@" > "$RUN_DIR/argv.txt"

cat > "$RUN_DIR/stats.csv" <<EOF
iteration,batch_pairs,plus_wins,minus_wins,draws,raw_result,active_params,avg_abs_shift,updated_params,avg_abs_update,max_abs_update,total_games
1,4,4,3,1,+1.000000,1,1.0,1,0.02,0.1,8
2,4,3,4,1,-1.000000,1,1.0,1,0.01,0.1,16
EOF

cat > "$RUN_DIR/state.params" <<EOF
Foo,int,105.500000,50,200,10,0.002
EOF

cat > "$RUN_DIR/meta.json" <<EOF
{ "format_version": 4, "completed_iterations": 2, "completed_pairs": 8,
  "total_pairs": 8, "batch_pairs": 4, "total_games": 16,
  "last_raw_result_mean": -1.0, "last_avg_abs_update": 0.01 }
EOF

sleep 0.3

cat > "$RUN_DIR/final.params" <<EOF
Foo,int,106,50,200,10,0.002
EOF
'''


class FakeResponse:
    def __init__(self, data):
        self._data = data
    def json(self):
        return self._data


class FakeReporter:

    ## worker.ServerReporter の代役。送られた payload を記録する

    def __init__(self):
        self.payloads = []
        self.errors   = []

    def report(self, config, endpoint, payload, files=None):
        assert endpoint == 'clientSubmitSpsa'
        self.payloads.append(dict(payload))
        return FakeResponse({})

    def report_engine_error(self, config, error, logs=None):
        self.errors.append((error, logs))
        return FakeResponse({})


def import_spsa_rshogi():
    sys.path.insert(0, CLIENT_DIR)
    return importlib.import_module('spsa_rshogi')


def fake_config(workdir, spsa_overrides=None):

    spsa = {
        'wrapper'      : 'RSHOGI',
        'alpha'        : 0.602,
        'gamma'        : 0.101,
        'a_ratio'      : 0.1,
        'total_pairs'  : 8,
        'batch_pairs'  : 4,
        'seed'         : 1,
        'active_regex' : '',
        'mapping'      : 'NONE',
        'early_stop'   : { 'patience' : 0 },
        'params_text'  : 'Foo, int, 100, 50, 200, 10, 0.002\n',
        'state_params' : '',
        'concurrency'  : 8,
        'carry'        : { 'pairs' : 0, 'games' : 0, 'wins' : 0, 'losses' : 0, 'draws' : 0 },
    }
    spsa.update(spsa_overrides or {})

    config = types.SimpleNamespace()
    config.workload = {
        'test' : {
            'id'  : 7,
            'dev' : { 'time_control' : '2.0+0.02' },
        },
        'result' : { 'id' : 3 },
        'spsa'   : spsa,
    }
    config.blacklist = []
    return config


class SpsaRunWorkloadTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_spsa_rshogi()

    def run_in_temp(self, spsa_overrides=None):

        ## Client の cwd 相当の一時ディレクトリで run_workload を回す

        with tempfile.TemporaryDirectory() as workdir:

            fake_binary = os.path.join(workdir, 'spsa-ob')
            with open(fake_binary, 'w') as fout:
                fout.write(FAKE_SPSA)
            os.chmod(fake_binary, 0o755)

            engine = os.path.join(workdir, 'Engines', 'YO-test')
            os.makedirs(os.path.dirname(engine))
            open(engine, 'w').close()

            book = os.path.join(workdir, 'Books', 'test.epd')
            os.makedirs(os.path.dirname(book))
            open(book, 'w').close()

            config   = fake_config(workdir, spsa_overrides)
            reporter = FakeReporter()

            cwd = os.getcwd()
            os.chdir(workdir)

            # テストを速くする: 報告間隔を縮め、バイナリの用意をスキップ
            saved = (self.mod.REPORT_INTERVAL, self.mod.POLL_INTERVAL, self.mod.ensure_spsa_binary)
            self.mod.REPORT_INTERVAL   = 0.1
            self.mod.POLL_INTERVAL     = 0.05
            self.mod.ensure_spsa_binary = lambda config: (fake_binary, None)

            try:
                self.mod.run_workload(config, reporter, engine, [('EvalDir', '/abs/eval')],
                                      book, 1, 16, 1.0)
            finally:
                self.mod.REPORT_INTERVAL, self.mod.POLL_INTERVAL, self.mod.ensure_spsa_binary = saved
                os.chdir(cwd)

            run_dir = os.path.join(workdir, 'SPSA', '7', 'run')
            argv    = open(os.path.join(run_dir, 'argv.txt')).read()
            return config, reporter, argv

    def test_full_run_reports_progress_and_final(self):

        config, reporter, argv = self.run_in_temp()

        # 偽 spsa には正しいフラグ一式が渡っている
        self.assertIn('--total-pairs 8', argv)
        self.assertIn('--batch-pairs 4', argv)
        self.assertIn('--btime 2000 --binc 20', argv)
        self.assertIn('--init-from', argv)
        self.assertIn('--usi-option EvalDir=/abs/eval', argv)
        self.assertIn('--require-startpos-file', argv)

        # 最後の報告が完走 (finished=1 + final.params)
        self.assertTrue(reporter.payloads)
        final = reporter.payloads[-1]
        self.assertEqual(final['finished'], '1')
        self.assertIn('Foo,int,106', final['final_params'])
        self.assertEqual(final['completed_pairs'], 8)
        self.assertEqual(final['total_games'], 16)
        self.assertEqual(final['wins'], 7)
        self.assertEqual(final['losses'], 7)
        self.assertEqual(final['draws'], 2)
        self.assertIn('Foo,int,105.500000', final['state_params'])
        self.assertEqual(reporter.errors, [])

    def test_takeover_offsets_are_added(self):

        # 別マシンからの引き継ぎ: サーバ由来の累計 (carry) が上乗せされる
        config, reporter, argv = self.run_in_temp({
            'total_pairs'  : 100,
            'carry'        : { 'pairs' : 50, 'games' : 100,
                               'wins' : 40, 'losses' : 35, 'draws' : 25 },
            'state_params' : 'Foo,int,120.000000,50,200,10,0.002\n',
        })

        # 残り 50 ペアの新しいスケジュールで、既存 state を起点に開始
        self.assertIn('--total-pairs 50', argv)
        self.assertIn('--use-existing-state-as-init', argv)
        self.assertNotIn('--init-from', argv)

        final = reporter.payloads[-1]
        self.assertEqual(final['completed_pairs'], 50 + 8)
        self.assertEqual(final['total_games'],    100 + 16)
        self.assertEqual(final['wins'],            40 + 7)

    def test_crash_without_final_reports_error_and_blacklists(self):

        with tempfile.TemporaryDirectory() as workdir:

            # final.params を書かずに異常終了する偽 spsa
            fake_binary = os.path.join(workdir, 'spsa-ob')
            with open(fake_binary, 'w') as fout:
                fout.write('#!/bin/bash\necho "Error: engine died" >&2\nexit 1\n')
            os.chmod(fake_binary, 0o755)

            engine = os.path.join(workdir, 'Engines', 'YO-test')
            os.makedirs(os.path.dirname(engine)); open(engine, 'w').close()
            book = os.path.join(workdir, 'Books', 'test.epd')
            os.makedirs(os.path.dirname(book)); open(book, 'w').close()

            config   = fake_config(workdir)
            reporter = FakeReporter()

            cwd = os.getcwd()
            os.chdir(workdir)
            saved = (self.mod.REPORT_INTERVAL, self.mod.POLL_INTERVAL, self.mod.ensure_spsa_binary)
            self.mod.REPORT_INTERVAL   = 0.1
            self.mod.POLL_INTERVAL     = 0.05
            self.mod.ensure_spsa_binary = lambda config: (fake_binary, None)

            try:
                self.mod.run_workload(config, reporter, engine, [], book, 1, 16, 1.0)
            finally:
                self.mod.REPORT_INTERVAL, self.mod.POLL_INTERVAL, self.mod.ensure_spsa_binary = saved
                os.chdir(cwd)

            # エラーが報告され、再割り当てを避けるためにブラックリストへ入る
            self.assertTrue(reporter.errors)
            self.assertIn('Error: engine died', reporter.errors[0][1])
            self.assertIn(7, config.blacklist)


if __name__ == '__main__':
    unittest.main()
