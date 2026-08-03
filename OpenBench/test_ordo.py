import os
import tempfile

from django.core.management import call_command
from django.test import TestCase

from OpenBench.models import Engine, Test
from OpenBench.ordo import (
    InconsistentResultError,
    player_key,
    reconstructed_pairs,
    synthetic_games,
)


class OrdoExportTests(TestCase):

    def make_test(self, deleted=False, dev_sha='a', base_sha='b', **results):
        dev = Engine.objects.create(
            name='dev-branch', source='dev-source', sha=dev_sha * 40, bench=1)
        base = Engine.objects.create(
            name='base-branch', source='base-source', sha=base_sha * 40, bench=1)
        defaults = {
            'games'  : 30,
            'losses' : 5,
            'draws'  : 10,
            'wins'   : 15,
            'LL'     : 1,
            'LD'     : 2,
            'DD'     : 3,  # 2組の引分2局と1組の1勝1敗
            'DW'     : 4,
            'WW'     : 5,
        }
        defaults.update(results)
        return Test.objects.create(
            author='tester',
            dev=dev,
            base=base,
            dev_repo='https://example.com/dev',
            base_repo='https://example.com/base',
            dev_engine='YaneuraOu-nagisa',
            base_engine='YaneuraOu-nagisa',
            dev_display='候補',
            base_display='基準',
            dev_options='Threads=1 Hash=64',
            base_options='Threads=1 Hash=64',
            dev_network='DEVNET01',
            base_network='BASENET1',
            dev_netname='dev-net',
            base_netname='base-net',
            dev_time_control='8.0+0.08',
            base_time_control='8.0+0.08',
            book_name='book.epd',
            test_mode='SPRT',
            deleted=deleted,
            **defaults)

    def test_central_bucket_is_split_by_trinomial_counts(self):
        test = self.make_test()
        pairs = list(reconstructed_pairs(test))

        self.assertEqual(len(pairs), 15)
        self.assertEqual(sum(pair == ('D', 'D') for pair in pairs), 2)
        self.assertEqual(sum(set(pair) == {'L', 'W'} for pair in pairs), 1)

        outcomes = [outcome for pair in pairs for outcome in pair]
        self.assertEqual(outcomes.count('L'), test.losses)
        self.assertEqual(outcomes.count('D'), test.draws)
        self.assertEqual(outcomes.count('W'), test.wins)

    def test_synthetic_games_balance_both_roles(self):
        test = self.make_test()
        games = list(synthetic_games([test]))
        dev_name = games[0].white

        self.assertEqual(len(games), test.games)
        self.assertEqual(sum(game.white == dev_name for game in games), test.games // 2)
        self.assertEqual(sum(game.black == dev_name for game in games), test.games // 2)
        self.assertEqual(
            {game.result for game in games},
            {'1-0', '0-1', '1/2-1/2'})

    def test_inconsistent_aggregates_are_rejected(self):
        test = self.make_test(games=28)
        with self.assertRaisesRegex(InconsistentResultError, 'Test %d' % test.id):
            list(reconstructed_pairs(test))

    def test_command_includes_deleted_and_skips_exact_selfplay(self):
        deleted = self.make_test(deleted=True)
        selfplay = self.make_test(dev_sha='c', base_sha='c')
        selfplay.base_repo = selfplay.dev_repo
        selfplay.base_engine = selfplay.dev_engine
        selfplay.base_network = selfplay.dev_network
        selfplay.base_build_args = selfplay.dev_build_args
        selfplay.save()
        self.assertEqual(player_key(selfplay, 'dev'), player_key(selfplay, 'base'))

        handle, path = tempfile.mkstemp(suffix='.pgn')
        os.close(handle)
        try:
            call_command('export_ordo', output=path)
            with open(path, encoding='utf-8') as pgn:
                data = pgn.read()
        finally:
            os.remove(path)

        self.assertEqual(data.count('[Event "ShogiBench reconstructed result"]'), deleted.games)
        self.assertIn('[ShogiBenchDeleted "1"]', data)
        self.assertIn('dev-branch / dev-net [', data)
        self.assertNotIn('候補 [', data)

    def test_active_only_excludes_deleted_tests(self):
        self.make_test(deleted=True)

        handle, path = tempfile.mkstemp(suffix='.pgn')
        os.close(handle)
        try:
            call_command('export_ordo', output=path, active_only=True)
            self.assertEqual(os.path.getsize(path), 0)
        finally:
            os.remove(path)

    def test_time_control_override_preserves_recorded_value(self):
        test = self.make_test()
        test.dev_time_control = test.base_time_control = '40.0+0.40'
        test.save()

        handle, path = tempfile.mkstemp(suffix='.pgn')
        os.close(handle)
        try:
            call_command(
                'export_ordo',
                output=path,
                test_ids=[test.id],
                time_control_override=['%d=8.0+0.08' % test.id])
            with open(path, encoding='utf-8') as pgn:
                first_game = pgn.read().split('[Event ', 2)[1]
        finally:
            os.remove(path)

        self.assertIn('[ShogiBenchTimeControl "8.0+0.08"]', first_game)
        self.assertIn('[ShogiBenchRecordedTimeControl "40.0+0.40"]', first_game)

    def test_runtime_hash_does_not_split_the_same_player(self):
        test = self.make_test()
        test.base_repo = test.dev_repo
        test.base_engine = test.dev_engine
        test.base.sha = test.dev.sha
        test.base.save()
        test.base_network = test.dev_network
        test.base_build_args = test.dev_build_args
        test.dev_options = 'Threads=1 Hash=64'
        test.base_options = 'Threads=1 Hash=256'
        test.save()

        self.assertEqual(player_key(test, 'dev'), player_key(test, 'base'))
