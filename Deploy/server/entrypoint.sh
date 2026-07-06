#!/bin/sh

set -e

python manage.py migrate --noinput

# The Artifact/PGN watcher threads live inside the gunicorn workers, and a
# lockfile in the working directory guarantees only one set runs per host.
#
# Threaded workers keep the app responsive during long requests: an upload
# of a 200MB network can take upwards of 30 minutes on a slow uplink, and
# sync workers would both block the app and be killed by the timeout.
exec gunicorn OpenSite.wsgi:application \
    --bind "0.0.0.0:${PORT:-8080}" \
    --workers "${WEB_CONCURRENCY:-2}" \
    --worker-class gthread \
    --threads "${GUNICORN_THREADS:-4}" \
    --timeout 300 \
    --access-logfile -
