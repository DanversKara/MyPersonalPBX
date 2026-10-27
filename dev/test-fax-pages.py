#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Fax pages on a throwaway DB: Routes -> Detect faxes, admin Faxes, My Phone
Faxes, who may open a fax PDF, and deletes. Uses pbx-brain/fax.py to create
real faxes (needs gs + tiff2pdf).

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-fax-pages.py      # exits non-zero on failure
"""
import os, sys, re, sqlite3, subprocess, tempfile, time
here = os.path.dirname(os.path.abspath(__file__))
FAXDIR = tempfile.mkdtemp(prefix="faxdir-")
os.environ["PBX_FAX_DIR"] = FAXDIR
sys.path.insert(0, os.path.join(here, "..", "pbx-api"))
from fastapi.testclient import TestClient
import app as A, fax_ui, bcrypt
A.init_db()
sys.path.insert(0, os.path.join(here, "..", "pbx-brain"))
import fax
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)
d = sqlite3.connect(os.environ["PBX_DB"]); d.row_factory = sqlite3.Row
for u, pw in (("phone8800", "pw-8800-x"), ("phone8801", "pw-8801-x")):
    d.execute("UPDATE logins SET pwhash=? WHERE username=?", (bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode(), u))
adm = d.execute("SELECT username FROM logins WHERE role='admin' LIMIT 1").fetchone()[0]
d.execute("UPDATE logins SET pwhash=? WHERE username=?", (bcrypt.hashpw(b"pw-admin-1", bcrypt.gensalt()).decode(), adm))
d.commit()
A._asterisk_status = lambda: {"contacts": [], "fax_ready": False}
A._best_effort_apply = lambda: APPLIED.append(1)
APPLIED = []
def login(u, pw):
    A._login_failures.clear()
    c = TestClient(A.app, client=("192.168.1.50", 5555), follow_redirects=False)
    c.post("/login", data={"username": u, "password": pw})
    return c
def csrf(c, url):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)

ac = login(adm, "pw-admin-1")
t = ac.get("/routes/inbound/new/edit").text
check("route form has the fax settings", "Detect faxes on this number" in t and 'name="fax_wait"' in t)
check("route form warns when Asterisk lacks fax", "pbx-update --with-fax" in t)
tok = csrf(ac, "/routes/inbound/new/edit")
r = ac.post("/routes/inbound/new/edit", data={"csrf_token": tok, "did": "1 (555) 000-1111", "ring_extens": "8800",
                                              "timeout_sec": "30", "enabled": "on", "fax_detect": "1", "fax_wait": "5",
                                              "fax_email": "office@example.com; boss@example.com", "fax_email_users": "1"})
row = d.execute("SELECT * FROM inbound_routes WHERE did='5550001111'").fetchone()
check("saved: fax on, 5 s, emails", r.status_code == 302 and row["fax_detect"] == 1 and row["fax_wait"] == 5
      and row["fax_email"] == "office@example.com, boss@example.com", dict(row) if row else r.text[-300:])
check("first fax number re-renders the trunk config (T.38)", APPLIED == [1])
r = ac.post("/routes/inbound/new/edit", data={"csrf_token": tok, "did": "5550002222", "ring_extens": "8801",
                                              "timeout_sec": "30", "enabled": "on", "fax_email": "not-an-email"})
check("bad fax email refused", r.status_code == 400 and "look like an email" in r.text, (r.status_code, r.text[-300:]))
check("routes list marks the fax number", "+ fax" in ac.get("/routes").text)

def make_fax(did, caller):
    p = os.path.join(FAXDIR, "fax-t-%d.tif" % (time.time() * 1000))
    subprocess.run(["gs", "-q", "-sDEVICE=tiffg3", "-r204x196", "-o", p, "-c",
                    "/Helvetica findfont 30 scalefont setfont 72 600 moveto (Test) show showpage"], check=True)
    return fax.fax_done(p, "SUCCESS", "1", "", did, caller, "")
fid = make_fax("5550001111", "3105550100")
fid_fail = fax.fax_done("", "FAILED", "0", "fax transmission not established", "5550001111", "3105550199", "")

page = ac.get("/faxes").text
check("admin Faxes lists it with who it's for", "3105550100" in page and "(8800)" in page and "View" in page)
check("admin sees the failed one with the reason", "not established" in page)
check("admin can open the PDF", ac.get(f"/api/fax/{fid}").content[:4] == b"%PDF")

u1 = login("phone8800", "pw-8800-x")
t = u1.get("/ucp").text
check("recipient gets a Faxes tab with the new count", "/ucp/faxes" in t and "Faxes (1)" in t, re.findall(r"Faxes[^<]*", t))
t = u1.get("/ucp/faxes").text
check("recipient's Faxes page lists it", "3105550100" in t and f"/api/fax/{fid}" in t)
r = u1.get(f"/api/fax/{fid}")
check("recipient can open the PDF", r.status_code == 200 and r.content[:4] == b"%PDF" and "inline" in r.headers["content-disposition"])
check("opening marks it seen", "Faxes (1)" not in u1.get("/ucp").text)
check("download link", "attachment" in u1.get(f"/api/fax/{fid}?download=1").headers["content-disposition"])

u2 = login("phone8801", "pw-8801-x")
check("someone else can't open it", u2.get(f"/api/fax/{fid}").status_code == 404)
check("…and has no Faxes tab", "/ucp/faxes" not in u2.get("/ucp").text)
check("not signed in: refused", TestClient(A.app).get(f"/api/fax/{fid}").status_code == 401)

path = d.execute("SELECT path FROM faxes WHERE id=?", (fid,)).fetchone()[0]
tok1 = csrf(u1, "/ucp/faxes")
u1.post("/ucp/faxes/delete", data={"csrf_token": tok1, "ids": str(fid)})
check("user delete: gone for them, file removed (no one else had it)",
      f"/api/fax/{fid}" not in u1.get("/ucp/faxes").text and not os.path.exists(path))
check("admin list shows it as deleted", "Deleted" in ac.get("/faxes").text)

fid2 = make_fax("5550001111", "3105550111")
p2 = d.execute("SELECT path FROM faxes WHERE id=?", (fid2,)).fetchone()[0]
tok = csrf(ac, "/faxes")
r = ac.post("/faxes/delete", data={"csrf_token": tok, "all": "1"})
check("admin delete all: rows and files gone", r.status_code == 303 and d.execute("SELECT COUNT(*) FROM faxes").fetchone()[0] == 0 and not os.path.exists(p2))
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
