#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Fax API for the app + the web send form, on a throwaway DB (no Asterisk):
list, send (PDF and picture), checks (number, sender's DIDs, file type,
minutes), sent list and PDF, cancel/delete, received PDF, overview and
/me/wait fields. Needs gs, tiff2pdf and Pillow.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-fax-api.py      # exits non-zero on failure
"""
import io, os, re, sys, sqlite3, subprocess, tempfile, time
here = os.path.dirname(os.path.abspath(__file__))
FAXDIR = tempfile.mkdtemp(prefix="faxdir-")
os.environ["PBX_FAX_DIR"] = FAXDIR
sys.path.insert(0, os.path.join(here, "..", "pbx-api"))
from fastapi.testclient import TestClient
import app as A, app_api, bcrypt
A.init_db()
sys.path.insert(0, os.path.join(here, "..", "pbx-brain"))
import fax
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)
d = sqlite3.connect(os.environ["PBX_DB"]); d.row_factory = sqlite3.Row
d.execute("INSERT INTO inbound_routes (did, ring_extens, fax_detect) VALUES ('5550001111','[\"8800\"]',1)")
d.execute("INSERT INTO inbound_routes (did, ring_extens) VALUES ('5550002222','[\"8801\"]')")
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('default_quota_call_minutes','100')")
d.execute("UPDATE logins SET pwhash=? WHERE username='phone8800'", (bcrypt.hashpw(b"pw-8800-x", bcrypt.gensalt()).decode(),))
d.commit()
A._asterisk_status = lambda: {"contacts": [], "fax_ready": True}
def lid(u): return d.execute("SELECT id FROM logins WHERE username=?", (u,)).fetchone()[0]
c = TestClient(A.app)
H = {"Authorization": "Bearer " + c.post("/api/v1/app/pair", json={"code": app_api._new_pair_code(lid("phone8800"))}).json()["token"]}
H2 = {"Authorization": "Bearer " + c.post("/api/v1/app/pair", json={"code": app_api._new_pair_code(lid("phone8801"))}).json()["token"]}

# Faxing is a plan feature: no plan, no Faxes tab and no sending.
ov = c.get("/api/v1/me/overview", headers=H).json()
check("no fax in plan: tab hidden", ov.get("fax_enabled") is False, ov)
r = c.get("/api/v1/me/faxes", headers=H).json()
check("no fax in plan: can't send, says why", r["can_send"] is False and "plan" in r["why_not"], r)
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('default_quota_fax','1')"); d.commit()

r = c.get("/api/v1/me/faxes", headers=H).json()
check("list: can send, from my number only", r["can_send"] is True and r["numbers"] == ["5550001111"], r)
ov = c.get("/api/v1/me/overview", headers=H).json()
check("overview: fax tab on", ov.get("fax_enabled") is True and ov.get("new_faxes") == 0, ov)

pdf = os.path.join(FAXDIR, "doc.pdf")
subprocess.run(["gs", "-q", "-sDEVICE=pdfwrite", "-o", pdf, "-c", "/Helvetica findfont 30 scalefont setfont 72 600 moveto (Doc) show showpage"], check=True)
def send(h, data, name, **f):
    fields = {"to_number": "3105550100", "from_did": "5550001111", "cover": "1"}
    fields.update(f)
    return c.post("/api/v1/me/faxes/send", headers=h, data=fields, files={"file": (name, data)}).json()
r = send(H, open(pdf, "rb").read(), "doc.pdf", to_name="Acme", subject="Hi", note="Hello")
check("send a PDF with a cover", r["ok"] and r["id"], r)
row = d.execute("SELECT * FROM fax_outbox WHERE id=?", (r["id"],)).fetchone()
check("queued: 2 pages, 11-digit number, my DID", row["status"] == "queued" and row["pages"] == 2
      and row["to_number"] == "13105550100" and row["from_did"] == "5550001111" and os.path.isfile(row["tif_path"]))

from PIL import Image
buf = io.BytesIO(); Image.new("RGB", (1200, 1600), "white").save(buf, "PNG")
r = send(H, buf.getvalue(), "photo.png", cover="0")
check("send a picture (no cover)", r["ok"] and d.execute("SELECT pages FROM fax_outbox WHERE id=?", (r["id"],)).fetchone()[0] == 1, r)

for name, kw, word in (("bad number", {"to_number": "555"}, "full fax number"),
                       ("someone else's DID", {"from_did": "5550002222"}, "your numbers"),
                       ("unsupported file", {"_file": ("notes.docx", b"PK..")}, "PDF")):
    f = kw.pop("_file", ("doc.pdf", open(pdf, "rb").read()))
    r = send(H, f[1], f[0], **kw)
    check("refused: " + name, not r["ok"] and word in r["message"], r)
r = send(H2, open(pdf, "rb").read(), "doc.pdf", from_did="5550001111")
check("refused: other user can't use my number", not r["ok"])
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('default_quota_call_minutes','0')"); d.commit()
r = send(H, open(pdf, "rb").read(), "doc.pdf")
check("refused: no minutes left", not r["ok"] and "minutes" in r["message"], r)
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('default_quota_call_minutes','100')"); d.commit()

r = c.get("/api/v1/me/faxes", headers=H).json()
check("sent list shows both, newest first", [s["status"] for s in r["sent"]] == ["queued", "queued"] and r["sent"][0]["has_pdf"])
sid = r["sent"][0]["id"]
check("sent PDF opens for me", c.get(f"/api/v1/me/faxes/sent/{sid}/pdf", headers=H).content[:4] == b"%PDF")
check("…not for someone else", c.get(f"/api/v1/me/faxes/sent/{sid}/pdf", headers=H2).status_code == 404)
job = os.path.dirname(d.execute("SELECT tif_path FROM fax_outbox WHERE id=?", (sid,)).fetchone()[0])
r = c.post("/api/v1/me/faxes/sent/delete", headers=H, data={"ids": str(sid)}).json()
check("cancel a queued fax: gone, files removed", r["ok"] and not os.path.exists(job) and len(c.get("/api/v1/me/faxes", headers=H).json()["sent"]) == 1)

# a received fax
tif = os.path.join(FAXDIR, "fax-in.tif")
subprocess.run(["gs", "-q", "-sDEVICE=tiffg3", "-r204x196", "-o", tif, "-c", "/Helvetica findfont 30 scalefont setfont 72 600 moveto (In) show showpage"], check=True)
fid = fax.fax_done(tif, "SUCCESS", "1", "", "5550001111", "3105550199", "")
w = c.get("/api/v1/me/wait", headers=H, params={"msg": 0, "call": 0, "vm": 0, "fax": 0, "timeout": 0}).json()
check("wait: a new fax counts as a change", w["changed"] and w["fax"] == fid, w)
w = c.get("/api/v1/me/wait", headers=H, params={"msg": 999999, "call": 999999, "vm": 999999, "timeout": 0}).json()
check("wait: older apps (no fax=) unaffected", w["changed"] is False, w)
r = c.get("/api/v1/me/faxes", headers=H).json()
check("received list", r["received"][0]["id"] == fid and r["received"][0]["has_pdf"] and not r["received"][0]["seen"])
check("overview: 1 new fax", c.get("/api/v1/me/overview", headers=H).json()["new_faxes"] == 1)
check("received PDF opens in the app", c.get(f"/api/v1/me/faxes/{fid}/pdf", headers=H).content[:4] == b"%PDF")
check("…and is then seen", c.get("/api/v1/me/overview", headers=H).json()["new_faxes"] == 0)
check("…not for someone else", c.get(f"/api/v1/me/faxes/{fid}/pdf", headers=H2).status_code == 404)
r = c.post("/api/v1/me/faxes/delete", headers=H, data={"ids": str(fid)}).json()
check("delete received", r["ok"] and c.get("/api/v1/me/faxes", headers=H).json()["received"] == [])

# web form
A._login_failures.clear()
w = TestClient(A.app, follow_redirects=False)
w.post("/login", data={"username": "phone8800", "password": "pw-8800-x"})
page = w.get("/ucp/faxes").text
tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
check("web: send form shows my number", "Send a fax" in page and '<option value="5550001111">' in page)
r = w.post("/ucp/faxes/send", data={"csrf_token": tok, "to_number": "3105550100", "from_did": "5550001111", "cover": "1"},
           files={"file": ("doc.pdf", open(pdf, "rb").read())})
check("web: send queues it", r.status_code == 303 and "sent=1" in r.headers["location"], r.headers.get("location"))
check("web: sent list shows it", "Waiting to send" in w.get("/ucp/faxes").text)
# monthly allowance (Billing: Faxes per month)
u = c.get("/api/v1/me/usage", headers=H).json()
check("usage: fax included, unlimited by default", u["includes"]["fax"] is True and u["features"]["faxes"]["quota"] is None
      and u["features"]["faxes"]["used"] >= 1, u)
used = u["features"]["faxes"]["used"]
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('default_quota_faxes', ?)", (str(used + 1),)); d.commit()
r = c.get("/api/v1/me/faxes", headers=H).json()
check("one fax left", r["faxes_left"] == 1 and r["can_send"] is True, (r["faxes_left"], r["can_send"]))
check("web form says how many are left", "1 fax left this month" in w.get("/ucp/faxes").text)
r = send(H, open(pdf, "rb").read(), "doc.pdf")
check("send the last one", r["ok"], r)
r = send(H, open(pdf, "rb").read(), "doc.pdf")
check("limit reached: refused, says so", not r["ok"] and "this month" in r["message"], r)
u = c.get("/api/v1/me/usage", headers=H).json()
check("usage: 0 left", u["features"]["faxes"]["left"] == 0, u["features"]["faxes"])
d.execute("UPDATE fax_outbox SET status='failed' WHERE id=(SELECT MAX(id) FROM fax_outbox WHERE login_id=?)", (lid("phone8800"),)); d.commit()
check("a failed fax doesn't count", c.get("/api/v1/me/faxes", headers=H).json()["faxes_left"] == 1)
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('default_quota_fax','0')"); d.commit()
check("fax off: no faxes line in usage", "faxes" not in c.get("/api/v1/me/usage", headers=H).json()["features"])
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
