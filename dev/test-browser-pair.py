#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""'Sign in with my browser' for the app: /ucp/app/connect signs in (password
or single sign-on), comes back, and hands the app a pairing link carrying
the app's state. In-process on a throwaway DB.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-browser-pair.py      # exits non-zero on failure
"""
import os, sys, re, html, sqlite3, urllib.parse, base64, json, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-api"))
from fastapi.testclient import TestClient
import app as A, app_api, sso_ui, bcrypt
A.init_db()
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)
d = sqlite3.connect(os.environ["PBX_DB"])
d.execute("UPDATE logins SET pwhash=? WHERE username='phone8800'", (bcrypt.hashpw(b"pw-8800-x", bcrypt.gensalt()).decode(),)); d.commit()
ST = "AbC123_-xyz789"
c = TestClient(A.app, follow_redirects=False)
r = c.get(f"/ucp/app/connect?st={ST}")
check("not signed in: off to /login with next", r.status_code == 302 and r.headers["location"] == "/login?" + urllib.parse.urlencode({"next": f"/ucp/app/connect?st={ST}"}), r.headers.get("location"))
r = c.get(r.headers["location"])
check("login form carries next", f'name="next" value="/ucp/app/connect?st={ST}"' in r.text)
A._login_failures.clear()
r = c.post("/login", data={"username": "phone8800", "password": "wrong", "next": f"/ucp/app/connect?st={ST}"})
check("wrong password keeps next", f'name="next" value="/ucp/app/connect?st={ST}"' in r.text)
r = c.post("/login", data={"username": "phone8800", "password": "pw-8800-x", "next": f"/ucp/app/connect?st={ST}"})
check("signed in: back to connect", r.status_code == 302 and r.headers["location"] == f"/ucp/app/connect?st={ST}", r.headers.get("location"))
r = c.get(r.headers["location"])
m = re.search(r'id="go" href="([^"]+)"', r.text)
link = html.unescape(m.group(1)) if m else ""
check("hands the app an intent link", link.startswith("intent://pair?") and "package=org.ownpbx.myphone" in link, link)
q = dict(urllib.parse.parse_qsl(link.split("?", 1)[1].split("#", 1)[0]))
check("link has server, code and the app's state", q.get("st") == ST and len(q.get("c", "")) == 8 and q.get("s", "").startswith("http"), q)
check("the code pairs", c.post("/api/v1/app/pair", json={"code": q["c"], "device_name": "t"}).status_code == 200)
A._login_failures.clear()
for bad in ("//evil.example.com", "https://evil.example.com/ucp/app/connect", "/ucp/app/connect?st=x", "/ucp/app/connect?st=abcdefgh&x=1", "/ucp/settings"):
    r = TestClient(A.app, follow_redirects=False).post("/login", data={"username": "phone8800", "password": "pw-8800-x", "next": bad})
    if not (r.status_code == 302 and r.headers["location"] == "/ucp"):
        check("unsafe next ignored: " + bad, False, r.headers.get("location"))
check("unsafe next values ignored", True)
r = TestClient(A.app, follow_redirects=False).get("/ucp/app/connect?st=<script>")
check("bad state dropped", r.headers.get("location", "").endswith(urllib.parse.quote("/ucp/app/connect", safe="")))

# single sign-on comes back too
ISS = "https://auth.example.com/application/o/pbx/"
for k, v in {"sso_enabled": "1", "sso_issuer": ISS, "sso_client_id": "cid", "sso_client_secret": "sec",
             "sso_match": "username", "sso_auto_redirect": "1"}.items():
    d.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?,?)", (k, v))
d.commit()
SEEN = {}
def fake_http(url, data=None, headers=None, timeout=10):
    if url.endswith("/.well-known/openid-configuration"):
        return {"issuer": ISS, "authorization_endpoint": "https://auth.example.com/authorize/", "token_endpoint": "https://auth.example.com/token/"}
    enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    claims = {"iss": ISS, "aud": "cid", "exp": time.time() + 300, "nonce": SEEN["nonce"], "sub": "u1", "preferred_username": "phone8800"}
    return {"id_token": enc({}) + "." + enc(claims) + ".x"}
sso_ui._http = fake_http
A._login_failures.clear()
c = TestClient(A.app, follow_redirects=False)
r = c.get(c.get(f"/ucp/app/connect?st={ST}").headers["location"])     # /login?next=... with auto redirect on
check("auto sign-on keeps next", r.status_code == 302 and r.headers["location"].startswith("/login/oidc?next="), r.headers.get("location"))
r = c.get(r.headers["location"])
qa = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query)); SEEN["nonce"] = qa["nonce"]
r = c.get("/login/oidc/callback", params={"code": "x", "state": qa["state"]})
check("after Authentik: back to connect", r.status_code == 302 and r.headers["location"] == f"/ucp/app/connect?st={ST}", (r.status_code, r.headers.get("location"), r.text[-200:]))
check("then the app link", "intent://pair?" in c.get(r.headers["location"]).text)
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
