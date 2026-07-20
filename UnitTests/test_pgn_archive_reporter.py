#!/bin/python3

import importlib
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


def sample_game():
    return (
        '[Event "checkpoint"]\n'
        '[Site "Local"]\n'
        '[Date "2026.07.20"]\n'
        '[Round "1"]\n'
        '[White "dev"]\n'
        '[Black "base"]\n'
        '[Result "1-0"]\n'
        '\n'
        '1. 7g7f {book} 3c3d {book} 1-0\n\n'
    )


class PGNArchiveReporterTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.worker = import_worker()

    def config(self):
        return types.SimpleNamespace(workload={
            'test' : {
                'id'          : 12,
                'book_index'  : 34,
                'upload_pgns' : 'COMPACT',
            },
            'result' : { 'id' : 56 },
        })

    def test_http_failure_retries_same_part_and_offsets(self):
        class FailedResponse:
            def raise_for_status(self):
                raise RuntimeError('HTTP 500')

        class SuccessfulResponse:
            def raise_for_status(self):
                return None

        with tempfile.TemporaryDirectory() as tempdir:
            path = os.path.join(tempdir, 'games.pgn')
            with open(path, 'w') as fout:
                fout.write(sample_game())

            reporter = self.worker.PGNArchiveReporter(self.config(), [path], 1.0)

            with patch.object(
                    self.worker.ServerReporter, 'report_pgn',
                    return_value=FailedResponse()) as report:
                with self.assertRaisesRegex(RuntimeError, 'HTTP 500'):
                    reporter.checkpoint()
                self.assertEqual(reporter.offsets, {})
                self.assertEqual(reporter.part, 0)
                self.assertEqual(report.call_args.args[2], 0)

            with patch.object(
                    self.worker.ServerReporter, 'report_pgn',
                    return_value=SuccessfulResponse()) as report:
                self.assertTrue(reporter.checkpoint())
                self.assertGreater(reporter.offsets[path], 0)
                self.assertEqual(reporter.part, 1)
                self.assertEqual(report.call_args.args[2], 0)

                # 新しい対局が無ければ空のpartを送らない。
                self.assertFalse(reporter.checkpoint())
                self.assertEqual(report.call_count, 1)


if __name__ == '__main__':
    unittest.main()
