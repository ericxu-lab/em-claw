#!/bin/bash
# Switch CryoSPARC v4.7.1 command_vis from the Flask dev server to gunicorn,
# with worker recycling so the RSS leak (8.9 GB over 90 days) can't accumulate.
#
# RUN AS YOUR_CRYOSPARC_ADMIN_ACCOUNT ON YOUR_CRYOSPARC_MASTER_HOST.  Edits config only -- does NOT restart anything.
# Apply with:  cryosparcm restart command_vis
#
# Reverse with: cp <the .bak file printed below> supervisord.conf && cryosparcm restart command_vis

set -euo pipefail

M="${CRYOSPARC_MASTER_DIR:-YOUR_CRYOSPARC_MASTER_PATH}"
PY=$M/deps/anaconda/envs/cryosparc_master_env/bin/python3.10
STAMP=$(date +%Y%m%d-%H%M%S)

[ -w "$M/supervisord.conf" ] || { echo "ERROR: run this as the CryoSPARC admin account (supervisord.conf not writable)"; exit 1; }

cp -p "$M/supervisord.conf" "$M/supervisord.conf.bak-$STAMP"
echo "backup: $M/supervisord.conf.bak-$STAMP"

# 1. command_vis-ONLY gunicorn config.  Deliberately separate from gunicorn.conf.py:
#    that file is shared with command_core (the scheduler) and command_rtp, and we do
#    NOT want to recycle the scheduler's worker mid-job.
cat > "$M/gunicorn_vis.conf.py" <<'EOF'
from gunicorn import glogging
from cryosparc_compute.jobs.common import LOG_FMT


class Logger(glogging.Logger):
    error_fmt = LOG_FMT
    datefmt = r"%Y-%m-%d %H:%M:%S"
    access_fmt = "%(message)s"


workers = 1
threads = 8
worker_class = "gthread"
logger_class = Logger

# command_vis streams large blobs (gridfs assets, map/particle-set saves). The Flask dev
# server had no request timeout at all, so 120s (the shared default) would be a REGRESSION
# and would kill big external-job saves mid-write.
timeout = 600
graceful_timeout = 120

# The actual leak defence: respawn the worker periodically. Without this, switching to
# gunicorn just changes which server leaks.
max_requests = 2000
max_requests_jitter = 200
EOF
echo "wrote:  $M/gunicorn_vis.conf.py"

# 2. Point [program:command_vis] at gunicorn instead of flask.
"$PY" - "$M/supervisord.conf" <<'EOF'
import re, sys

path = sys.argv[1]
src = open(path).read()

old = ('command=flask --app "cryosparc_command.command_vis" run -h "0.0.0.0" '
       '-p %(ENV_CRYOSPARC_COMMAND_VIS_PORT)s --with-threads')
new = ('command=gunicorn -n command_vis -b "0.0.0.0:%(ENV_CRYOSPARC_COMMAND_VIS_PORT)s" '
       '"cryosparc_command.command_vis:app" -c "%(ENV_CRYOSPARC_ROOT_DIR)s/gunicorn_vis.conf.py"')

if new in src:
    print("already patched, nothing to do")
    sys.exit(0)
if src.count(old) != 1:
    print(f"ERROR: expected exactly 1 match for the flask command line, found {src.count(old)}.")
    print("Config differs from what was inspected -- patch NOT applied.")
    sys.exit(1)

open(path, "w").write(src.replace(old, new))
print("patched [program:command_vis]")
EOF

echo
echo "--- resulting block ---"
grep -A4 'program:command_vis' "$M/supervisord.conf"
echo
echo "Config changed. Nothing restarted yet. To apply:  cryosparcm restart command_vis"
