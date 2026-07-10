#!/bin/python3

# .tune の照合 (check_contexts) / 自動追随 (retune) / .params 生成
# (params_from_tune) のテスト。fuuppi-spsa の check_contexts.py / retune.py の
# 移植が意図どおり動くことを、合成した小さな .tune + ソースで確認する

import importlib
import os
import sys
import unittest

PARENT     = os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir))
CLIENT_DIR = os.path.join(PARENT, 'Client')


def import_yotune():
    sys.path.insert(0, CLIENT_DIR)
    return importlib.import_module('yotune')


TUNE = '''#set file engine\\search.cpp
#set declaration %%TUNE_DECLARATION%%
#set options %%TUNE_OPTIONS%%
#context futility

if (eval >= beta + 100@ * depth - 25@2)
    return eval;

#context nullmove

value = eval - 390@ - 20@m * depth;

#add %%TUNE_ISREADY%%

    nullmove_1 = @1;

#context clear

h.fill(-523@);

#context razoring

if (eval < alpha - 500@)
    continue;
'''

SOURCE_EXACT = '''
// search body
if (eval >= beta + 100 * depth - 25)
    return eval;

value = eval - 390 - 20 * depth;

h.fill(-523);

if (eval < alpha - 500)
    continue;

%%TUNE_DECLARATION%%
%%TUNE_OPTIONS%%
%%TUNE_ISREADY%%
'''

# futility の 100 -> 120, razoring の 500 -> 512 に定数ドリフト
SOURCE_DRIFT = SOURCE_EXACT.replace('100 * depth', '120 * depth').replace('- 500)', '- 512)')

# nullmove の構造が変わった (項が増えた)
SOURCE_STRUCT = SOURCE_EXACT.replace(
    'value = eval - 390 - 20 * depth;',
    'value = eval - 390 - 20 * depth + improving * 3;')


class CheckContextsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.yo = import_yotune()

    def test_parse_blocks_and_files(self):

        blocks = list(self.yo.iter_context_blocks(TUNE))
        self.assertEqual([b['name'] for b in blocks], ['futility', 'nullmove', 'clear', 'razoring'])
        self.assertTrue(all(b['file'] == 'engine/search.cpp' for b in blocks))
        self.assertEqual(self.yo.tune_files(TUNE), ['engine/search.cpp'])

        # #add 以降の本文は context に含まれない
        nullmove = blocks[1]['body']
        self.assertIn('390@', nullmove)
        self.assertNotIn('TUNE_ISREADY', nullmove)

    def test_markers(self):

        markers = { m['marker'] for m in self.yo.iter_markers(TUNE) }
        self.assertEqual(markers, { '%%TUNE_DECLARATION%%', '%%TUNE_OPTIONS%%', '%%TUNE_ISREADY%%' })

        results = self.yo.check_markers(TUNE, { 'engine/search.cpp' : SOURCE_EXACT })
        self.assertTrue(all(r['status'] == self.yo.EXACT for r in results))

        # マーカーの無いソース (上流の素の YaneuraOu 等) は MISSING
        results = self.yo.check_markers(TUNE, { 'engine/search.cpp' : 'no markers here' })
        self.assertTrue(all(r['status'] == self.yo.MISSING for r in results))

    def by_name(self, results):
        return { r['name'] : r for r in results }

    def test_all_exact(self):

        results = self.yo.check_contexts(TUNE, { 'engine/search.cpp' : SOURCE_EXACT })
        counts  = self.yo.summarize_check(results)
        self.assertEqual(counts, { 'EXACT' : 4, 'NUMDRIFT' : 0, 'MISSING' : 0 })

    def test_numdrift_reports_value_changes(self):

        results = self.by_name(self.yo.check_contexts(TUNE, { 'engine/search.cpp' : SOURCE_DRIFT }))

        self.assertEqual(results['futility']['status'], self.yo.NUMDRIFT)
        self.assertIn('100->120', results['futility']['detail'])

        self.assertEqual(results['razoring']['status'], self.yo.NUMDRIFT)
        self.assertIn('500->512', results['razoring']['detail'])

        self.assertEqual(results['nullmove']['status'], self.yo.EXACT)

    def test_missing_on_structure_change(self):

        results = self.by_name(self.yo.check_contexts(TUNE, { 'engine/search.cpp' : SOURCE_STRUCT }))
        self.assertEqual(results['nullmove']['status'], self.yo.MISSING)
        self.assertEqual(results['futility']['status'], self.yo.EXACT)

    def test_missing_source_file(self):

        results = self.yo.check_contexts(TUNE, {})
        self.assertTrue(all(r['status'] == self.yo.MISSING for r in results))
        self.assertIn('ソースファイルがありません', results[0]['detail'])


class RetuneTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.yo = import_yotune()

    def test_retune_rewrites_drifted_blocks(self):

        new_text, report = self.yo.retune(TUNE, { 'engine/search.cpp' : SOURCE_DRIFT })

        self.assertEqual(report['exact'], ['nullmove', 'clear'])
        self.assertEqual(report['auto'], ['futility', 'razoring'])
        self.assertEqual(report['manual'], [])

        # 新しい数値に @ マーカーが同じ位置で付け直されている
        self.assertIn('120@ * depth - 25@2', new_text)
        self.assertIn('512@', new_text)

        # #add ブロックや無関係の行はそのまま
        self.assertIn('nullmove_1 = @1;', new_text)
        self.assertIn('#set declaration %%TUNE_DECLARATION%%', new_text)

        # 追随後は全 EXACT になる
        results = self.yo.check_contexts(new_text, { 'engine/search.cpp' : SOURCE_DRIFT })
        self.assertEqual(self.yo.summarize_check(results),
                         { 'EXACT' : 4, 'NUMDRIFT' : 0, 'MISSING' : 0 })

    def test_retune_leaves_structural_changes_manual(self):

        new_text, report = self.yo.retune(TUNE, { 'engine/search.cpp' : SOURCE_STRUCT })

        self.assertIn('nullmove', [name for name, why in report['manual']])
        # 手当てが必要なブロックの本文は無変更
        self.assertIn('390@', new_text)


class ParamsFromTuneTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.yo = import_yotune()

    def test_fresh_generation(self):

        text, report = self.yo.params_from_tune(TUNE)
        rows = self.yo.parse_params_entries(text)
        by_name = { r['name'] : r for r in rows }

        # 名前: prefix + suffix (省略時は連番)
        self.assertEqual([r['name'] for r in rows],
                         ['futility_1', 'futility_2', 'nullmove_1', 'nullmove_m', 'clear_1', 'razoring_1'])

        # tune.py と同じ既定値: 値=contextの数値、レンジ=0..2倍
        self.assertEqual(by_name['futility_1']['value'], 100.0)
        self.assertEqual((by_name['futility_1']['min'], by_name['futility_1']['max']), (0.0, 200.0))
        self.assertEqual(by_name['futility_1']['step'], 10.0)
        self.assertEqual(by_name['futility_1']['delta'], 0.002)

        # 'eval - 390@' の 390 は式の一部の正値 (tune.py の解釈と同じ)
        self.assertEqual(by_name['nullmove_1']['value'], 390.0)
        self.assertEqual((by_name['nullmove_1']['min'], by_name['nullmove_1']['max']), (0.0, 780.0))

        # '-523@' のように負号が隣接する場合は負値としてレンジも反転する
        self.assertEqual(by_name['clear_1']['value'], -523.0)
        self.assertEqual((by_name['clear_1']['min'], by_name['clear_1']['max']), (-1046.0, 0.0))

        self.assertEqual(len(report['added']), 6)

    def test_merge_keeps_existing_and_retires_removed(self):

        existing = ('futility_1, int, 123, 50, 250, 8, 0.002 // 前回の到達値\n'
                    'obsolete_1, int, 7, 0, 14, 1, 0.002\n')

        text, report = self.yo.params_from_tune(TUNE, existing)
        by_name = { r['name'] : r for r in self.yo.parse_params_entries(text) }

        # 既存行の値・レンジ・コメントは保持される (継続チューニング)
        self.assertEqual(by_name['futility_1']['value'], 123.0)
        self.assertEqual(by_name['futility_1']['max'], 250.0)
        self.assertEqual(by_name['futility_1']['comment'], '前回の到達値')

        # .tune から消えた行は NOT USED で残る
        self.assertTrue(by_name['obsolete_1']['not_used'])
        self.assertIn('obsolete_1', report['retired'])

        # 新しいパラメータは追加される
        self.assertIn('razoring_1', by_name)
        self.assertIn('razoring_1', report['added'])

    def test_repo_fixture_suisho10_roundtrip(self):

        # リポジトリ同梱の実キット (Scripts/tune/suisho10.tune + .params) で、
        # .tune から列挙したパラメータ名が .params と一致することを確認する
        tune_path   = os.path.join(PARENT, 'Scripts', 'tune', 'suisho10.tune')
        params_path = os.path.join(PARENT, 'Scripts', 'tune', 'suisho10.params')

        with open(tune_path, encoding='utf-8') as fin:
            tune_text = fin.read()
        with open(params_path, encoding='utf-8') as fin:
            params_text = fin.read()

        tune_names  = [name for name, value in self.yo.tune_param_names(tune_text)]
        param_names = [r['name'] for r in self.yo.parse_params_entries(params_text)]

        # .tune のパラメータは全て .params にある
        self.assertTrue(len(tune_names) >= 50)
        self.assertTrue(set(tune_names) <= set(param_names))

        # この fixture の .params には .tune から消えた行が 3 つ残っている
        # (YaneuraOuWorker_clear1_*)。同期するとそれらが NOT USED になり、
        # 生きている行は値そのままで維持される
        stale = set(param_names) - set(tune_names)
        self.assertEqual(stale, { 'YaneuraOuWorker_clear1_1',
                                  'YaneuraOuWorker_clear1_2',
                                  'YaneuraOuWorker_clear1_3' })

        text, report = self.yo.params_from_tune(tune_text, params_text)
        self.assertEqual(report['added'], [])
        self.assertEqual(set(report['retired']), stale)
        self.assertEqual(report['kept'], len(tune_names))

        synced = { r['name'] : r for r in self.yo.parse_params_entries(text) }
        self.assertTrue(all(synced[name]['not_used'] for name in stale))
        self.assertAlmostEqual(synced[tune_names[0]]['value'],
                               self.yo.parse_params_entries(params_text)[0]['value'], places=3)


if __name__ == '__main__':
    unittest.main()
