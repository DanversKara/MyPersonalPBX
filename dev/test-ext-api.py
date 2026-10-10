#!/opt/pbx/brain-venv/bin/python
# SPDX-License-Identifier: GPL-2.0-or-later
"""End-to-end test for the v1 EXT API.

Run on the PBX host as root:
    /opt/pbx/brain-venv/bin/python scripts/test-ext-api.py <ADMIN_API_KEY> [PANEL_PASSWORD]

The admin key is created in the panel: API Keys tab -> Issue new key.
PANEL_PASSWORD (optional) is your admin panel password; passing it also runs
the session/CSRF/XSS checks against the HTML pages.
Everything this test creates via the API it also deletes via the API,
so the DB is left clean. Exits non-zero on the first failure.
"""
import os
import sys

import requests

BASE = os.environ.get("PBX_API_URL", "http://127.0.0.1:8001")
PJSIP_CONF = "/etc/asterisk/pjsip.conf"
TEST_EXTEN = "8899"
# Bump on every fix so a stale copy is obvious from its output.
BUILD_ID = "2026-10-06-2245"

passed = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        sys.exit(1)


def main():
    if len(sys.argv) not in (2, 3):
        print(__doc__)
        sys.exit(2)
    admin_key = sys.argv[1]
    A = {"Authorization": f"Bearer {admin_key}"}
    print(f"== test-ext-api.py build {BUILD_ID} ==")
    print(f"== v1 EXT API tests against {BASE} ==")

    # Pre-flight: clean leftovers from an interrupted run (the test deletes
    # everything it creates, but only if it runs to completion).
    try:
        requests.post(f"{BASE}/api/v1/safety/lock", headers=A, timeout=10,
                      json={"locked": False})
        requests.post(f"{BASE}/api/v1/safety/kill-switch", headers=A, timeout=30,
                      json={"engaged": False})
        requests.delete(f"{BASE}/api/v1/extensions/{TEST_EXTEN}",
                        headers=A, timeout=10)
        r = requests.get(f"{BASE}/api/v1/api-keys", headers=A, timeout=10)
        if r.status_code == 200:
            for k in r.json()["api_keys"]:
                if k["name"] == "test-user-key":
                    requests.delete(f"{BASE}/api/v1/api-keys/{k['id']}",
                                    headers=A, timeout=10)
    except Exception:
        pass

    # --- auth ---
    r = requests.get(f"{BASE}/api/v1/me", headers=A, timeout=10)
    check("admin key authenticates", r.status_code == 200, r.text)
    check("me reports admin role", r.json()["login"]["role"] == "admin", r.text)

    r = requests.get(f"{BASE}/api/v1/me",
                     headers={"Authorization": "Bearer pbx_bogus"}, timeout=10)
    check("bogus key -> 401", r.status_code == 401, r.text)
    r = requests.get(f"{BASE}/api/v1/me", timeout=10)
    check("no key -> 401", r.status_code == 401, r.text)

    # --- create extension ---
    r = requests.post(f"{BASE}/api/v1/extensions", headers=A, timeout=15, json={
        "username": "apitest-ext", "exten": TEST_EXTEN, "display_name": "API Test",
    })
    check("create extension -> 201", r.status_code == 201, r.text)
    body = r.json()
    check("sip_secret returned once at creation",
          isinstance(body.get("sip_secret"), str) and len(body["sip_secret"]) >= 16)
    check("password auto-generated + returned once",
          isinstance(body.get("password"), str) and len(body["password"]) >= 8)
    check("config applied", body.get("applied") is True,
          body.get("apply_error", ""))
    test_pw = body["password"]

    # duplicate exten -> 409
    r = requests.post(f"{BASE}/api/v1/extensions", headers=A, timeout=15, json={
        "username": "apitest-dup", "exten": TEST_EXTEN})
    check("duplicate exten -> 409", r.status_code == 409, r.text)

    # bad exten -> 400
    r = requests.post(f"{BASE}/api/v1/extensions", headers=A, timeout=15, json={
        "username": "apitest-bad", "exten": "abc"})
    check("bad exten -> 400", r.status_code == 400, r.text)

    # --- read (no secrets leak) ---
    r = requests.get(f"{BASE}/api/v1/extensions", headers=A, timeout=10)
    check("list extensions -> 200", r.status_code == 200, r.text)
    exts = r.json()["extensions"]
    check("new extension in list", any(e["exten"] == TEST_EXTEN for e in exts))
    r = requests.get(f"{BASE}/api/v1/extensions/{TEST_EXTEN}", headers=A, timeout=10)
    check("get extension -> 200", r.status_code == 200, r.text)
    check("secrets never in GET responses",
          "sip_secret" not in r.text and "pwhash" not in r.text)
    r = requests.get(f"{BASE}/api/v1/extensions/0000", headers=A, timeout=10)
    check("unknown exten -> 404", r.status_code == 404, r.text)

    # --- asterisk actually picked it up (privileged apply path works) ---
    with open(PJSIP_CONF) as f:
        pjsip = f.read()
    check("endpoint rendered into pjsip.conf", f"[phone{TEST_EXTEN}]\ntype=endpoint" in pjsip)

    # --- patch ---
    r = requests.patch(f"{BASE}/api/v1/extensions/{TEST_EXTEN}", headers=A,
                       timeout=15, json={"display_name": "API Test Renamed"})
    check("patch -> 200 + applied", r.status_code == 200 and
          r.json()["extension"]["display_name"] == "API Test Renamed", r.text)
    r = requests.patch(f"{BASE}/api/v1/extensions/{TEST_EXTEN}", headers=A,
                       timeout=15, json={})
    check("empty patch -> 400", r.status_code == 400, r.text)

    # --- patch custom sip_secret (was silently ignored -> 400 "nothing to update") ---
    r = requests.patch(f"{BASE}/api/v1/extensions/{TEST_EXTEN}", headers=A,
                       timeout=15, json={"sip_secret": "setbyapi123"})
    check("patch sip_secret -> 200, echoed once + applied",
          r.status_code == 200 and r.json().get("sip_secret") == "setbyapi123"
          and r.json().get("applied") is True, r.text)
    with open(PJSIP_CONF) as f:
        pjsip = f.read()
    check("custom secret rendered into pjsip.conf", "setbyapi123" in pjsip)
    r = requests.patch(f"{BASE}/api/v1/extensions/{TEST_EXTEN}", headers=A,
                       timeout=15, json={"sip_secret": "x" * 200})
    check("overlong sip_secret -> 400", r.status_code == 400, r.text)
    r = requests.patch(f"{BASE}/api/v1/extensions/{TEST_EXTEN}", headers=A,
                       timeout=15, json={"sip_secret": ""})
    check("empty sip_secret -> 400 nothing to update", r.status_code == 400, r.text)

    # --- rotate secret ---
    r = requests.post(f"{BASE}/api/v1/extensions/{TEST_EXTEN}/rotate-secret",
                      headers=A, timeout=15)
    check("rotate-secret -> 200, secret returned once",
          r.status_code == 200 and len(r.json().get("sip_secret", "")) >= 16, r.text)
    check("rotate applied", r.json().get("applied") is True)

    # --- reset password + verify via session login ---
    r = requests.post(f"{BASE}/api/v1/extensions/{TEST_EXTEN}/reset-password",
                      headers=A, timeout=10, json={"password": "newpass123"})
    check("reset-password -> 200", r.status_code == 200, r.text)
    r = requests.post(f"{BASE}/auth/login", timeout=10,
                      json={"username": "apitest-ext", "password": "newpass123"})
    check("new password works on /auth/login", r.status_code == 200, r.text)
    r = requests.post(f"{BASE}/auth/login", timeout=10,
                      json={"username": "apitest-ext", "password": test_pw})
    check("old password rejected", r.status_code == 401, r.text)
    r = requests.post(f"{BASE}/api/v1/extensions/{TEST_EXTEN}/reset-password",
                      headers=A, timeout=10, json={"password": "short"})
    check("short password -> 400", r.status_code == 400, r.text)

    # --- sign-in audit ---
    r = requests.get(f"{BASE}/api/v1/signins?limit=50", headers=A, timeout=10)
    check("signins -> 200", r.status_code == 200, r.text)
    signins = r.json()["signins"]
    test_rows = [x for x in signins if x["login"] == "apitest-ext"]
    check("successful login audited",
          any(x["ok"] == 1 for x in test_rows), str(test_rows))
    check("failed login audited",
          any(x["ok"] == 0 for x in test_rows), str(test_rows))
    r = requests.get(f"{BASE}/api/v1/signins?limit=50&failed_only=1",
                     headers=A, timeout=10)
    check("failed_only filter",
          r.status_code == 200 and all(x["ok"] == 0 for x in r.json()["signins"]),
          r.text[:200])

    # --- user-scoped key ---
    r = requests.post(f"{BASE}/api/v1/api-keys", headers=A, timeout=10, json={
        "username": "apitest-ext", "name": "test-user-key"})
    check("create user api key -> 201", r.status_code == 201, r.text)
    user_key = r.json()["key"]
    check("key format", user_key.startswith("pbx_"), user_key[:8])
    U = {"Authorization": f"Bearer {user_key}"}
    r = requests.get(f"{BASE}/api/v1/me", headers=U, timeout=10)
    check("user key authenticates", r.status_code == 200, r.text)
    r = requests.get(f"{BASE}/api/v1/extensions", headers=U, timeout=10)
    check("user sees only own extension",
          r.status_code == 200 and len(r.json()["extensions"]) == 1
          and r.json()["extensions"][0]["exten"] == TEST_EXTEN, r.text)
    r = requests.get(f"{BASE}/api/v1/extensions/8800", headers=U, timeout=10)
    check("user cannot read other extension", r.status_code in (403, 404), r.text)
    r = requests.post(f"{BASE}/api/v1/extensions", headers=U, timeout=10, json={
        "username": "x", "exten": "8877"})
    check("user cannot create extensions -> 403", r.status_code == 403, r.text)
    r = requests.get(f"{BASE}/api/v1/api-keys", headers=U, timeout=10)
    check("user cannot list api keys -> 403", r.status_code == 403, r.text)

    # --- live status endpoints ---
    r = requests.get(f"{BASE}/api/v1/devices", headers=A, timeout=15)
    check("devices -> 200 with list",
          r.status_code == 200 and isinstance(r.json().get("devices"), list), r.text[:200])
    r = requests.get(f"{BASE}/api/v1/calls/live", headers=A, timeout=15)
    check("live calls -> 200 with list",
          r.status_code == 200 and isinstance(r.json().get("calls"), list), r.text[:200])
    r = requests.get(f"{BASE}/api/v1/devices", headers=U, timeout=15)
    check("user devices -> 200 (own only)",
          r.status_code == 200 and isinstance(r.json().get("devices"), list), r.text[:200])
    r = requests.get(f"{BASE}/api/v1/calls/live", headers=U, timeout=15)
    check("user live calls -> 200", r.status_code == 200, r.text[:200])
    r = requests.get(f"{BASE}/api/v1/signins", headers=U, timeout=10)
    check("user cannot read signins -> 403", r.status_code == 403, r.text)

    # --- revoke key ---
    key_id = requests.get(f"{BASE}/api/v1/api-keys", headers=A,
                          timeout=10).json()["api_keys"]
    key_id = next(k["id"] for k in key_id if k["name"] == "test-user-key")
    r = requests.delete(f"{BASE}/api/v1/api-keys/{key_id}", headers=A, timeout=10)
    check("revoke key -> 200", r.status_code == 200, r.text)
    r = requests.get(f"{BASE}/api/v1/me", headers=U, timeout=10)
    check("revoked key -> 401", r.status_code == 401, r.text)

    # --- safety: kill switch + safety lock ---
    r = requests.get(f"{BASE}/api/v1/safety", headers=A, timeout=10)
    check("safety state", r.status_code == 200 and
          r.json() == {"kill_switch": False, "safety_lock": False, "lock_mode": "off"}, r.text)
    r = requests.post(f"{BASE}/api/v1/safety/lock", headers=A, timeout=10,
                      json={"locked": True})
    check("lock on", r.json() == {"safety_lock": True, "lock_mode": "full"}, r.text)
    r = requests.post(f"{BASE}/api/v1/extensions", headers=A, timeout=10, json={
        "username": "locked-ext", "exten": "8877"})
    check("create blocked when locked -> 403", r.status_code == 403, r.text)
    r = requests.post(f"{BASE}/api/v1/api-keys", headers=A, timeout=10,
                      json={"username": "admin"})
    check("key create blocked when locked -> 403", r.status_code == 403, r.text)
    # kill switch bypasses the lock (emergency)
    r = requests.post(f"{BASE}/api/v1/safety/kill-switch", headers=A, timeout=30,
                      json={"engaged": True})
    check("kill switch engages while locked",
          r.status_code == 200 and r.json()["kill_switch"] is True, r.text[:200])
    check("kill switch applied", r.json().get("applied") is True,
          r.json().get("apply_error", ""))
    with open(PJSIP_CONF) as f:
        pjsip = f.read()
    check("pjsip emptied under kill switch",
          "[phone" not in pjsip and "ep-trunk" not in pjsip,
          [l for l in pjsip.split("\n") if l.startswith("[")][:6])
    r = requests.post(f"{BASE}/api/v1/safety/kill-switch", headers=A, timeout=30,
                      json={"engaged": False})
    check("kill switch released", r.json()["kill_switch"] is False, r.text[:200])
    with open(PJSIP_CONF) as f:
        pjsip = f.read()
    check("trunk endpoint restored after release", "ep-trunk-voipms" in pjsip)
    r = requests.post(f"{BASE}/api/v1/safety/lock", headers=A, timeout=10,
                      json={"locked": False})
    check("unlocked", r.json() == {"safety_lock": False, "lock_mode": "off"}, r.text)
    # admin-only lock: admin API frozen, mode reported
    r = requests.post(f"{BASE}/api/v1/safety/lock", headers=A, timeout=10,
                      json={"locked": True, "mode": "admin"})
    check("admin-only lock on", r.json() == {"safety_lock": True, "lock_mode": "admin"}, r.text)
    r = requests.post(f"{BASE}/api/v1/extensions", headers=A, timeout=10, json={
        "username": "locked-ext", "exten": "8877"})
    check("create blocked under admin-only lock -> 403", r.status_code == 403, r.text)
    r = requests.post(f"{BASE}/api/v1/safety/lock", headers=A, timeout=10,
                      json={"locked": True, "mode": "bogus"})
    check("bad lock mode -> 422", r.status_code == 422, r.text[:200])
    r = requests.post(f"{BASE}/api/v1/safety/lock", headers=A, timeout=10,
                      json={"locked": False})
    check("unlocked again", r.json()["lock_mode"] == "off", r.text)
    r = requests.post(f"{BASE}/api/v1/extensions", headers=A, timeout=15, json={
        "username": "unlocked-ext", "exten": "8877"})
    check("mutations work after unlock -> 201", r.status_code == 201, r.text[:200])
    r = requests.delete(f"{BASE}/api/v1/extensions/8877", headers=A, timeout=15)
    check("cleanup unlocked-ext", r.status_code == 200, r.text[:200])

    # --- delete extension (cascades) ---
    r = requests.delete(f"{BASE}/api/v1/extensions/{TEST_EXTEN}", headers=A, timeout=15)
    check("delete extension -> 200 + applied",
          r.status_code == 200 and r.json().get("applied") is True, r.text)
    r = requests.get(f"{BASE}/api/v1/extensions/{TEST_EXTEN}", headers=A, timeout=10)
    check("deleted extension -> 404", r.status_code == 404, r.text)
    with open(PJSIP_CONF) as f:
        pjsip = f.read()
    check("endpoint removed from pjsip.conf", f"[phone{TEST_EXTEN}]" not in pjsip)

    print(f"\nALL {len(passed)} TESTS PASSED")
    return A


def security_tests(headers, admin_pw=None):
    """Live security checks: XSS escaping, CSRF, input validation."""
    A = headers
    passed = []
    def check(name, cond, detail=""):
        assert cond, f"FAIL: {name} {detail}"
        passed.append(name)
        print(f"  ok: {name}")

    print(f"\n== security tests against {BASE} ==")
    # Preflight: clean leftovers from an interrupted run
    for e in ("8878", "8879"):
        try:
            requests.delete(f"{BASE}/api/v1/extensions/{e}", headers=A, timeout=15)
        except Exception:
            pass
    # --- XSS: malicious display name must be escaped in panel HTML ---
    xss = "<script>alert(1)</script>"
    r = requests.post(f"{BASE}/api/v1/extensions", headers=A, timeout=15, json={
        "username": "xss-test", "exten": "8878", "display_name": xss})
    check("create xss-test ext -> 201", r.status_code == 201, r.text[:200])
    # panel session for HTML pages (needs admin password as 2nd CLI arg)
    if admin_pw:
        s = requests.Session()
        r = s.post(f"{BASE}/login", data={"username": "admin", "password": admin_pw}, timeout=10)
        assert s.cookies.get("pbx_session"), "panel login failed"
        r = s.get(f"{BASE}/logins", timeout=10)
        check("XSS payload escaped in /logins",
              xss not in r.text and "&lt;script&gt;" in r.text)
        # CSRF: panel POST without token -> 403
        r = s.post(f"{BASE}/logins/new/edit",
                   data={"username": "csrf-test", "password": "x12345678", "role": "user"},
                   timeout=10)
        check("CSRF blocks token-less panel POST -> 403", r.status_code == 403, r.text[:200])
        # v1 via session without CSRF header -> 403
        r = s.post(f"{BASE}/api/v1/safety/lock", json={"locked": True}, timeout=10)
        check("CSRF blocks token-less v1 session POST -> 403", r.status_code == 403, r.text[:200])
    else:
        print("  skip: panel session tests (pass admin password as 2nd arg to enable)")
    # --- input validation: config injection rejected ---
    r = requests.post(f"{BASE}/api/v1/extensions", headers=A, timeout=15, json={
        "username": "inj-test", "exten": "8879", "sip_username": "pwn\n[evil]"})
    check("sip_username newline injection -> 400", r.status_code == 400, r.text[:200])
    # --- cleanup ---
    for e in ("8878", "8879"):
        requests.delete(f"{BASE}/api/v1/extensions/{e}", headers=A, timeout=15)
    print(f"\nALL {len(passed)} SECURITY TESTS PASSED")


if __name__ == "__main__":
    headers = main()
    # Optional 2nd arg: admin panel password -> enables session/CSRF tests
    security_tests(headers, sys.argv[2] if len(sys.argv) > 2 else None)
