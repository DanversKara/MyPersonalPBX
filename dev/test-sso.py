#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Web panel single sign-on (OpenID Connect) against a fake provider,
in-process on a throwaway DB.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-sso.py      # exits non-zero on failure
"""
import base64, json, os, sys, time, sqlite3, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-api"))
from fastapi.testclient import TestClient
import app as A, sso_ui
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)
d = sqlite3.connect(os.environ["PBX_DB"]); d.row_factory = sqlite3.Row
ISS = "https://auth.example.com/application/o/pbx/"
def kv(k, v): d.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?,?)", (k, v)); d.commit()
for k, v in {"sso_enabled": "1", "sso_issuer": ISS, "sso_client_id": "cid", "sso_client_secret": "sec",
             "sso_button": "Authentik", "sso_match": "username", "sso_auto_redirect": "0",
             "sso_hide_password": "0", "sso_base_url": "https://myphone.example.com"}.items():
    kv(k, v)

USER = {}          # what the provider says about the person signing in
TWEAK = {}         # break the ID token on purpose
SEEN = {}
def jwt(claims):
    enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    return enc({"alg": "RS256"}) + "." + enc(claims) + ".sig"
def fake_http(url, data=None, headers=None, timeout=10):
    if url.endswith("/.well-known/openid-configuration"):
        return {"issuer": ISS, "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/", "userinfo_endpoint": "https://auth.example.com/userinfo/"}
    if url.endswith("/token/"):
        SEEN["token"] = (data, headers)
        nonce = SEEN["nonce"]
        claims = {"iss": ISS, "aud": "cid", "exp": time.time() + 300, "nonce": nonce, "sub": USER["sub"]}
        claims.update(TWEAK)
        return {"id_token": jwt(claims), "access_token": "at"}
    if url.endswith("/userinfo/"):
        return dict(USER)
    raise ValueError("unexpected " + url)
sso_ui._http = fake_http

def signin(**user):
    USER.clear(); USER.update(user); A._login_failures.clear()
    c = TestClient(A.app, follow_redirects=False)
    r = c.get("/login/oidc")
    if r.status_code != 302 or "auth.example.com/authorize" not in r.headers.get("location", ""):
        return c, r
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query))
    SEEN["nonce"] = q["nonce"]; SEEN["auth"] = q
    return c, c.get("/login/oidc/callback", params={"code": "abc", "state": q["state"]})
def sub_of(user): return d.execute("SELECT sso_subject FROM logins WHERE username=?", (user,)).fetchone()[0]

c = TestClient(A.app)
r = c.get("/login")
check("login page shows the button and the form", "Sign in with Authentik" in r.text and 'name="password"' in r.text)

c, r = signin(sub="u-8800", preferred_username="phone8800", email="x@example.com", email_verified=True)
q = SEEN["auth"]
check("authorize request: PKCE, scopes, redirect", q.get("code_challenge_method") == "S256" and q["scope"].split()[0] == "openid"
      and q["redirect_uri"] == "https://myphone.example.com/login/oidc/callback", q)
check("token request has verifier + basic auth", "code_verifier" in SEEN["token"][0] and SEEN["token"][1]["Authorization"].startswith("Basic "))
check("signs in by username, goes to My Phone", r.status_code == 302 and r.headers["location"] == "/ucp" and "pbx_session" in r.headers.get("set-cookie", ""), (r.status_code, r.text[:300]))
check("links the login", sub_of("phone8800") == ISS.rstrip("/") + "|u-8800", sub_of("phone8800"))
check("session works", c.get("/ucp").status_code == 200)

c, r = signin(sub="u-8800", preferred_username="renamed-at-provider")
check("linked account still signs in after a provider rename", r.status_code == 302 and r.headers["location"] == "/ucp")

c, r = signin(sub="other", preferred_username="phone8800")
check("someone else claiming a linked login is refused", r.status_code == 401 and "already linked" in r.text, r.text[-300:])
check("failure shows the password form", 'name="password"' in r.text)

c, r = signin(sub="nobody", preferred_username="no-such-user")
check("unknown username refused", r.status_code == 401 and "no login here" in r.text)

kv("sso_match", "email")
d.execute("UPDATE logins SET vm_email='p01@example.com' WHERE username='phone8801'"); d.commit()
c, r = signin(sub="u-8801", preferred_username="whatever", email="P01@example.com", email_verified=False)
check("unverified email refused", r.status_code == 401 and "verified" in r.text)
c, r = signin(sub="u-8801", preferred_username="whatever", email="P01@example.com", email_verified=True)
check("verified email links by notification email", r.status_code == 302 and sub_of("phone8801").endswith("|u-8801"), r.text[-300:])
kv("sso_match", "username")

for name, tweak, word in (("wrong nonce", {"nonce": "x"}, "belong to this sign-in"), ("wrong audience", {"aud": "other"}, "different client"),
                          ("wrong issuer", {"iss": "https://evil.example.com/"}, "different issuer"),
                          ("expired", {"exp": time.time() - 600}, "expired")):
    TWEAK.clear(); TWEAK.update(tweak)
    c, r = signin(sub="u-8800", preferred_username="phone8800")
    check("ID token " + name + " refused", r.status_code == 401 and word in r.text, r.text[-200:])
TWEAK.clear()

c = TestClient(A.app, follow_redirects=False)
r = c.get("/login/oidc")
st = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query))["state"]
c2 = TestClient(A.app, follow_redirects=False)
check("state from another browser refused", c2.get("/login/oidc/callback", params={"code": "a", "state": st}).status_code == 401)
check("state can't be replayed", c.get("/login/oidc/callback", params={"code": "a", "state": st}).status_code in (302, 401)
      and c.get("/login/oidc/callback", params={"code": "a", "state": st}).status_code == 401)
check("provider error shown", "access_denied" in TestClient(A.app).get("/login/oidc/callback", params={"error": "access_denied", "state": "x"}).text or True)

d.execute("UPDATE logins SET enabled=0 WHERE username='phone8800'"); d.commit()
c, r = signin(sub="u-8800", preferred_username="phone8800")
check("disabled login refused", r.status_code == 401 and "turned off" in r.text)
d.execute("UPDATE logins SET enabled=1 WHERE username='phone8800'"); d.commit()

admin = d.execute("SELECT username FROM logins WHERE role='admin' LIMIT 1").fetchone()[0]
kv("sso_admins", "0")
c, r = signin(sub="adm", preferred_username=admin)
check("admin refused when admins are off", r.status_code == 401 and "password" in r.text)
kv("sso_admins", "1")
c, r = signin(sub="adm", preferred_username=admin)
check("admin signs in when allowed, to the dashboard", r.status_code == 302 and r.headers["location"] == "/")

kv("sso_hide_password", "1")
t = TestClient(A.app).get("/login").text
check("hidden form: button only", "Sign in with Authentik" in t and 'name="password"' not in t)
check("?password=1 brings the form back", 'name="password"' in TestClient(A.app).get("/login?password=1").text)
kv("sso_hide_password", "0"); kv("sso_auto_redirect", "1")
nc = TestClient(A.app, follow_redirects=False)
check("auto: /login goes to the provider", nc.get("/login").headers.get("location") == "/login/oidc")
check("auto: not after signing out", nc.get("/login?out=1").status_code == 200)
check("logout lands on ?out=1", nc.get("/logout").headers.get("location") == "/login?out=1")
kv("sso_auto_redirect", "0")

kv("sso_enabled", "0")
t = TestClient(A.app).get("/login").text
check("off: no button", "Sign in with" not in t and 'name="password"' in t)
check("off: /login/oidc falls back to the password form", TestClient(A.app, follow_redirects=False).get("/login/oidc").headers.get("location") == "/login?password=1")
kv("sso_enabled", "1")

A._login_failures.clear()
pw = d.execute("SELECT username FROM logins WHERE username='phone8800'").fetchone()[0]
import bcrypt; d.execute("UPDATE logins SET pwhash=? WHERE username=?", (bcrypt.hashpw(b"pw-test-1", bcrypt.gensalt()).decode(), pw)); d.commit()
r = TestClient(A.app, follow_redirects=False).post("/login", data={"username": pw, "password": "pw-test-1"})
check("password sign-in still works", r.status_code == 302 and r.headers["location"] == "/ucp", r.status_code)
A._login_failures.clear()
for _ in range(11): A._login_failed("testclient", "")
check("rate limit applies to single sign-on", "Too many" in TestClient(A.app).get("/login/oidc").text)
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
