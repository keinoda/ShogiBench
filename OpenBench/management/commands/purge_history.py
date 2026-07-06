# Wipe the testing history — Tests, Results, Machines, PGNs and log
# events, plus their files under /Media/ — while keeping everything worth
# keeping: Users, Profiles, Worker Keys, Networks and their auxiliary
# files, and Build Variants. Engine rows here are per-commit snapshots
# referenced by Tests, not the engine configs (those live in Engines/*.json).
#
# >>> python manage.py purge_history          # dry run: show what would go
# >>> python manage.py purge_history --yes    # actually delete
#
# Run it while no test you care about is active: workers holding a
# deleted workload simply error once and re-register.

import glob
import os

from django.core.management.base import BaseCommand

from OpenBench.models import Engine, LogEvent, Machine, PGN, Profile, Result, Test
from OpenSite.settings import MEDIA_ROOT

class Command(BaseCommand):

    help = 'Delete all Tests, Results, Machines, PGNs and Log Events (keeps Users, Networks, Build Variants)'

    def add_arguments(self, parser):
        parser.add_argument('--yes', action='store_true', help='Actually delete; without this, only report')

    def handle(self, *args, **options):

        doit = options['yes']

        # Files referenced by rows, plus orphans matching their patterns.
        # Networks are bare <SHA8> files in /Media/ and never match these
        files  = set(glob.glob(os.path.join(MEDIA_ROOT, '*.pgn.bz2')))
        files |= set(glob.glob(os.path.join(MEDIA_ROOT, '*.log')))
        files |= set(glob.glob(os.path.join(MEDIA_ROOT, 'PGNs', '*')))

        for pgn in PGN.objects.all():
            files.add(os.path.join(MEDIA_ROOT, pgn.filename()))
        for event in LogEvent.objects.exclude(log_file=''):
            files.add(os.path.join(MEDIA_ROOT, event.log_file))

        files = { f for f in files if os.path.isfile(f) }

        plan = [
            ('Results',    Result),
            ('Tests',      Test),
            ('Engines (commit snapshots)', Engine),
            ('Machines',   Machine),
            ('Log Events', LogEvent),
            ('PGNs',       PGN),
        ]

        for label, model in plan:
            count = model.objects.count()
            self.stdout.write('%-28s %6d rows%s' % (label, count, '' if doit else ' (would delete)'))
            if doit:
                model.objects.all().delete()

        self.stdout.write('%-28s %6d files%s' % ('Media files', len(files), '' if doit else ' (would delete)'))
        if doit:
            for path in files:
                os.remove(path)

        profiles = Profile.objects.count()
        self.stdout.write('%-28s %6d rows reset (games/tests -> 0)' % ('Profiles', profiles))
        if doit:
            Profile.objects.all().update(games=0, tests=0)

        if not doit:
            self.stdout.write('\nDry run only. Re-run with --yes to delete.')
        else:
            self.stdout.write(self.style.SUCCESS('\nHistory purged. Networks, Build Variants and accounts are untouched.'))
