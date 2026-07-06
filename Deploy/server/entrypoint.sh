#!/bin/sh

set -e

python manage.py migrate --noinput

# The Artifact/PGN watcher threads live inside the gunicorn workers, and a
# lockfile in the working directory guarantees only one set runs per host.
exec gunicorn OpenSite.wsgi:application \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-2}" \
    --timeout 120 \
    --access-logfile -
