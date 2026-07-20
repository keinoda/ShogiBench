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

import os
import sys
import tarfile
import threading
import time
import traceback

from OpenBench.pgn_archive import archive_path
from OpenBench.models import PGN

from django.db import transaction, OperationalError
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage

class PGNWatcher(threading.Thread):

    def __init__(self, stop_event, *args, **kwargs):
        self.stop_event = stop_event
        super().__init__(*args, **kwargs)

    def process_pgn(self, pgn):

        with transaction.atomic():

            # 同じ差分の再送や複数workerからの処理が重なっても二重追加しない。
            pgn = PGN.objects.select_for_update().get(pk=pgn.pk)
            if pgn.processed:
                return

            tar_path = archive_path(pgn.test_id)
            pgn_path = FileSystemStorage().path(pgn.filename())

            # Ensure Media/PGNs exists
            dir_name = os.path.dirname(tar_path)
            os.makedirs(dir_name, exist_ok=True)

            # First PGN will create the initial .tar file
            mode = 'a' if os.path.exists(tar_path) else 'w'
            with tarfile.open(tar_path, mode) as tar:
                # tar追記後・DB更新前に落ちた場合も、同じメンバーを増やさない。
                if pgn.filename() not in tar.getnames():
                    tar.add(pgn_path, arcname=pgn.filename())

            # Delete the raw .pgn.bz2 file, and don't process it again
            FileSystemStorage().delete(pgn.filename())
            pgn.processed = True
            pgn.save()

    def run(self):
        while not self.stop_event.wait(timeout=15):

            try: # Never exit on errors, to keep the watcher alive
                for pgn in PGN.objects.filter(processed=False):
                    self.process_pgn(pgn)

            # Expect the database to be locked sometimes
            except OperationalError as error:
                if 'database is locked' not in str(error).lower():
                    traceback.print_exc()
                    sys.stdout.flush()

            except: # Totally unknown error
                traceback.print_exc()
                sys.stdout.flush()
