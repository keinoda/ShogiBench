#!/bin/python3

# サーバ側 SPSA (rshogi ラッパー) の .params テキスト処理のテスト。
# OpenBench/spsa_params.py は Django に依存しないので直接 import できる

import importlib
import os
import sys
import unittest

PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir))


def import_spsa_params():
    sys.path.insert(0, PARENT)
    return importlib.import_module('OpenBench.spsa_params')


class SpsaParamsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_spsa_params()

    def test_basic_parse(self):

        text = 'FooParam, int, 100, 50, 200, 10, 0.002\n' \
               'BarParam, float, 0.5, 0.0, 1.0, 0.05, 0.002\n'

        rows, errors = self.mod.parse_params_text(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 2)

        self.assertEqual(rows[0]['name'], 'FooParam')
        self.assertEqual(rows[0]['kind'], 'int')
        self.assertEqual(rows[0]['value'], 100.0)
        self.assertEqual(rows[0]['min'], 50.0)
        self.assertEqual(rows[0]['max'], 200.0)
        self.assertEqual(rows[0]['c_end'], 10.0)
        self.assertEqual(rows[0]['r_end'], 0.002)
        self.assertFalse(rows[0]['not_used'])

    def test_comments_and_not_used(self):

        # fuuppi-spsa の canonical と同じ流儀: # 行コメント、// 行内コメント、
        # [[NOT USED]] マーカー。コメント内のマーカーは誤検出しない
        text = '# whole line comment\n' \
               '\n' \
               'Live, int, 1, 0, 10, 1, 0.002 // 生きている\n' \
               'Dead, int, 5, 0, 10, 1, 0.002 [[NOT USED]] // 死んでいる\n' \
               'Tricky, int, 5, 0, 10, 1, 0.002 // 旧: [[NOT USED]]\n'

        rows, errors = self.mod.parse_params_text(text)
        self.assertEqual(errors, [])
        self.assertEqual([x['name'] for x in rows], ['Live', 'Dead', 'Tricky'])
        self.assertEqual([x['not_used'] for x in rows], [False, True, False])
        self.assertEqual(rows[0]['comment'], '生きている')

    def test_negative_values_sign_flip_rows(self):

        # sign_flip 済み canonical (例: SPSA_NMP_MARGIN_OFFSET) は負の値・負のレンジ
        text = 'SPSA_NMP_MARGIN_OFFSET, int, -390, -780, -195, 30, 0.002\n'
        rows, errors = self.mod.parse_params_text(text)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]['value'], -390.0)
        self.assertEqual(rows[0]['min'], -780.0)
        self.assertEqual(rows[0]['max'], -195.0)

    def test_errors(self):

        checks = [
            ('OnlySix, int, 1, 0, 10, 1\n',            '7カラム'),
            ('Bad Name, int, 1, 0, 10, 1, 0.002\n',    '使えない文字'),
            ('Foo, str, 1, 0, 10, 1, 0.002\n',         '型は int か float'),
            ('Foo, int, 11, 0, 10, 1, 0.002\n',        '[min, max] の外'),
            ('Foo, int, 5, 10, 0, 1, 0.002\n',         'min が max'),
            ('Foo, int, 5, 0, 10, 0, 0.002\n',         'C_end'),
            ('Foo, int, 5, 0, 10, 1, 0\n',             'R_end'),
            ('Foo, int, 5, 0, 10, 1, 0.002\nFoo, int, 5, 0, 10, 1, 0.002\n', '重複'),
        ]

        for text, expected in checks:
            rows, errors = self.mod.parse_params_text(text)
            self.assertTrue(any(expected in e for e in errors),
                            'expected "%s" in errors for %r, got %s' % (expected, text, errors))

    def test_requires_one_active_row(self):

        rows, errors = self.mod.parse_params_text('Dead, int, 5, 0, 10, 1, 0.002 [[NOT USED]]\n')
        self.assertTrue(any('1つもありません' in e for e in errors))

        rows, errors = self.mod.parse_params_text('')
        self.assertTrue(errors)

    def test_not_used_rows_skip_schedule_checks(self):

        # [[NOT USED]] 行は C_end/R_end が 0 でも許す (ファイル整合性のため保持するだけ)
        text = 'Live, int, 1, 0, 10, 1, 0.002\n' \
               'Dead, int, 5, 0, 10, 0, 0 [[NOT USED]]\n'
        rows, errors = self.mod.parse_params_text(text)
        self.assertEqual(errors, [])

    def test_rows_to_parameters(self):

        text = 'Foo, int, 100, 50, 200, 10, 0.002\n' \
               'Bar, float, 0.5, 0.0, 1.0, 0.05, 0.002 [[NOT USED]]\n'
        rows, errors = self.mod.parse_params_text(text)
        params = self.mod.rows_to_parameters(rows)

        self.assertEqual(params['Foo']['index'], 0)
        self.assertEqual(params['Foo']['start'], 100.0)
        self.assertEqual(params['Foo']['value'], 100.0)
        self.assertFalse(params['Foo']['float'])
        self.assertFalse(params['Foo']['not_used'])
        self.assertTrue(params['Bar']['float'])
        self.assertTrue(params['Bar']['not_used'])

    def test_parse_state_params_text(self):

        # rshogi の state.params は整数パラメータも小数表記で保存する
        text = 'Foo,int,42.000000,0,100,10,0.002\n' \
               'Bar,float,0.123456,0,1,0.05,0.002\n'
        values = self.mod.parse_state_params_text(text)
        self.assertEqual(values['Foo'], 42.0)
        self.assertAlmostEqual(values['Bar'], 0.123456)

    def test_normalize_params_text(self):

        text = 'Foo, int, 1, 0, 10, 1, 0.002\r\nBar, int, 2, 0, 10, 1, 0.002   \r\n\r\n'
        normalized = self.mod.normalize_params_text(text)
        self.assertEqual(normalized, 'Foo, int, 1, 0, 10, 1, 0.002\nBar, int, 2, 0, 10, 1, 0.002\n')
        self.assertEqual(self.mod.normalize_params_text(''), '')


if __name__ == '__main__':
    unittest.main()
