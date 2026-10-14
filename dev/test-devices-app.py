#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""/api/v1/devices lists the My Phone app's line ("<sip_username>-app") under
its owner, in-process on a throwaway DB.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-devices-app.py      # exits non-zero on failure
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-api"))
import app as A
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, x); ok or fails.append(n)

A._asterisk_status = lambda: {"contacts": [
    {"aor": "phone8800", "ip": "192.168.8.110", "port": "5060", "transport": "", "status": "Avail", "rtt_ms": 28.5},
    {"aor": "phone8800-app", "ip": "172.56.20.9", "port": "40211", "transport": "tls", "status": "Avail", "rtt_ms": 91.0},
    {"aor": "nobody-app", "ip": "10.0.0.9", "port": "5060", "transport": "", "status": "Avail", "rtt_ms": None},
]}
with A.db() as c:
    admin = dict(c.execute("SELECT id, role FROM logins WHERE role='admin' LIMIT 1").fetchone())
    owner = dict(c.execute("SELECT id, role FROM logins WHERE sip_username='phone8800'").fetchone())
    other = dict(c.execute("SELECT id, role FROM logins WHERE sip_username='phone8801'").fetchone())

A._v1_actor = lambda req: admin
r = A.v1_devices(None)
by = {d["sip_username"]: d for d in r["devices"]}
check("desk phone listed", by.get("phone8800", {}).get("kind") == "phone", by.keys())
app = by.get("phone8800-app", {})
check("app line listed under its owner", app.get("kind") == "app" and app.get("exten") == "8800", app)
check("app shows as external via edge", app.get("location") == "external" and "edge" in app.get("via", ""), app)
check("unknown -app ignored", "nobody-app" not in by)
check("counts", r["counts"] == {"internal": 1, "external": 1}, r["counts"])
A._v1_actor = lambda req: owner
check("owner sees both", len(A.v1_devices(None)["devices"]) == 2)
A._v1_actor = lambda req: other
check("other user sees none", A.v1_devices(None)["devices"] == [])
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
