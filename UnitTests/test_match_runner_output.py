#!/bin/python3

import importlib
import os
import sys
import types
import unittest


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


if __name__ == '__main__':
    unittest.main()
