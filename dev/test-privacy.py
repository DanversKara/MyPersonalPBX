#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Account deletion + data download requests (/ucp/delete, /privacy).

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-privacy.py      # run from the repo root; exits non-zero on failure
"""
import io, os, re, sys, tempfile, zipfile
here = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp(prefix="privtest-")
os.environ["PBX_EXPORT_DIR"] = os.path.join(tmp, "exports")
os.environ["PBX_VMGREET_DIR"] = os.path.join(tmp, "vmgreet")
sys.path.insert(0, os.path.join(here, "..", "shared"))
sys.path.insert(0, os.path.join(here, "..", "pbx-api"))
import app as A
import ucp, privacy_ui
import bcrypt
from fastapi.testclient import TestClient

fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)

A.init_db()
ucp.VM_ROOT = os.path.join(tmp, "vm"); os.makedirs(ucp.VM_ROOT)
ucp.REC_ROOT = os.path.join(tmp, "rec"); os.makedirs(ucp.REC_ROOT)
A._best_effort_apply = lambda: None
mail = []
privacy_ui._send = lambda to, subj, text: mail.append((to, subj, text)) or True
privacy_ui._admin_to = lambda: "boss@example.com"

db = A.db()
adm = db.execute("SELECT username FROM logins WHERE role='admin' LIMIT 1").fetchone()[0]
pw = lambda p: bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
db.execute("UPDATE logins SET pwhash=? WHERE username=?", (pw("pw-admin-1"), adm))
u = dict(db.execute("SELECT * FROM logins WHERE exten='8800'").fetchone())
db.execute("UPDATE logins SET pwhash=?, vm_email='u@example.com' WHERE id=?", (pw("pw-user-12"), u["id"]))
# Some data for 8800.
db.execute("INSERT INTO cdr (call_id, started_at, src, dst, direction, login_id, duration_sec, disposition)"
           " VALUES ('c1', datetime('now','localtime'), '8800', '5551234567', 'out', ?, 42, 'ANSWERED')", (u["id"],))
db.execute("INSERT INTO messages (from_ext, to_ext, body, login_id) VALUES ('5551234567','8800','hello there [MMS: https://x/y.jpg]', ?)", (u["id"],))
db.execute("INSERT INTO contacts (login_id, name, number, number_norm) VALUES (?, 'Bob', '5551234567', '5551234567')", (u["id"],))
db.execute("INSERT OR IGNORE INTO voicemail_boxes (login_id, mailbox) VALUES (?, 'vm-8800')", (u["id"],))
vm = os.path.join(ucp.VM_ROOT, "msg1.wav"); open(vm, "wb").write(b"RIFFfakewav")
db.execute("INSERT INTO voicemail_messages (mailbox, caller, path, duration_sec) VALUES ('vm-8800', '5551234567', ?, 5)", (vm,))
db.commit()
A._login_failures.clear()

# --- not signed in
pub = TestClient(A.app, client=("203.0.113.9", 5555), follow_redirects=False)
t = pub.get("/ucp/delete").text
check("public page explains and offers sign-in", "Sign in to make a request" in t and "90 days" in t)
r = pub.post("/login", data={"username": u["username"], "password": "pw-user-12", "next": "/ucp/delete"})
check("sign-in returns to /ucp/delete", r.headers.get("location") == "/ucp/delete", r.headers.get("location"))

uc = TestClient(A.app, client=("203.0.113.9", 5555), follow_redirects=False)
uc.post("/login", data={"username": u["username"], "password": "pw-user-12"})
csrf = lambda c, url: re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)
t = uc.get("/ucp/delete").text
check("signed-in page has both forms", "Request my data" in t and "Request account deletion" in t and "u@example.com" in t)
check("settings links to it", "/ucp/delete" in uc.get("/ucp/settings").text)

tok = csrf(uc, "/ucp/delete")
r = uc.post("/ucp/delete/export", data={"csrf_token": tok, "email": "bad"})
check("bad email refused", "msg=email" in r.headers["location"])
r = uc.post("/ucp/delete/export", data={"csrf_token": tok, "email": "u@example.com"})
check("export requested", "msg=exp_ok" in r.headers["location"])
check("admin told", any(m[0] == "boss@example.com" and "Data download" in m[1] for m in mail))
r = uc.post("/ucp/delete/export", data={"csrf_token": tok, "email": "u@example.com"})
check("second export refused while open", "msg=dup" in r.headers["location"])
check("page shows pending export", "We'll email" in uc.get("/ucp/delete").text)

# --- admin builds it
ac = TestClient(A.app, client=("192.168.1.50", 5555), follow_redirects=False)
ac.post("/login", data={"username": adm, "password": "pw-admin-1"})
t = ac.get("/privacy").text
check("admin page lists it", "Build &amp; email download" in t and u["username"] in t)
rid = db.execute("SELECT id FROM privacy_requests WHERE kind='export'").fetchone()[0]
mail.clear()
atok = csrf(ac, "/privacy")
r = ac.post(f"/privacy/{rid}/export", data={"csrf_token": atok})
check("export built", "emailed" in r.headers["location"], r.headers["location"])
link = re.search(r"https?://\S+/data-export/(\S+)", mail[-1][2] if mail else "")
check("link emailed to the user", bool(link) and mail[-1][0] == "u@example.com")
r = pub.get(f"/data-export/{link.group(1)}")
check("link downloads a zip without sign-in", r.status_code == 200 and r.headers["content-type"] == "application/zip")
z = zipfile.ZipFile(io.BytesIO(r.content))
names = z.namelist()
check("zip has the parts", all(n in names for n in ("account.json", "calls.csv", "messages.csv", "contacts.csv",
                                                    "voicemail.csv", "README.txt")), names)
check("voicemail audio included", any(n.startswith("voicemail/") and n.endswith(".wav") for n in names), names)
check("texts and MMS links included", "hello there" in z.read("messages.csv").decode())
check("calls included", "5551234567" in z.read("calls.csv").decode())
check("contacts included", "Bob" in z.read("contacts.csv").decode())
acct = z.read("account.json").decode()
check("no password or SIP secret", "pwhash" not in acct and "sip_secret" not in acct)
check("download counted", db.execute("SELECT downloads FROM privacy_requests WHERE id=?", (rid,)).fetchone()[0] == 1)
check("bad token 404", pub.get("/data-export/" + "x" * 43).status_code == 404)

# --- deletion request
r = uc.post("/ucp/delete/account", data={"csrf_token": tok, "email": "u@example.com", "confirm": "nope", "ack": "1"})
check("needs DELETE typed", "msg=confirm" in r.headers["location"])
mail.clear()
r = uc.post("/ucp/delete/account", data={"csrf_token": tok, "email": "u@example.com", "confirm": "delete",
                                         "ack": "1", "reason": "moving"})
check("deletion requested", "msg=del_ok" in r.headers["location"])
check("user gets a receipt", any(m[0] == "u@example.com" and "deletion request" in m[1] for m in mail))
check("page shows deadline and cancel", "Cancel my deletion request" in uc.get("/ucp/delete").text)
uc.post("/ucp/delete/cancel", data={"csrf_token": tok})
check("cancel works", db.execute("SELECT COUNT(*) FROM privacy_requests WHERE kind='delete' AND status='cancelled'").fetchone()[0] == 1)
uc.post("/ucp/delete/account", data={"csrf_token": tok, "email": "u@example.com", "confirm": "DELETE", "ack": "1"})
did = db.execute("SELECT id, due_at FROM privacy_requests WHERE kind='delete' AND status='pending'").fetchone()
check("due in 90 days", privacy_ui._days_left(did["due_at"]) in (89, 90), did["due_at"])
t = ac.get("/privacy").text
check("admin sees the deletion with days left", "Delete account now" in t and "day(s) left" in t)

mail.clear()
atok = csrf(ac, "/privacy")
r = ac.post(f"/privacy/{did['id']}/delete", data={"csrf_token": atok})
check("deleted", "Deleted" in r.headers["location"], r.headers["location"])
gone = lambda q, *a: db.execute(q, a).fetchone()[0] == 0
check("login gone", gone("SELECT COUNT(*) FROM logins WHERE id=?", u["id"]))
check("calls gone", gone("SELECT COUNT(*) FROM cdr WHERE src='8800' OR dst='8800'"))
check("texts gone", gone("SELECT COUNT(*) FROM messages WHERE to_ext='8800'"))
check("contacts gone", gone("SELECT COUNT(*) FROM contacts WHERE login_id=?", u["id"]))
check("voicemail gone (DB + file)", gone("SELECT COUNT(*) FROM voicemail_messages WHERE mailbox='vm-8800'") and not os.path.exists(vm))
check("request kept as a record", db.execute("SELECT status, login_id FROM privacy_requests WHERE id=?", (did["id"],)).fetchone()[:] == ("done", None))
check("confirmation emailed", any(m[0] == "u@example.com" and "deleted" in m[1] for m in mail))
check("their signed-in session no longer works", "Sign in to make a request" in uc.get("/ucp/delete").text)
check("download link still works after deletion", pub.get(f"/data-export/{link.group(1)}").status_code == 200)

# --- expiry
db.execute("UPDATE privacy_requests SET token_expires='2000-01-01 00:00:00' WHERE id=?", (rid,)); db.commit()
r = pub.get(f"/data-export/{link.group(1)}")
check("expired link refused and zip removed", r.status_code == 404 and not os.listdir(os.environ["PBX_EXPORT_DIR"]))

# --- settings, and the Logins page delete still works
atok = csrf(ac, "/privacy")
ac.post("/privacy/settings", data={"csrf_token": atok, "contact": "privacy@example.com", "auto": "1"})
check("contact shown to visitors", "privacy@example.com" in pub.get("/ucp/delete").text)
u2 = db.execute("SELECT id FROM logins WHERE exten='8801'").fetchone()[0]
r = ac.post(f"/logins/{u2}/delete", data={"csrf_token": csrf(ac, "/logins")})
check("Logins page delete still works", r.status_code == 303 and gone("SELECT COUNT(*) FROM logins WHERE id=?", u2))

print("FAILED:", ", ".join(fails) or "none")
sys.exit(1 if fails else 0)
