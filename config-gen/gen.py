#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Render Asterisk configs from the PBX DB. Safe: write temp, move atomically.

Called by pbx-api after every mutating change, and by hand when needed:
    python3 config-gen/gen.py && asterisk -rx "pjsip reload"
"""
import os
import re
import sqlite3
import subprocess

from jinja2 import Environment, FileSystemLoader

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("PBX_DB", "/var/lib/pbx/pbx.db")
AST_DIR = os.environ.get("AST_CFG_DIR", "/etc/asterisk")
TPL_DIR = os.path.join(BASE, "templates")


def q(sql, args=()):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def kill_switch_on():
    rows = q("SELECT value FROM kv_settings WHERE key='kill_switch'")
    return bool(rows) and rows[0]["value"] == "1"


def atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, path)


def _clean_str(v):
    """Strip newlines/control chars from a value rendered into pjsip.conf.

    Defense in depth: trunk names/secrets and SIP usernames come from admin
    input. A newline here would inject arbitrary PJSIP sections. The API
    also validates names on input; this makes the renderer safe regardless.
    """
    if not isinstance(v, str):
        return v
    return re.sub(r'[\r\n\x00-\x1f\x7f]', '', v)


def _clean_rows(rows):
    out = [{k: _clean_str(val) for k, val in row.items()} for row in rows]
    for r in out:
        # callerid=Name <number>: a name containing <...> or quotes could
        # replace the number (one phone posing as another extension).
        if isinstance(r.get("display_name"), str):
            r["display_name"] = re.sub(r'[<>"\\;]', "", r["display_name"]).strip()
    return out


def render_pjsip():
    env = Environment(loader=FileSystemLoader(TPL_DIR),
                      keep_trailing_newline=True)
    tpl = env.get_template("pjsip.conf.j2")
    if kill_switch_on():
        # Emergency: no endpoints, no trunk registrations. Asterisk accepts
        # nothing until the switch is released and configs re-rendered.
        extensions, trunks = [], []
    else:
        extensions = _clean_rows(q("SELECT * FROM logins WHERE enabled=1 AND exten!='' ORDER BY exten"))
        trunks = _clean_rows(q("SELECT * FROM trunks WHERE enabled=1 ORDER BY name"))
    out = tpl.render(extensions=extensions, trunks=trunks)
    atomic_write(os.path.join(AST_DIR, "pjsip.conf"), out)


def reload_asterisk():
    # NB: Asterisk 22 removed `pjsip reload`; reload the module instead.
    subprocess.run(["asterisk", "-rx", "module reload res_pjsip.so"], check=True)


if __name__ == "__main__":
    render_pjsip()
    print("wrote pjsip.conf")
