from contextlib import nullcontext

from django.core.management.base import BaseCommand, CommandError

from OpenBench.models import Test
from OpenBench.ordo import RATING_TEST_MODES, format_pgn, player_key, synthetic_games


class Command(BaseCommand):

    help = 'Export aggregate SPRT/GAMES results as a synthetic PGN for Ordo'

    def add_arguments(self, parser):
        parser.add_argument(
            '--output', default='-',
            help='Output path, or - for stdout (default: -)')
        parser.add_argument(
            '--active-only', action='store_true',
            help='Exclude workloads hidden by logical deletion')
        parser.add_argument(
            '--test-id', action='append', type=int, dest='test_ids',
            help='Export only this test id; may be specified more than once')
        parser.add_argument(
            '--time-control-override', action='append', default=[],
            metavar='TEST_ID=CONTROL',
            help='Override only the analysis metadata for one test')

    def handle(self, *args, **options):
        tests = Test.objects.filter(
            games__gt=0,
            test_mode__in=RATING_TEST_MODES,
        ).select_related('dev', 'base').order_by('id')

        if options['active_only']:
            tests = tests.filter(deleted=False)
        if options['test_ids']:
            tests = tests.filter(id__in=options['test_ids'])

        tests = list(tests)
        if options['test_ids']:
            found = {test.id for test in tests}
            missing = sorted(set(options['test_ids']) - found)
            if missing:
                raise CommandError(
                    'No exportable SPRT/GAMES result for test id(s): %s'
                    % ', '.join(map(str, missing)))

        time_control_overrides = {}
        for value in options['time_control_override']:
            try:
                test_id, control = value.split('=', 1)
                test_id = int(test_id)
            except (TypeError, ValueError):
                raise CommandError(
                    'Time-control override must use TEST_ID=CONTROL: %s' % value)
            if not control or test_id not in {test.id for test in tests}:
                raise CommandError(
                    'Time-control override refers to an unselected test: %s' % value)
            time_control_overrides[test_id] = control

        selfplay = [
            test for test in tests
            if player_key(test, 'dev') == player_key(test, 'base')
        ]

        output = options['output']
        context = (
            nullcontext(None)
            if output == '-'
            else open(output, 'w', encoding='utf-8', newline='\n')
        )

        game_count = 0
        with context as stream:
            # 大量出力時もPGN全体をメモリへ載せず、局単位で書く。
            for game in synthetic_games(tests, time_control_overrides):
                if output == '-':
                    self.stdout.write(format_pgn(game), ending='')
                else:
                    stream.write(format_pgn(game))
                game_count += 1

        deleted_tests = sum(test.deleted for test in tests if test not in selfplay)
        self.stderr.write(
            'Exported %d synthetic games from %d tests (%d deleted); '
            'skipped %d exact self-play tests / %d games.' % (
                game_count,
                len(tests) - len(selfplay),
                deleted_tests,
                len(selfplay),
                sum(test.games for test in selfplay),
            ))
