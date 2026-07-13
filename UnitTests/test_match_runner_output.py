#!/bin/python3

import importlib
import hashlib
import io
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


PARENT = os.path.join(os.path.dirname(__file__), os.path.pardir)
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


class MatchRunnerOutputTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.worker = import_worker()

    def fresh_results(self):
        return {
            'trinomial'   : [0, 0, 0],
            'pentanomial' : [0, 0, 0, 0, 0],
            'games'       : {},
            'crashes'     : 0,
            'timelosses'  : 0,
            'illegals'    : 0,
        }

    def test_parse_current_shogitest_finished_game_line_as_dev_win(self):
        line = 'Finished game 17 (YaneuraOu-nagisa-dev vs YaneuraOu-nagisa-base): 1-0 {Sente wins by adjudication}'
        self.assertEqual(
            self.worker.MatchRunner.parse_finished_game(line),
            (17, 2, 'Sente wins by adjudication'))

    def test_parse_reversed_engine_order_as_dev_win(self):
        line = 'Finished game 18 of infinite (YaneuraOu-nagisa-base vs YaneuraOu-nagisa-dev): 0-1 {Gote wins by resignation}'
        self.assertEqual(
            self.worker.MatchRunner.parse_finished_game(line),
            (18, 2, 'Gote wins by resignation'))

    def test_parse_finished_game_reason_containing_colon(self):
        line = 'Finished game 19 (dev vs base): 1/2-1/2 {Draw by adjudication: Reached move limit}'
        self.assertEqual(
            self.worker.MatchRunner.parse_finished_game(line),
            (19, 1, 'Draw by adjudication: Reached move limit'))

    def test_pair_results_are_reported_from_dev_perspective(self):
        results = self.fresh_results()

        self.worker.MatchRunner.update_results(
            results, 'Finished game 1 (YaneuraOu-nagisa-dev vs YaneuraOu-nagisa-base): 1-0 {Sente resigns}')
        self.worker.MatchRunner.update_results(
            results, 'Finished game 2 (YaneuraOu-nagisa-base vs YaneuraOu-nagisa-dev): 0-1 {Gote resigns}')

        self.assertEqual(results['trinomial'], [0, 0, 2])
        self.assertEqual(results['pentanomial'], [0, 0, 0, 0, 1])
        self.assertEqual(results['games'], {})

    def test_time_and_illegal_reasons_still_count(self):
        results = self.fresh_results()

        self.worker.MatchRunner.update_results(
            results, 'Finished game 1 (YaneuraOu-nagisa-dev vs YaneuraOu-nagisa-base): 0-1 {Sente loses on time}')
        self.worker.MatchRunner.update_results(
            results, 'Finished game 2 (YaneuraOu-nagisa-base vs YaneuraOu-nagisa-dev): 1-0 {Gote makes an illegal move}')

        self.assertEqual(results['timelosses'], 1)
        self.assertEqual(results['illegals'], 1)
        self.assertEqual(results['trinomial'], [2, 0, 0])
        self.assertEqual(results['pentanomial'], [1, 0, 0, 0, 0])
        self.assertEqual(results['games'], {})


class MatchRunnerPonderModeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.worker = import_worker()

    def config(self, book_name, ponder_mode):
        engine = {
            'options'           : 'Threads=1 Hash=16',
            'network'           : '',
            'network_aux_files' : [],
            'private'           : False,
            'engine'            : 'YaneuraOu',
            'time_control'      : '10.0+0.10',
            'ponder_mode'       : ponder_mode,
            'build'             : {},
        }
        return types.SimpleNamespace(
            workload={
                'test' : {
                    'book'       : { 'name' : book_name },
                    'type'       : 'GAMES',
                    'syzygy_wdl' : 'DISABLED',
                    'dev'        : engine,
                },
            },
            syzygy_max=0,
            syzygy_path='',
        )

    def test_shogitest_receives_selected_mode(self):
        config = self.config('openings_shogi_sfen.epd', 'early')
        command = self.worker.MatchRunner.engine_settings(config, 'engine', 'dev', 1.0, 0)

        self.assertIn('proto=usi', command)
        self.assertIn(' ponder=early ', command)

    def test_missing_mode_defaults_to_off(self):
        config = self.config('openings_shogi_sfen.epd', 'off')
        del config.workload['test']['dev']['ponder_mode']
        command = self.worker.MatchRunner.engine_settings(config, 'engine', 'dev', 1.0, 0)

        self.assertIn(' ponder=off ', command)

    def test_fastchess_does_not_receive_shogitest_option(self):
        config = self.config('openings.epd', 'early')
        command = self.worker.MatchRunner.engine_settings(config, 'engine', 'dev', 1.0, 0)

        self.assertIn('proto=uci', command)
        self.assertNotIn('ponder=', command)

    def test_unknown_mode_is_rejected(self):
        config = self.config('openings_shogi_sfen.epd', 'unexpected')
        with self.assertRaisesRegex(ValueError, 'Unknown Ponder mode'):
            self.worker.MatchRunner.engine_settings(config, 'engine', 'dev', 1.0, 0)

    def affinity_config(self, concurrency=2):
        config = self.config('openings_shogi_sfen.epd', 'early')
        config.workload['test']['base'] = dict(config.workload['test']['dev'])
        config.workload['test']['base']['ponder_mode'] = 'standard'
        config.workload['distribution'] = {
            'runner-count'     : 1,
            'concurrency-per'  : concurrency,
            'games-per-runner' : 2,
            'threads-per-game' : 2,
            'cpu-affinity'     : True,
        }
        config.threads = 8
        return config

    def test_ponder_affinity_uses_disjoint_physical_cpus(self):
        config = self.affinity_config()
        with patch.object(self.worker.platform, 'system', return_value='Linux'), patch.object(
                self.worker, 'linux_physical_cpu_ids', return_value=[2, 4, 6, 8, 10, 12]):
            affinity = self.worker.MatchRunner.affinity_settings(config, 0)

        self.assertEqual(affinity, '-cpu-affinity 2,4,6,8')

    def test_ponder_affinity_rejects_insufficient_physical_cpus(self):
        config = self.affinity_config()
        with patch.object(self.worker.platform, 'system', return_value='Linux'), patch.object(
                self.worker, 'linux_physical_cpu_ids', return_value=[2, 4, 6]):
            with self.assertRaisesRegex(RuntimeError, 'requires 4 dedicated physical CPUs'):
                self.worker.MatchRunner.affinity_settings(config, 0)

    def test_non_ponder_match_does_not_request_affinity(self):
        config = self.affinity_config()
        config.workload['distribution']['cpu-affinity'] = False
        self.assertEqual(self.worker.MatchRunner.affinity_settings(config, 0), '')

    def test_runner_command_includes_the_validated_affinity(self):
        config = self.affinity_config()
        settings = self.worker.MatchRunner
        with patch.object(self.worker.platform, 'system', return_value='Linux'), \
                patch.object(self.worker, 'linux_physical_cpu_ids', return_value=[2, 4, 6, 8]), \
                patch.object(settings, 'executable', return_value='./shogitest-ob'), \
                patch.object(settings, 'basic_settings', return_value='-repeat'), \
                patch.object(settings, 'adjudication_settings', return_value=''), \
                patch.object(settings, 'engine_settings', return_value='-engine fake'), \
                patch.object(settings, 'book_settings', return_value='-openings fake'), \
                patch.object(settings, 'pgnout_settings', return_value='-pgnout fake'):
            command = self.worker.build_runner_command(
                config, 'dev-engine', 'base-engine', 1.0, 0.0, 0)

        self.assertIn('-concurrency 2', command)
        self.assertIn('-cpu-affinity 2,4,6,8', command)

    def test_linux_cpu_selection_removes_smt_siblings(self):
        topology = {
            '/sys/devices/system/cpu/cpu0/topology/physical_package_id' : '0',
            '/sys/devices/system/cpu/cpu0/topology/core_id'             : '0',
            '/sys/devices/system/cpu/cpu1/topology/physical_package_id' : '0',
            '/sys/devices/system/cpu/cpu1/topology/core_id'             : '1',
            '/sys/devices/system/cpu/cpu4/topology/physical_package_id' : '0',
            '/sys/devices/system/cpu/cpu4/topology/core_id'             : '0',
        }

        def topology_file(path, *args, **kwargs):
            if path not in topology:
                raise FileNotFoundError(path)
            return io.StringIO(topology[path])

        with patch.object(self.worker.platform, 'system', return_value='Linux'), \
                patch.object(self.worker.os, 'sched_getaffinity', return_value={4, 1, 0}, create=True), \
                patch('builtins.open', side_effect=topology_file):
            self.assertEqual(self.worker.linux_physical_cpu_ids(), [0, 1])


class StageNetworkOptionsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.worker = import_worker()

    def test_stale_staged_files_are_replaced_by_sha(self):
        old_cwd = os.getcwd()

        with tempfile.TemporaryDirectory() as tempdir:
            try:
                os.chdir(tempdir)
                os.mkdir('Networks')

                network = b'correct-network'
                progress = b'correct-progress'
                net_sha = hashlib.sha256(network).hexdigest()[:8].upper()
                progress_sha = hashlib.sha256(progress).hexdigest()[:8].upper()

                with open(os.path.join('Networks', net_sha), 'wb') as fout:
                    fout.write(network)
                with open(os.path.join('Networks', progress_sha), 'wb') as fout:
                    fout.write(progress)

                staged_dir = os.path.join('Networks', '%s-dir' % (net_sha))
                os.mkdir(staged_dir)
                with open(os.path.join(staged_dir, 'nn.bin'), 'wb') as fout:
                    fout.write(b'stale-network')
                with open(os.path.join(staged_dir, 'progress.bin'), 'wb') as fout:
                    fout.write(b'stale-progress')

                config = types.SimpleNamespace(workload={ 'test' : { 'dev' : {
                    'private'           : False,
                    'network'           : net_sha,
                    'build'             : {
                        'network_option'      : 'EvalDir',
                        'network_filename'    : 'nn.bin',
                        'network_aux_options' : { 'progress.bin' : 'LS_PROGRESS_COEFF' },
                    },
                    'network_aux_files' : [
                        { 'name' : 'progress.bin', 'sha' : progress_sha },
                    ],
                } } })

                pairs = self.worker.stage_network_options(config, 'dev')

                with open(os.path.join(staged_dir, 'nn.bin'), 'rb') as fin:
                    self.assertEqual(fin.read(), network)
                with open(os.path.join(staged_dir, 'progress.bin'), 'rb') as fin:
                    self.assertEqual(fin.read(), progress)

                abs_dir = os.path.abspath(staged_dir)
                self.assertIn(('EvalDir', abs_dir), pairs)
                self.assertIn(('LS_PROGRESS_COEFF', os.path.abspath(os.path.join(staged_dir, 'progress.bin'))), pairs)

            finally:
                os.chdir(old_cwd)

    def test_eval_options_accepts_space_separator(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as fout:
            path = fout.name
            fout.write('LS_BUCKET_MODE progress8kpabs\n')
            fout.write('FV_SCALE=28\n')

        try:
            self.assertEqual(self.worker.parse_eval_options_file(path), [
                ('LS_BUCKET_MODE', 'progress8kpabs'),
                ('FV_SCALE', '28'),
            ])
        finally:
            os.remove(path)

    def test_eval_options_managed_path_option_stops_workload(self):
        old_cwd = os.getcwd()

        with tempfile.TemporaryDirectory() as tempdir:
            try:
                os.chdir(tempdir)
                os.mkdir('Networks')

                network = b'correct-network'
                eval_options = b'LS_PROGRESS_COEFF ./progress.bin\n'
                net_sha = hashlib.sha256(network).hexdigest()[:8].upper()
                opts_sha = hashlib.sha256(eval_options).hexdigest()[:8].upper()

                with open(os.path.join('Networks', net_sha), 'wb') as fout:
                    fout.write(network)
                with open(os.path.join('Networks', opts_sha), 'wb') as fout:
                    fout.write(eval_options)

                config = types.SimpleNamespace(workload={ 'test' : { 'dev' : {
                    'private'           : False,
                    'network'           : net_sha,
                    'build'             : {
                        'network_option'      : 'EvalDir',
                        'network_filename'    : 'nn.bin',
                        'network_aux_options' : { 'progress.bin' : 'LS_PROGRESS_COEFF' },
                    },
                    'network_aux_files' : [
                        { 'name' : 'eval_options.txt', 'sha' : opts_sha },
                    ],
                } } })

                with self.assertRaises(self.worker.utils.OpenBenchCorruptedNetworkException):
                    self.worker.stage_network_options(config, 'dev')

            finally:
                os.chdir(old_cwd)


if __name__ == '__main__':
    unittest.main()
