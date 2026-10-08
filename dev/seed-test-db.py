#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Seed the sandbox test DB: one admin login + two phone logins (8800, 8801).

Logins ARE the extensions (1:1) — no separate extensions table.
"""
import os
import sqlite3

import bcrypt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("PBX_DB", "/var/lib/pbx/pbx.db")
SCHEMA = os.path.join(BASE, "db", "schema.sql")


def main():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    with open(SCHEMA) as f:
        c.executescript(f.read())

    admin_hash = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode()
    c.execute("INSERT OR IGNORE INTO logins (username, pwhash, role)"
              " VALUES (?,?,?)", ("admin", admin_hash, "admin"))

    for exten, user in (("8800", "phone8800"), ("8801", "phone8801")):
        c.execute(
            "INSERT OR REPLACE INTO logins (username, pwhash, role, exten, sip_username,"
            " sip_secret, display_name)"
            " VALUES (?,?,?,?,?,?,?)",
            (user, admin_hash, "user", exten, user, f"secret-{exten}",
             f"Test Phone {exten}"))
        c.execute("INSERT OR IGNORE INTO voicemail_boxes (mailbox, login_id)"
                  " VALUES (?, (SELECT id FROM logins WHERE username=?))",
                  (f"vm-{exten}", user))
    c.execute("INSERT OR IGNORE INTO kv_settings (key, value) VALUES "
              "('feature_vm','*97'), ('feature_spy','*555')")
    c.commit()
    print("seeded:", DB)
    for r in c.execute("SELECT exten, sip_username FROM logins WHERE exten!=''"
                       " ORDER BY exten"):
        print("  ext", r[0], "user", r[1])


if __name__ == "__main__":
    main()
