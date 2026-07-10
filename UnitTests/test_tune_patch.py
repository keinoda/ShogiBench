#!/bin/python3

# ワーカーの TUNE ビルド前パッチ (utils.apply_tune_patch -> 本物の Client/tune.py)
# を、小さな模擬ソースツリーに対して実行する結合テスト。
# パラメータがグローバル変数 + TUNE() マクロに置き換わることを確認する

import importlib
import os
import sys
import tempfile
import unittest

PARENT     = os.path.join(os.path.dirname(__file__), os.path.pardir)
CLIENT_DIR = os.path.abspath(os.path.join(PARENT, 'Client'))


def import_client_utils():
    sys.path.insert(0, CLIENT_DIR)
    return importlib.import_module('utils')


TUNE = '''#set file engine\\search.cpp
#set declaration %%TUNE_DECLARATION%%
#set options %%TUNE_OPTIONS%%
#context futility

if (eval >= beta + 100@ * depth - 25@2)
    return eval;
'''

PARAMS = ('futility_1, int, 123, 50, 250, 8, 0.002\n'
          'futility_2, int, 25, 0, 50, 2, 0.002\n')

SOURCE = '''#include "search.h"

// %%TUNE_DECLARATION%%
// %%TUNE_OPTIONS%%

int search(int eval, int beta, int depth) {
    if (eval >= beta + 100 * depth - 25)
        return eval;
    return 0;
}
'''


class TunePatchTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.utils = import_client_utils()

    def make_source_tree(self, root):
        os.makedirs(os.path.join(root, 'engine'))
        with open(os.path.join(root, 'engine', 'search.cpp'), 'w') as fout:
            fout.write(SOURCE)

    def test_patch_injects_tune_macros(self):

        with tempfile.TemporaryDirectory() as root:

            self.make_source_tree(root)
            tune = { 'name' : 'mini', 'sha' : 'AAAA0000',
                     'tune_text' : TUNE, 'params_text' : PARAMS }

            self.utils.apply_tune_patch(tune, root)

            with open(os.path.join(root, 'engine', 'search.cpp')) as fin:
                patched = fin.read()

            # 定数がグローバル変数名に置き換わっている
            self.assertIn('futility_1 * depth - futility_2', patched)
            self.assertNotIn('100 * depth - 25', patched)

            # 宣言は .params の現在値で初期化される (継続チューニングの起点)
            self.assertIn('int futility_1 = 123;', patched)
            self.assertIn('int futility_2 = 25;', patched)

            # TUNE() マクロが .params のレンジで注入されている
            self.assertIn('TUNE(SetRange(50.0, 250.0), futility_1, SetDefaultRange);', patched)
            self.assertIn('TUNE(SetRange(0.0, 50.0), futility_2, SetDefaultRange);', patched)

    def test_patch_fails_loudly_on_context_drift(self):

        with tempfile.TemporaryDirectory() as root:

            self.make_source_tree(root)

            # ソースの定数が進んで context が一致しない (バージョンずれ)
            path = os.path.join(root, 'engine', 'search.cpp')
            with open(path) as fin:
                drifted = fin.read().replace('100 * depth', '120 * depth')
            with open(path, 'w') as fout:
                fout.write(drifted)

            tune = { 'name' : 'mini', 'sha' : 'AAAA0000',
                     'tune_text' : TUNE, 'params_text' : PARAMS }

            with self.assertRaises(self.utils.OpenBenchBuildFailedException) as ctx:
                self.utils.apply_tune_patch(tune, root)

            # ログに tune.py の replaced count エラーが残る (サーバへ送られる)
            self.assertIn('replaced count', ctx.exception.logs)

    def test_binary_name_distinguishes_tune_builds(self):

        base  = self.utils.engine_binary_name('YO', 'a' * 40, 'Networks/AB', False, 'tournament')
        tuned = self.utils.engine_binary_name('YO', 'a' * 40, 'Networks/AB', False, 'tournament', 'AAAA0000')

        self.assertNotEqual(base, tuned)
        self.assertTrue(tuned.endswith('-TAAAA0000'))


if __name__ == '__main__':
    unittest.main()
