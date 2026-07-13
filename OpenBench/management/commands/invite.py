# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
#   OpenBench is a chess engine testing framework authored by Andrew Grant.   #
#   <https://github.com/AndyGrant/OpenBench>           <andrew@grantnet.us>   #
#                                                                             #
#   OpenBench is free software: you can redistribute it and/or modify         #
#   it under the terms of the GNU General Public License as published by      #
#   the Free Software Foundation, either version 3 of the License, or         #
#   (at your option) any later version.                                       #
#                                                                             #
#   OpenBench is distributed in the hope that it will be useful,              #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of            #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the             #
#   GNU General Public License for more details.                              #
#                                                                             #
#   You should have received a copy of the GNU General Public License         #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# Invite-only registration helper. Registration through the website is
# disabled via require_manual_registration, so administrators create
# accounts with this command instead:
#
# >>> python manage.py invite <username> [--email X] [--password X] [--approver]
#
# When no password is given, you are prompted to choose one. In
# non-interactive use a random password is generated and printed once.
# Users can change their password afterwards on the /profile/ page.

import getpass
import secrets
import sys

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from OpenBench.models import Profile

class Command(BaseCommand):

    help = 'Create an enabled user account (invite-only registration)'

    def add_arguments(self, parser):
        parser.add_argument('username', help='Django-compatible username')
        parser.add_argument('--email', default='', help='Email address (optional)')
        parser.add_argument('--password', default=None, help='Password. Randomly generated when omitted')
        parser.add_argument('--approver', action='store_true', help='Grant test-approval rights')

    def handle(self, *args, **options):

        username = options['username']

        try:
            User._meta.get_field('username').run_validators(username)
        except ValidationError as error:
            raise CommandError('Invalid username: %s' % ('; '.join(error.messages)))

        if User.objects.filter(username=username).exists():
            raise CommandError('User "%s" already exists' % (username))

        password  = options['password']
        generated = False

        # Ask for a password when run interactively; only fall back to a
        # random one when there is no terminal to ask on
        if not password and sys.stdin.isatty():
            for attempt in range(3):
                password = getpass.getpass('Password: ')
                confirm  = getpass.getpass('Password (again): ')
                if password and password == confirm:
                    break
                self.stderr.write('Passwords did not match (or were empty). Try again.')
                password = None

            if not password:
                raise CommandError('No password provided')

        if not password:
            password  = secrets.token_urlsafe(16)
            generated = True

        user = User.objects.create_user(username, options['email'], password)
        Profile.objects.create(user=user, enabled=True, approver=options['approver'])

        self.stdout.write(self.style.SUCCESS('Created user "%s"' % (username)))
        self.stdout.write('Username : %s' % (username))
        if generated:
            self.stdout.write('Password : %s (randomly generated)' % (password))
        self.stdout.write('Approver : %s' % (options['approver']))
        self.stdout.write('Passwords can be changed at /profile/, or with "manage.py changepassword <username>".')
