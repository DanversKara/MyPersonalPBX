#!/opt/pbx/api-venv/bin/python
# SPDX-License-Identifier: GPL-2.0-or-later
"""Create (or reset the password of) a panel admin login.

    sudo /opt/pbx/api-venv/bin/python /root/own-pbx/scripts/create-admin.py
    sudo ... create-admin.py --username admin        (prompts for the password)

Run on the PBX server. Use it for the first admin after a fresh install, or
to get back in if you lose the admin password.
"""
import argparse
import getpass
import os
import sqlite3
import sys

import bcrypt

DB = os.environ.get("PBX_DB", "/var/lib/pbx/pbx.db")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--username")
    a = ap.parse_args()
    if not os.path.exists(DB):
        sys.exit(f"{DB} not found - run scripts/deploy.sh first.")
    user = (a.username or input("Admin username [admin]: ").strip() or "admin")
    if not user.replace("_", "").replace("-", "").replace(".", "").isalnum() or len(user) > 40:
        sys.exit("Username: letters, numbers, . _ - only (max 40).")
    while True:
        pw = getpass.getpass("Password (min 12 characters): ")
        if len(pw) < 12:
            print("Too short - use at least 12 characters.")
            continue
        if pw != getpass.getpass("Repeat password: "):
            print("Passwords don't match.")
            continue
        break
    h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    c = sqlite3.connect(DB)
    row = c.execute("SELECT id, role FROM logins WHERE username=?", (user,)).fetchone()
    if row:
        c.execute("UPDATE logins SET pwhash=?, role='admin', enabled=1 WHERE id=?", (h, row[0]))
        what = "password reset (and set to admin)"
    else:
        c.execute("INSERT INTO logins (username, pwhash, role, exten, enabled) VALUES (?,?,'admin','',1)", (user, h))
        what = "created"
    c.execute("INSERT INTO audit (actor, action, detail) VALUES ('console', 'admin.create', ?)", (f"{user}: {what}",))
    c.commit()
    print(f"Admin '{user}' {what}. Sign in at http://<this-server-ip>:8001")


if __name__ == "__main__":
    main()
