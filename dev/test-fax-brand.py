#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Fax branding + fax as a plan feature, on a throwaway DB (no Asterisk):
admin branding form (logo, company, footer, header, "let users brand"),
the user's own branding in My Phone, which one a fax uses, the header line
stored for pbx-brain, cover previews (web and the app), the app's summary,
and the plan gate (billing plan / default / per-user exception).
Needs gs, tiff2pdf and Pillow.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-fax-brand.py      # exits non-zero on failure
"""
import io, os, re, sys, sqlite3, subprocess, tempfile
here = os.path.dirname(os.path.abspath(__file__))
os.environ["PBX_FAX_DIR"] = tempfile.mkdtemp(prefix="faxdir-")
os.environ["PBX_FAX_BRAND_DIR"] = BRAND = tempfile.mkdtemp(prefix="brand-")
sys.path.insert(0, os.path.join(here, "..", "pbx-api"))
from fastapi.testclient import TestClient
import app as A, app_api, bcrypt
A.init_db()
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)
d = sqlite3.connect(os.environ["PBX_DB"]); d.row_factory = sqlite3.Row
for u, pw in (("phone8800", "pw-8800-x"),):
    d.execute("UPDATE logins SET pwhash=? WHERE username=?", (bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode(), u))
adm = d.execute("SELECT username FROM logins WHERE role='admin' LIMIT 1").fetchone()[0]
d.execute("UPDATE logins SET pwhash=? WHERE username=?", (bcrypt.hashpw(b"pw-admin-1", bcrypt.gensalt()).decode(), adm))
d.execute("INSERT INTO inbound_routes (did, ring_extens, fax_detect) VALUES ('5550001111','[\"8800\"]',1)")
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('default_quota_call_minutes','100')")
d.commit()
A._asterisk_status = lambda: {"contacts": [], "fax_ready": True}
def lid(u): return d.execute("SELECT id FROM logins WHERE username=?", (u,)).fetchone()[0]
def login(u, pw):
    A._login_failures.clear()
    c = TestClient(A.app, client=("192.168.1.50", 5555), follow_redirects=False)
    c.post("/login", data={"username": u, "password": pw})
    return c
def csrf(c, url):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)
def png(color=(200, 30, 30)):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (600, 200), "white"); ImageDraw.Draw(im).rectangle((20, 20, 300, 180), fill=color)
    b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()
def pdf_text(data):
    p = os.path.join(BRAND, "x.pdf"); open(p, "wb").write(data)
    return data[:4] == b"%PDF"

# ---- plan gate
uc = login("phone8800", "pw-8800-x")
H = {"Authorization": "Bearer " + uc.post("/api/v1/app/pair", json={"code": app_api._new_pair_code(lid("phone8800"))}).json()["token"]}
t = uc.get("/ucp/faxes").text
check("no fax in plan: My Phone says so, no branding section", "included in your plan" in t and "My fax branding" not in t)
check("no fax in plan: app has no Faxes tab", uc.get("/api/v1/me/overview", headers=H).json()["fax_enabled"] is False)
ac = login(adm, "pw-admin-1")
bt = ac.get("/billing").text
check("billing: Fax in the no-plan defaults and per-user table", 'name="fax"' in bt and "<th>Fax</th>" in bt, bt[-400:])
pt = ac.get("/billing/plans/new/edit").text
check("billing: plan form has Fax", 'name="fax"' in pt and "send and receive faxes" in pt)
check("billing: plan form has Faxes per month", 'name="faxes"' in pt and 'name="faxes_unl"' in pt)
ptok = csrf(ac, "/billing/plans/new/edit")
r = ac.post("/billing/plans/new/edit", data={"csrf_token": ptok, "name": "Fax Pro", "price": "9.99", "currency": "usd",
                                             "fax": "1", "faxes": "40", "active": "1"})
pl = d.execute("SELECT fax, faxes FROM billing_plans WHERE name='Fax Pro'").fetchone()
check("plan saved with 40 faxes/month", pl is not None and pl["fax"] == 1 and pl["faxes"] == 40, (r.status_code, r.text[-300:] if r.status_code >= 400 else ""))
import billing
check("plan lists 'Fax: 40 faxes / month'", "Fax: 40 faxes / month" in billing.plan_features(dict(d.execute("SELECT * FROM billing_plans WHERE name='Fax Pro'").fetchone())),
      billing.plan_features(dict(d.execute("SELECT * FROM billing_plans WHERE name='Fax Pro'").fetchone())))
tok = csrf(ac, "/billing")
r = ac.post(f"/billing/access/{lid('phone8800')}", data={"csrf_token": tok, "fax": "1"})
check("per-user exception turns fax on", d.execute("SELECT quota FROM user_entitlements WHERE login_id=? AND feature='fax'",
                                                    (lid("phone8800"),)).fetchone()[0] == 1, r.status_code)
check("…app now shows Faxes", uc.get("/api/v1/me/overview", headers=H).json()["fax_enabled"] is True)

# ---- admin branding
t = ac.get("/faxes").text
check("admin Faxes has the branding form", "Fax branding" in t and 'action="/faxes/branding"' in t and "users_allowed" in t)
tok = csrf(ac, "/faxes")
r = ac.post("/faxes/branding", data={"csrf_token": tok, "company": "Acme Dental", "address": "1 Main St\nFresno CA",
                                     "phone": "5595550100", "email": "o@acme.test", "website": "acme.test",
                                     "footer": "CONFIDENTIAL notice", "header": "Acme Dental HQ", "users_allowed": "1"},
            files={"logo": ("logo.png", png(), "image/png")})
b = d.execute("SELECT * FROM fax_branding WHERE login_id=0").fetchone()
check("admin branding saved with a fax-ready logo", r.status_code == 303 and b and b["company"] == "Acme Dental"
      and b["logo"] and os.path.isfile(os.path.join(BRAND, b["logo"])), (r.status_code, dict(b) if b else None))
from PIL import Image
with Image.open(os.path.join(BRAND, b["logo"])) as im:
    check("logo is black & white and trimmed", im.mode == "1" and im.width <= 700 and im.height <= 220, (im.mode, im.size))
check("admin logo served", ac.get("/faxes/branding/logo").headers.get("content-type") == "image/png")
r = ac.get("/faxes/branding/preview")
check("admin cover preview is a PDF", r.status_code == 200 and pdf_text(r.content), r.status_code)
r = ac.post("/faxes/branding", data={"csrf_token": tok, "company": "Acme Dental", "header": "Acme Dental HQ", "users_allowed": "1"},
            files={"logo": ("x.png", b"not a picture", "image/png")})
check("bad logo refused with a message", "brand_err=" in r.headers.get("location", ""), r.headers.get("location"))
check("users can't post the admin form", uc.post("/faxes/branding", data={"csrf_token": "x"}).status_code in (401, 403))

# ---- the user's own branding
t = uc.get("/ucp/faxes").text
check("My Phone shows My fax branding (using the PBX's)", "My fax branding" in t and "Using Acme Dental" in t)
utok = csrf(uc, "/ucp/faxes")
fl = uc.get("/api/v1/me/faxes", headers=H).json()
check("app summary: PBX branding", fl["branding"] == {"name": "Acme Dental", "own": False, "logo": True, "editable": True}, fl.get("branding"))

def send():
    pdf = os.path.join(BRAND, "doc.pdf")
    subprocess.run(["gs", "-q", "-sDEVICE=pdfwrite", "-o", pdf, "-c", "/Helvetica findfont 30 scalefont setfont 72 600 moveto (Doc) show showpage"], check=True)
    return uc.post("/api/v1/me/faxes/send", headers=H, files={"file": ("doc.pdf", open(pdf, "rb").read())},
                   data={"to_number": "3105550100", "from_did": "5550001111", "cover": "1", "to_name": "Bob"}).json()
r = send()
row = d.execute("SELECT * FROM fax_outbox WHERE id=?", (r.get("id"),)).fetchone()
check("fax uses the PBX header line", r["ok"] and row["header"] == "Acme Dental HQ", (r, dict(row) if row else None))

r = uc.post("/ucp/faxes/branding", data={"csrf_token": utok, "use_own": "1", "company": "Paul's Repairs",
                                         "phone": "5594069020", "footer": "Thanks for your business"},
            files={"logo": ("me.png", png((20, 90, 200)), "image/png")})
ub = d.execute("SELECT * FROM fax_branding WHERE login_id=?", (lid("phone8800"),)).fetchone()
check("user saved own branding with a logo", r.status_code == 303 and ub and ub["use_own"] == 1 and ub["company"] == "Paul's Repairs" and ub["logo"],
      (r.status_code, r.headers.get("location")))
check("user logo served to them", uc.get("/ucp/faxes/branding/logo").status_code == 200)
r = uc.get("/api/v1/me/faxes/cover-preview?to_name=Bob&subject=Hi", headers=H)
check("app cover preview (own branding) is a PDF", r.status_code == 200 and pdf_text(r.content), r.status_code)
fl = uc.get("/api/v1/me/faxes", headers=H).json()
check("app summary: own branding", fl["branding"]["own"] is True and fl["branding"]["name"] == "Paul's Repairs", fl["branding"])
r = send()
row = d.execute("SELECT * FROM fax_outbox WHERE id=?", (r.get("id"),)).fetchone()
check("fax uses the user's header (company when no header set)", row["header"] == "Paul's Repairs", dict(row))

# admin turns own branding off: back to the PBX's, and the form disappears
r = ac.post("/faxes/branding", data={"csrf_token": tok, "company": "Acme Dental", "header": "Acme Dental HQ"})
check("admin turned off own branding", d.execute("SELECT value FROM kv_settings WHERE key='fax_brand_users'").fetchone()[0] == "0")
r = send()
row = d.execute("SELECT * FROM fax_outbox WHERE id=?", (r.get("id"),)).fetchone()
check("…user's faxes use the PBX branding again", row["header"] == "Acme Dental HQ", dict(row))
check("…and My Phone hides My fax branding", "My fax branding" not in uc.get("/ucp/faxes").text)
r = uc.post("/ucp/faxes/branding", data={"csrf_token": utok, "use_own": "1", "company": "X"})
check("…and saving it is refused", "turned off" in r.headers.get("location", "").replace("%20", " "), r.headers.get("location"))

# admin removes the logo
r = ac.post("/faxes/branding", data={"csrf_token": tok, "company": "Acme Dental", "remove_logo": "1"})
check("admin logo removed", d.execute("SELECT logo FROM fax_branding WHERE login_id=0").fetchone()[0] == ""
      and ac.get("/faxes/branding/logo").status_code == 404)

# plan gate also stops sending
d.execute("UPDATE user_entitlements SET quota=0 WHERE login_id=? AND feature='fax'", (lid("phone8800"),)); d.commit()
r = send()
check("fax taken out of the plan: sending refused", not r["ok"] and "plan" in r["message"], r)

print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
