#!/usr/bin/env bash
# Resume the baseline harvest. Safe to run daily -- it skips what already
# landed and stops cleanly when OpenSky's allowance runs out.
#
# Only the airports OpenSky can actually see. Measured over a real 48h ingest,
# Kuwait, Riyadh, Jeddah, Erbil, Abha, Baghdad and Tehran returned zero flights
# each; harvesting them costs about half the daily allowance and returns
# nothing. AirLabs schedule data covers those instead.
set -euo pipefail
cd "$(dirname "$0")"

export OPENSKY_CLIENT_ID="$(python3 -c 'import json;print(json.load(open("credentials.json"))["clientId"])')"
export OPENSKY_CLIENT_SECRET="$(python3 -c 'import json;print(json.load(open("credentials.json"))["clientSecret"])')"

# Exit 2 means "stopped early, resume tomorrow", which is the expected outcome
# most days -- don't let set -e treat it as a failure before the summary runs.
rc=0
# The airport list is the default now, derived from schedules.BLIND, so it is
# not spelled out here as well; the dates come from config.
python3 -m src.backfill || rc=$?

python3 - <<'PY'
from src import db
conn = db.connect()
n = conn.execute("SELECT COUNT(*) n FROM backfill_progress").fetchone()["n"]
b = conn.execute("SELECT COUNT(*) n FROM baseline").fetchone()["n"]
print(f"\nilerleme: {n}/104 dilim indi | baseline: {b} rota")
if b:
    print("BASELINE HAZIR -- artik frekans karsilastirmasi yapilabilir:")
    print("  python3 -m src.report")
PY
exit $rc
