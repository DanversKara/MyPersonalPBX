#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Sending a fax end to end on a REAL local Asterisk with fax support and ARI
(user pbxbrain / $ARI_PASS on 127.0.0.1:8088), our extensions.conf plus two
test contexts:
    [test-receiver] exten => _X.,1,Answer() / ReceiveFAX(<dir>/got-${EXTEN}.tif)
    [test-busy]     exten => _X.,1,Busy(3)
Runs pbx-agi and pbx-brain against a throwaway DB, queues faxes the way the
app does (fax_send.queue), and checks: sent, pages arrive, billed as minutes;
a busy line retries then fails.

    export PBX_DB=/tmp/apptest.db ARI_PASS=... FAX_GOT_DIR=... && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-fax-send-e2e.py
"""
import os, sys, sqlite3, subprocess, time, tempfile
here = os.path.dirname(os.path.abspath(__file__))
FAXDIR = os.environ.setdefault("PBX_FAX_DIR", "/var/spool/pbx/fax")
GOT = os.environ["FAX_GOT_DIR"]
sys.path.insert(0, os.path.join(here, "..", "pbx-api"))
import app as A, fax_send
A.init_db()
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)
DBP = os.environ["PBX_DB"]
d = sqlite3.connect(DBP); d.row_factory = sqlite3.Row
d.execute("INSERT INTO inbound_routes (did, ring_extens) VALUES ('5550001111','[\"8800\"]')")
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('default_quota_call_minutes','100')")
d.commit()
A._asterisk_status = lambda: {"contacts": [], "fax_ready": True}
fax_send.install(A)

brain_dir = os.path.join(here, "..", "pbx-brain")
env = dict(os.environ, PBX_STATUS_PORT="8199")
agi = subprocess.Popen([sys.executable, "agi_server.py"], cwd=brain_dir, env=env,
                       stdout=open(os.path.join(GOT, "agi.log"), "w"), stderr=subprocess.STDOUT)
runner = (
    "import sys; sys.path.insert(0, '.'); import app, faxout\n"
    "faxout.RETRY_SECS = 3; faxout.MAX_ATTEMPTS = 2\n"
    "faxout.endpoint_for = lambda n: 'Local/%s@%s' % (n, 'test-busy' if n.endswith('0000') else 'test-receiver')\n"
    "app.main()\n")
brain = subprocess.Popen([sys.executable, "-c", runner], cwd=brain_dir, env=env,
                         stdout=open(os.path.join(GOT, "brain.log"), "w"), stderr=subprocess.STDOUT)
time.sleep(4)
me = dict(d.execute("SELECT * FROM logins WHERE exten='8800'").fetchone())
pdf = os.path.join(GOT, "doc.pdf")
subprocess.run(["gs", "-q", "-sDEVICE=pdfwrite", "-o", pdf, "-c",
                "/Helvetica-Bold findfont 36 scalefont setfont 72 650 moveto (Invoice 1042) show "
                "/Helvetica findfont 18 scalefont setfont 72 600 moveto (Sent from the own-pbx fax queue) show showpage"], check=True)
try:
    fid, err = fax_send.queue(me, open(pdf, "rb").read(), "invoice.pdf", "(310) 555-0100", "5550001111",
                              to_name="Acme Billing", subject="Invoice 1042", note="Please see the attached invoice.", cover=True)
    check("queued", fid and not err, err)
    check("pages counted (cover + 1)", d.execute("SELECT pages FROM fax_outbox WHERE id=?", (fid,)).fetchone()[0] == 2)
    st = None
    for _ in range(90):
        st = d.execute("SELECT status, attempts, pages_sent, last_error FROM fax_outbox WHERE id=?", (fid,)).fetchone()
        if st["status"] in ("sent", "failed"):
            break
        time.sleep(1)
    check("sent", st["status"] == "sent" and st["pages_sent"] == 2, dict(st))
    got = os.path.join(GOT, "got-13105550100.tif")
    check("the far end received both pages", os.path.isfile(got) and
          subprocess.run(["tiffinfo", got], capture_output=True, text=True).stdout.count("TIFF Directory") == 2)
    cdr = d.execute("SELECT * FROM cdr WHERE call_id LIKE ?", (f"fax-{fid}-%",)).fetchone()
    check("billed as outside minutes", cdr and cdr["direction"] == "outbound" and cdr["login_id"] == me["id"] and cdr["bill_sec"] > 0)

    fid2, err = fax_send.queue(me, open(pdf, "rb").read(), "invoice.pdf", "3105550000", "5550001111", cover=False)
    for _ in range(60):
        st = d.execute("SELECT status, attempts, last_error FROM fax_outbox WHERE id=?", (fid2,)).fetchone()
        if st["status"] in ("sent", "failed"):
            break
        time.sleep(1)
    check("busy line: retried, then failed with a reason", st["status"] == "failed" and st["attempts"] == 2
          and "busy" in st["last_error"].lower(), dict(st))
    check("busy line: no minutes billed", d.execute("SELECT COUNT(*) FROM cdr WHERE call_id LIKE ?", (f"fax-{fid2}-%",)).fetchone()[0] == 0)
finally:
    brain.terminate(); agi.terminate()
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
