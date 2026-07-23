#!/bin/python3

# ワーカー側 SPSA (rshogi ラッパー) のヘルパのテスト。
# rshogi バイナリ無しでテストできる純関数 (コマンド組み立て・時間制御変換・
# run dir の進捗読み取り・起動モード決定) を対象にする

import importlib
import json
import os
import sys
import tempfile
import unittest

PARENT     = os.path.join(os.path.dirname(__file__), os.path.pardir)
CLIENT_DIR = os.path.abspath(os.path.join(PARENT, 'Client'))


def import_spsa_rshogi():
    sys.path.insert(0, CLIENT_DIR)
    return importlib.import_module('spsa_rshogi')


def example_spsa():
    return {
        'wrapper'      : 'RSHOGI',
        'alpha'        : 0.602,
        'gamma'        : 0.101,
        'a_ratio'      : 0.1,
        'total_pairs'  : 51200,
        'batch_pairs'  : 96,
        'seed'         : 1,
        'active_regex' : '',
        'mapping'      : 'NONE',
        'early_stop'   : { 'patience' : 0 },
        'params_text'  : 'Foo, int, 100, 50, 200, 10, 0.002\n',
        'state_params' : '',
        'concurrency'  : 160,
        'carry'        : { 'pairs' : 0, 'games' : 0, 'wins' : 0, 'losses' : 0, 'draws' : 0 },
    }


class TimeControlFlagTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_spsa_rshogi()

    def test_fischer(self):
        # 2.0+0.02 (run_production.sh の tc=2+0.02 相当) -> btime 2000ms binc 20ms
        flags = self.mod.time_control_flags('2.0+0.02', 1.0)
        self.assertEqual(flags, ['--btime', '2000', '--binc', '20'])

    def test_fischer_scaling(self):
        # 遅いマシン (scale=2.0) では持ち時間が伸びる
        flags = self.mod.time_control_flags('2.0+0.02', 2.0)
        self.assertEqual(flags, ['--btime', '4000', '--binc', '40'])

    def test_nodes_no_scaling(self):
        flags = self.mod.time_control_flags('N=25000', 3.0)
        self.assertEqual(flags, ['--nodes', '25000'])

    def test_movetime_to_byoyomi(self):
        flags = self.mod.time_control_flags('MT=1000', 0.5)
        self.assertEqual(flags, ['--byoyomi', '500'])

    def test_rejects_cyclic(self):
        with self.assertRaises(ValueError):
            self.mod.time_control_flags('40/60.0+0.6', 1.0)


class BuildCommandTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_spsa_rshogi()

    def build(self, spsa, mode_flags=None, total=None, mapping=None):
        return self.mod.build_spsa_command(
            '/client/spsa-ob', '/client/SPSA/7/run', '/client/Engines/YO', spsa,
            ['--btime', '2000', '--binc', '20'],
            [('EvalDir', '/client/Networks/AB-dir'), ('USI_OwnBook', 'false')],
            1, 16, '/client/Books/taya36.epd',
            mode_flags if mode_flags is not None else ['--init-from', '/client/SPSA/7/canonical.params'],
            total if total is not None else spsa['total_pairs'],
            mapping)

    def test_production_like_command(self):

        # fuuppi-spsa の run_production.sh と同じ構成のフラグが揃うこと
        cmd = self.build(example_spsa())

        def value_of(flag):
            return cmd[cmd.index(flag) + 1]

        self.assertEqual(cmd[0], '/client/spsa-ob')
        self.assertEqual(value_of('--run-dir'),     '/client/SPSA/7/run')
        self.assertEqual(value_of('--engine-path'), '/client/Engines/YO')
        self.assertEqual(value_of('--total-pairs'), '51200')
        self.assertEqual(value_of('--batch-pairs'), '96')
        self.assertEqual(value_of('--concurrency'), '160')
        self.assertEqual(value_of('--threads'),     '1')
        self.assertEqual(value_of('--hash-mb'),     '16')
        self.assertEqual(value_of('--btime'),       '2000')
        self.assertEqual(value_of('--binc'),        '20')
        self.assertEqual(value_of('--startpos-file'), '/client/Books/taya36.epd')
        self.assertIn('--require-startpos-file', cmd)
        self.assertEqual(value_of('--seed'), '1')
        self.assertEqual(value_of('--init-from'), '/client/SPSA/7/canonical.params')

        # USI オプションは Name=Value 形式で並ぶ
        self.assertIn('EvalDir=/client/Networks/AB-dir', cmd)
        self.assertIn('USI_OwnBook=false', cmd)

        # 指定していない機能のフラグは出ない
        for flag in ['--active-only-regex', '--engine-param-mapping', '--early-stop-patience']:
            self.assertNotIn(flag, cmd)

    def test_optional_flags(self):

        spsa = example_spsa()
        spsa['seed']         = None
        spsa['active_regex'] = '^(SPSA_LMR)_'
        spsa['mapping']      = 'YO'
        spsa['early_stop']   = { 'patience' : 5, 'avg_abs_update' : 0.01, 'result_variance' : 0.05 }

        cmd = self.build(spsa, mapping='/client/yo_rshogi_mapping.toml')

        self.assertNotIn('--seed', cmd)
        self.assertEqual(cmd[cmd.index('--active-only-regex') + 1], '^(SPSA_LMR)_')
        self.assertEqual(cmd[cmd.index('--engine-param-mapping') + 1], '/client/yo_rshogi_mapping.toml')
        self.assertEqual(cmd[cmd.index('--early-stop-patience') + 1], '5')
        self.assertEqual(cmd[cmd.index('--early-stop-avg-abs-update-threshold') + 1], '0.01')
        self.assertEqual(cmd[cmd.index('--early-stop-result-variance-threshold') + 1], '0.05')

    def test_mapping_requires_table(self):

        spsa = example_spsa()
        spsa['mapping'] = 'YO'
        with self.assertRaises(Exception):
            self.build(spsa, mapping=None)

    def test_resume_flags_appended(self):

        cmd = self.build(example_spsa(), mode_flags=['--resume', '--force-unlock'], total=51200)
        self.assertIn('--resume', cmd)
        self.assertIn('--force-unlock', cmd)
        self.assertNotIn('--init-from', cmd)


class RunDirProgressTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_spsa_rshogi()

    def write_run_dir(self, run_dir):

        os.makedirs(run_dir, exist_ok=True)

        # rshogi v4 の stats.csv (iteration,batch_pairs,plus_wins,minus_wins,draws,...)
        with open(os.path.join(run_dir, 'stats.csv'), 'w') as fout:
            fout.write('iteration,batch_pairs,plus_wins,minus_wins,draws,raw_result,'
                       'active_params,avg_abs_shift,updated_params,avg_abs_update,'
                       'max_abs_update,total_games\n')
            fout.write('1,96,100,80,12,+20.000000,98,1.5,98,0.02,0.4,192\n')
            fout.write('2,96,90,90,12,+0.000000,98,1.4,98,0.01,0.3,384\n')

        with open(os.path.join(run_dir, 'meta.json'), 'w') as fout:
            json.dump({
                'format_version'       : 4,
                'completed_iterations' : 2,
                'completed_pairs'      : 192,
                'total_pairs'          : 51200,
                'batch_pairs'          : 96,
                'total_games'          : 384,
                'last_raw_result_mean' : 0.0,
                'last_avg_abs_update'  : 0.01,
            }, fout)

        with open(os.path.join(run_dir, 'state.params'), 'w') as fout:
            fout.write('Foo,int,105.500000,50,200,10,0.002\n')

        with open(os.path.join(run_dir, 'values.csv'), 'w') as fout:
            fout.write('iteration,Foo\n')
            fout.write('0,100.000000\n')
            fout.write('1,103.000000\n')
            fout.write('2,105.500000\n')

    def test_read_stats_totals(self):

        with tempfile.TemporaryDirectory() as temp:
            self.write_run_dir(temp)
            batches, wins, losses, draws = self.mod.read_stats_totals(os.path.join(temp, 'stats.csv'))
            self.assertEqual((batches, wins, losses, draws), (2, 190, 170, 24))

    def test_read_run_progress(self):

        with tempfile.TemporaryDirectory() as temp:
            self.write_run_dir(temp)
            progress = self.mod.read_run_progress(temp)

            self.assertEqual(progress['completed_pairs'],   192)
            self.assertEqual(progress['completed_batches'], 2)
            self.assertEqual(progress['total_games'],       384)
            self.assertEqual(progress['wins'],              190)
            self.assertEqual(progress['losses'],            170)
            self.assertEqual(progress['draws'],             24)
            self.assertIn('Foo,int,105.500000', progress['state_params'])
            self.assertEqual(progress['final_params'], '')

    def test_read_run_progress_empty(self):

        with tempfile.TemporaryDirectory() as temp:
            progress = self.mod.read_run_progress(temp)
            self.assertEqual(progress['completed_pairs'], 0)
            self.assertEqual(progress['state_params'], '')

    def test_read_trajectory_uses_server_batch_and_offset(self):

        with tempfile.TemporaryDirectory() as temp:
            self.write_run_dir(temp)
            trajectory = self.mod.read_trajectory(temp, batch_offset=10, after_batch=11)

            self.assertEqual(trajectory['names'], ['Foo'])
            self.assertEqual([row[0] for row in trajectory['stats']], [11, 12])
            self.assertEqual([row[0] for row in trajectory['values']], [10, 11, 12])
            self.assertEqual(trajectory['values'][-1][1], [105.5])


class LaunchModeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_spsa_rshogi()

    def test_fresh_start(self):

        with tempfile.TemporaryDirectory() as temp:
            base_dir, run_dir = temp, os.path.join(temp, 'run')
            os.makedirs(run_dir)
            canonical = os.path.join(temp, 'canonical.params')

            flags, total, offsets = self.mod.choose_launch_mode(
                base_dir, run_dir, canonical, example_spsa())

            self.assertEqual(flags, ['--init-from', canonical])
            self.assertEqual(total, 51200)
            self.assertEqual(offsets['pairs'], 0)

    def test_resume_uses_meta_total(self):

        with tempfile.TemporaryDirectory() as temp:
            base_dir, run_dir = temp, os.path.join(temp, 'run')
            os.makedirs(run_dir)

            with open(os.path.join(run_dir, 'meta.json'), 'w') as fout:
                json.dump({ 'total_pairs' : 12345 }, fout)
            with open(os.path.join(run_dir, 'state.params'), 'w') as fout:
                fout.write('Foo,int,105.000000,50,200,10,0.002\n')

            flags, total, offsets = self.mod.choose_launch_mode(
                base_dir, run_dir, os.path.join(temp, 'canonical.params'), example_spsa())

            self.assertEqual(flags, ['--resume', '--force-unlock'])
            self.assertEqual(total, 12345)

    def test_partial_fresh_start_uses_force_init(self):

        # 初期化中に落ちて state.params だけ残った場合はcanonicalから再初期化する
        with tempfile.TemporaryDirectory() as temp:
            base_dir, run_dir = temp, os.path.join(temp, 'run')
            os.makedirs(run_dir)
            canonical = os.path.join(temp, 'canonical.params')

            with open(os.path.join(run_dir, 'state.params'), 'w') as fout:
                fout.write('Foo,int,100.000000,50,200,10,0.002\n')

            flags, total, offsets = self.mod.choose_launch_mode(
                base_dir, run_dir, canonical, example_spsa())

            self.assertEqual(flags, ['--init-from', canonical, '--force-init'])
            self.assertEqual(total, 51200)
            self.assertEqual(offsets['pairs'], 0)

    def test_takeover_from_server_state(self):

        # 別マシンからの引き継ぎ: サーバの state.params を起点に残りペアを回す
        with tempfile.TemporaryDirectory() as temp:
            base_dir, run_dir = temp, os.path.join(temp, 'run')
            os.makedirs(run_dir)

            spsa = example_spsa()
            spsa['carry'] = { 'pairs' : 20000, 'games' : 40000,
                              'wins' : 15000, 'losses' : 14000, 'draws' : 11000 }
            spsa['state_params'] = 'Foo,int,140.000000,50,200,10,0.002\n'

            flags, total, offsets = self.mod.choose_launch_mode(
                base_dir, run_dir, os.path.join(temp, 'canonical.params'), spsa)

            self.assertEqual(flags, ['--use-existing-state-as-init'])
            self.assertEqual(total, 51200 - 20000)
            self.assertEqual(offsets['pairs'], 20000)
            self.assertEqual(offsets['games'], 40000)
            self.assertEqual(offsets['wins'],  15000)

            # 引き継いだ state がディスクに書かれている
            with open(os.path.join(run_dir, 'state.params')) as fin:
                self.assertIn('Foo,int,140.000000', fin.read())

            # offsets.json が永続化される (以降の resume でも累計がずれない)
            self.assertEqual(self.mod.load_offsets(base_dir)['pairs'], 20000)


if __name__ == '__main__':
    unittest.main()
