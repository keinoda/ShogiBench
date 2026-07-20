import os

from django.conf import settings

import OpenBench.utils

from OpenBench.models import PGN


def archive_path(test_id):
    return os.path.join(settings.MEDIA_ROOT, 'PGNs', '%d.pgn.tar' % test_id)


def archive_status(workload):
    """棋譜アーカイブの利用可否を、画面とAPIで同じ基準から返す。"""

    if workload.upload_pgns == 'FALSE':
        return 'disabled'

    if not workload.finished:
        return 'active'

    if OpenBench.utils.getRecentMachines().filter(workload=workload.id).exists():
        return 'waiting'

    if PGN.objects.filter(test_id=workload.id, processed=False).exists():
        return 'processing'

    return 'ready' if os.path.exists(archive_path(workload.id)) else 'missing'
