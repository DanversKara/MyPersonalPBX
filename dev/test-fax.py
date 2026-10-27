#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Fax on ordinary numbers (pbx-brain/fax.py) on a throwaway DB: per-number
listening time, who a fax belongs to (direct number / ring group), TIFF ->
PDF, emails (received and failed), and files outside the fax folder refused.
Needs ghostscript (gs) and tiff2pdf (libtiff-tools).

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-fax.py      # exits non-zero on failure
"""
import os, sys, json, sqlite3, subprocess, tempfile, time, threading
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "pbx-api"))
import app as A
A.init_db()
FAXDIR = tempfile.mkdtemp(prefix="faxdir-")
os.environ["PBX_FAX_DIR"] = FAXDIR
sys.path.insert(0, os.path.join(here, "..", "pbx-brain"))
import fax, mailer
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)
d = sqlite3.connect(os.environ["PBX_DB"]); d.row_factory = sqlite3.Row
d.execute("UPDATE logins SET vm_email='u8800@example.com' WHERE exten='8800'")
d.execute("UPDATE logins SET vm_email='u8801@example.com' WHERE exten='8801'")
gid = d.execute("INSERT INTO ring_groups (name, members) VALUES ('Front desk', '[\"8800\",\"8801\"]')").lastrowid
d.execute("INSERT INTO inbound_routes (did, ring_extens, fax_detect, fax_wait, fax_email) VALUES ('15550001111','[\"8800\"]',1,5,'office@example.com')")
d.execute("INSERT INTO inbound_routes (did, ring_extens, group_id, fax_detect, fax_email_users) VALUES ('15550003333','[]',?,1,1)", (gid,))
d.execute("INSERT INTO inbound_routes (did, ring_extens) VALUES ('15550002222','[\"8801\"]')")
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('smtp_host','smtp.example.com')")
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('smtp_from','pbx@example.com')")
d.commit()
SENT = []
mailer.send = lambda cfg, to, subj, text, html=None, attachment=None, timeout=20: SENT.append((to, subj, text, attachment))

# Faxing is a plan feature: without it the number isn't listened on.
# Detect faxes always listens (the admin ticked it); the plan only decides
# who gets the fax. The log says so.
w, why = fax.fax_check("15550001111")
check("no fax in plan: still listens, and says who won't get it", w == 5 and "admin Faxes" in why, (w, why))
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('default_quota_fax','1')"); d.commit()
check("listening time per number", fax.fax_wait("15550001111") == 5 and fax.fax_wait("+15550001111") == 5 and fax.fax_wait("5550001111") == 5)
check("off for a number without detection", fax.fax_wait("15550002222") == 0 and fax.fax_wait("19999999999") == 0)

def make_tif():
    p = os.path.join(FAXDIR, "fax-test-%d.tif" % (time.time() * 1000))
    subprocess.run(["gs", "-q", "-sDEVICE=tiffg3", "-r204x196", "-o", p, "-c",
                    "/Helvetica findfont 30 scalefont setfont 72 600 moveto (Test) show showpage"], check=True)
    return p
def wait_mail(n):
    for _ in range(50):
        if len(SENT) >= n: return
        time.sleep(0.1)

tif = make_tif()
fid = fax.fax_done(tif, "SUCCESS", "1", "", "15550001111", "13105550100", " ACME FAX ")
f = d.execute("SELECT * FROM faxes WHERE id=?", (fid,)).fetchone()
check("received: stored as a PDF", f["status"] == "ok" and f["path"].endswith(".pdf") and os.path.isfile(f["path"]) and not os.path.exists(tif), dict(f))
check("caller, pages, station kept", f["caller"] == "13105550100" and f["pages"] == 1 and f["remote_station"] == "ACME FAX")
r = [x[0] for x in d.execute("SELECT l.exten FROM fax_recipients r JOIN logins l ON l.id=r.login_id WHERE fax_id=?", (fid,))]
check("belongs to whoever the number rings", r == ["8800"], r)
wait_mail(2)
to = sorted(x[0] for x in SENT)
check("emailed to the number's address and the user", to == ["office@example.com", "u8800@example.com"], to)
check("PDF attached", all(x[3] and x[3][2] == "application/pdf" and x[3][1][:4] == b"%PDF" for x in SENT))
check("subject names the sender", "13105550100" in SENT[0][1] and "1 page" in SENT[0][1], SENT[0][1])

SENT.clear()
fid2 = fax.fax_done(make_tif(), "SUCCESS", "2", "", "15550003333", "", "")
r = sorted(x[0] for x in d.execute("SELECT l.exten FROM fax_recipients r JOIN logins l ON l.id=r.login_id WHERE fax_id=?", (fid2,)))
check("ring-group number: every member gets it", r == ["8800", "8801"], r)
wait_mail(2)
check("…and both are emailed", sorted(x[0] for x in SENT) == ["u8800@example.com", "u8801@example.com"])
u8801 = d.execute("SELECT id FROM logins WHERE exten='8801'").fetchone()[0]
d.execute("INSERT INTO user_entitlements (login_id, feature, quota) VALUES (?, 'fax', 0)", (u8801,)); d.commit()
fid3 = fax.fax_done(make_tif(), "SUCCESS", "1", "", "15550003333", "", "")
r = sorted(x[0] for x in d.execute("SELECT l.exten FROM fax_recipients r JOIN logins l ON l.id=r.login_id WHERE fax_id=?", (fid3,)))
check("member without fax in their plan doesn't get it", r == ["8800"], r)
d.execute("DELETE FROM user_entitlements WHERE login_id=? AND feature='fax'", (u8801,)); d.commit()
wait_mail(1); time.sleep(0.5)     # its email goes out in the background

SENT.clear()
fid3 = fax.fax_done(os.path.join(FAXDIR, "missing.tif"), "FAILED", "0", "fax transmission not established", "15550001111", "3105550100", "")
f3 = d.execute("SELECT * FROM faxes WHERE id=?", (fid3,)).fetchone()
check("failed fax recorded with the reason", f3["status"] == "failed" and "not established" in f3["error"] and f3["path"] == "")
wait_mail(2)
check("failed fax: email says it didn't come through", SENT and "didn't come through" in SENT[0][1] and SENT[0][3] is None)

outside = tempfile.mktemp(suffix=".tif"); open(outside, "w").write("x")
fid4 = fax.fax_done(outside, "SUCCESS", "1", "", "15550001111", "", "")
row4 = tuple(d.execute("SELECT status, path FROM faxes WHERE id=?", (fid4,)).fetchone())
check("a file outside the fax folder is never used", row4 == ("failed", "") and os.path.exists(outside), row4)
# A fax that failed in the final handshake after a page arrived: keep it.
SENT.clear()
fidp = fax.fax_done(make_tif(), "FAILED", "0", "Unexpected command after page received", "15550001111", "18772333839", "")
fp = d.execute("SELECT * FROM faxes WHERE id=?", (fidp,)).fetchone()
check("partial fax kept as a PDF with a note", fp["status"] == "ok" and fp["pages"] == 1 and fp["path"].endswith(".pdf")
      and "May be incomplete" in fp["error"], dict(fp))
for _ in range(80):
    if any("may be missing" in x[2] for x in SENT): break
    time.sleep(0.1)
check("its email attaches the PDF and says it may be incomplete",
      any(x[3] and "may be missing" in x[2] for x in SENT), [(x[1], x[2][:200]) for x in SENT])
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
