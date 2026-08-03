from types import SimpleNamespace
from unittest import TestCase as UnitTestCase
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from OpenBench.models import Engine, Profile, Test
from OpenBench.ratings import (
    AiKey,
    MatchScore,
    RatingInputError,
    analyze_tests,
    observation_from_test,
    parse_test_ids,
    solve_ratings,
)


def fake_test(test_id, dev_key, base_key, **overrides):
    defaults = {
        'id'                : test_id,
        'test_mode'         : 'SPRT',
        'games'             : 30,
        'losses'            : 5,
        'draws'             : 10,
        'wins'              : 15,
        'LL'                : 1,
        'LD'                : 2,
        'DD'                : 3,
        'DW'                : 4,
        'WW'                : 5,
        'dev'               : SimpleNamespace(name='dev-%d' % test_id, sha=dev_key.commit),
        'base'              : SimpleNamespace(name='base-%d' % test_id, sha=base_key.commit),
        'dev_network'       : dev_key.network,
        'base_network'      : base_key.network,
        'dev_display'       : '',
        'base_display'      : '',
        'dev_netname'       : '',
        'base_netname'      : '',
        'dev_build_args'    : '',
        'base_build_args'   : '',
        'dev_options'       : 'Threads=1 Hash=64',
        'base_options'      : 'Threads=1 Hash=64',
        'dev_time_control'  : '8.0+0.08',
        'base_time_control' : '8.0+0.08',
        'dev_ponder_mode'   : 'off',
        'base_ponder_mode'  : 'off',
        'book_name'         : 'book.epd',
        'scale_method'      : 'BASE',
        'scale_nps'         : 0,
        'win_adj'           : 'movecount=3 score=400',
        'draw_adj'          : 'movenumber=40 movecount=8 score=10',
        'syzygy_wdl'        : 'OPTIONAL',
        'syzygy_adj'        : 'OPTIONAL',
        'deleted'           : False,
        'finished'          : True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class RatingCoreTests(UnitTestCase):

    def test_parse_test_ids_accepts_common_separators_and_removes_duplicates(self):
        self.assertEqual(parse_test_ids('103, 109\n110 103'), [103, 109, 110])

    def test_known_ordo_fixture_matches_without_expanding_games(self):
        matches = (
            MatchScore(1, 2, 2000, 836 + 212 / 2.0),
            MatchScore(0, 2, 6220, 2939 + 625 / 2.0),
            MatchScore(0, 1, 20062, 8227 + 3592 / 2.0),
        )
        ratings = solve_ratings(3, matches)

        self.assertAlmostEqual(ratings[0], 3.3207, places=3)
        self.assertAlmostEqual(ratings[1], 1.0361, places=3)
        self.assertAlmostEqual(ratings[2], -4.3568, places=3)
        self.assertAlmostEqual(float(ratings.mean()), 0.0, places=10)

    def test_input_order_does_not_change_ratings(self):
        matches = (
            MatchScore(0, 1, 200, 110),
            MatchScore(1, 2, 200, 120),
            MatchScore(0, 2, 200, 125),
        )
        forward = solve_ratings(3, matches)
        reverse = solve_ratings(3, reversed(matches))

        for first, second in zip(forward, reverse):
            self.assertAlmostEqual(first, second, places=10)

    def test_wdl_and_ptnml_mismatch_is_rejected(self):
        dev = AiKey('NET-A', 'a' * 40)
        base = AiKey('NET-B', 'b' * 40)
        test = fake_test(1, dev, base, wins=14, losses=6)

        with self.assertRaisesRegex(RatingInputError, 'W/D/LとPtnml'):
            observation_from_test(test)

    def test_disconnected_graph_is_rejected_with_components(self):
        keys = [
            AiKey('NET-%d' % index, str(index) * 40)
            for index in range(4)
        ]
        tests = (
            fake_test(1, keys[0], keys[1]),
            fake_test(2, keys[2], keys[3]),
        )

        with self.assertRaisesRegex(RatingInputError, '連結していません'):
            analyze_tests(tests, bootstrap_samples=16)

    def test_bootstrap_uses_ptnml_and_is_reproducible(self):
        keys = [
            AiKey('NET-%d' % index, str(index) * 40)
            for index in range(3)
        ]
        tests = (
            fake_test(1, keys[0], keys[1]),
            fake_test(2, keys[1], keys[2]),
            fake_test(3, keys[0], keys[2]),
        )

        first = analyze_tests(tests, bootstrap_samples=64, seed=7)
        second = analyze_tests(reversed(tests), bootstrap_samples=64, seed=7)

        self.assertEqual(first.players, second.players)
        self.assertEqual(first.cfs, second.cfs)
        self.assertEqual(first.bootstrap_samples, 64)


class RatingViewTests(TestCase):

    def setUp(self):
        user = User.objects.create_user(username='ratings-user', password='password')
        Profile.objects.create(user=user, enabled=True)
        self.client.login(username='ratings-user', password='password')
        self.engines = [
            Engine.objects.create(
                name='AI-%d' % index,
                source='https://example.com/%d' % index,
                sha=str(index) * 40,
                bench=1,
            )
            for index in range(3)
        ]

    def make_test(self, dev_index, base_index, **overrides):
        defaults = {
            'author'            : 'tester',
            'dev'               : self.engines[dev_index],
            'base'              : self.engines[base_index],
            'dev_repo'          : 'https://example.com/dev',
            'base_repo'         : 'https://example.com/base',
            'dev_engine'        : 'YaneuraOu-nagisa',
            'base_engine'       : 'YaneuraOu-nagisa',
            'dev_options'       : 'Threads=1 Hash=64',
            'base_options'      : 'Threads=1 Hash=64',
            'dev_network'       : 'NET-%d' % dev_index,
            'base_network'      : 'NET-%d' % base_index,
            'dev_netname'       : 'Network %d' % dev_index,
            'base_netname'      : 'Network %d' % base_index,
            'dev_time_control'  : '8.0+0.08',
            'base_time_control' : '8.0+0.08',
            'book_name'         : 'book.epd',
            'test_mode'         : 'SPRT',
            'games'             : 30,
            'losses'            : 5,
            'draws'             : 10,
            'wins'              : 15,
            'LL'                : 1,
            'LD'                : 2,
            'DD'                : 3,
            'DW'                : 4,
            'WW'                : 5,
            'finished'          : True,
        }
        defaults.update(overrides)
        return Test.objects.create(**defaults)

    def test_ratings_page_calculates_without_pgn(self):
        tests = (
            self.make_test(0, 1),
            self.make_test(1, 2),
            self.make_test(0, 2),
        )
        query = ','.join(str(test.id) for test in tests)

        with patch('OpenBench.ratings.BOOTSTRAP_SAMPLES', 64):
            response = self.client.get('/ratings/', {'tests': query})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '棋譜や合成PGNは使用しません')
        self.assertContains(response, '相対レーティング')
        self.assertContains(response, '優越確率')
        self.assertContains(response, '[1, 2, 3, 4, 5]')

    def test_ratings_page_rejects_missing_test(self):
        response = self.client.get('/ratings/', {'tests': '999999'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '存在しないテスト番号があります')

    def test_ratings_page_warns_but_calculates_for_condition_difference(self):
        tests = (
            self.make_test(0, 1),
            self.make_test(1, 2, book_name='other.epd'),
            self.make_test(0, 2),
        )
        query = ','.join(str(test.id) for test in tests)

        with patch('OpenBench.ratings.BOOTSTRAP_SAMPLES', 64):
            response = self.client.get('/ratings/', {'tests': query})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '条件差は補正せず')
        self.assertContains(response, '開始局面集')
        self.assertContains(response, '相対レーティング')
