#!/bin/bash

# Example "On-start Script" for a vast.ai template.
#
# Set the OPENBENCH_* variables in the template's Environment Variables
# (preferred, so the key is not stored in the script), or uncomment below:
#
#   export OPENBENCH_SERVER=https://shogibench.fly.dev
#   export OPENBENCH_USERNAME=youruser
#   export OPENBENCH_PASSWORD=<worker key token from /workers/>
#
# Optional: export SHOGIBENCH_THREADS=$(($(nproc) - 1))

REPO_RAW=https://raw.githubusercontent.com/keinoda/ShogiBench/shogi/Deploy/worker

curl -sSL "$REPO_RAW/setup_worker.sh" -o /root/setup_worker.sh
chmod +x /root/setup_worker.sh
nohup /root/setup_worker.sh > /root/shogibench-worker.log 2>&1 &
