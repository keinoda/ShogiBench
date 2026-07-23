#!/bin/python3

import os
import shlex
import subprocess
import unittest

PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir))
SCRIPT = os.path.join(PARENT, 'Deploy', 'worker', 'setup_worker.sh')


class SetupWorkerOpenFileLimitTests(unittest.TestCase):

    def test_raises_low_soft_limit(self):
        command = '''
            export SHOGIBENCH_SOURCE_ONLY=1
            source {script}
            ulimit -Sn 1024
            ensure_open_file_limit >/dev/null
            ulimit -Sn
        '''.format(script=shlex.quote(SCRIPT))

        result = subprocess.run(
            ['bash', '-c', command],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertGreater(int(result.stdout.strip()), 1024)


if __name__ == '__main__':
    unittest.main()
