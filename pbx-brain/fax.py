# SPDX-License-Identifier: GPL-2.0-or-later
"""Fax on an ordinary phone number (no separate fax line).

A number with "Detect faxes" on is answered straight away; the caller hears
ringing while Asterisk listens for a few seconds (FAXOPT(faxdetect)). A fax
machine's calling tone (CNG) or a T.38 offer jumps to the dialplan's `fax`
extension, which receives it (ReceiveFAX); anything else carries on to
pbx-brain as a normal call. See config-gen/static/extensions.conf [trunk-in].

This module is used by agi_server.py:
  fax-check <did>         -> how many seconds to listen (0 = off)
  fax-done  <file> ...    -> TIFF -> PDF, store, email (after the call ends)
"""
import json
import logging
import os
import sqlite3
import subprocess
import threading
import time

DB = os.environ.get("PBX_DB", "/var/lib/pbx/pbx.db")
FAX_DIR = os.environ.get("PBX_FAX_DIR", "/var/spool/pbx/fax")
WAIT_CHOICES = (3, 4, 5, 6, 8)
log = logging.getLogger("pbx-fax")


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def did_norm(v):
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def route_for(c, did):
    """The inbound route for a DID, matched like pbx-brain does."""
    try:
        r = c.execute("SELECT * FROM inbound_routes WHERE did=? AND enabled=1", (did,)).fetchone()
        if r:
            return r
        want = did_norm(did)
        for r in c.execute("SELECT * FROM inbound_routes WHERE enabled=1 ORDER BY id"):
            if did_norm(r["did"]) == want:
                return r
    except sqlite3.OperationalError:
        pass
    return None


def fax_allowed(c, login) -> bool:
    """Faxing is a plan feature (mirrors pbx-api entitlements.quota 'fax').
    Admins always may."""
    try:
        if login["role"] == "admin":
            return True
        q = c.execute("SELECT quota FROM user_entitlements WHERE login_id=? AND feature='fax'",
                      (login["id"],)).fetchone()
        if q is not None:
            return int(q[0]) != 0
        d = c.execute("SELECT value FROM kv_settings WHERE key='default_quota_fax'").fetchone()
        return bool(d and str(d[0]).strip() not in ("", "0"))
    except (sqlite3.OperationalError, IndexError, KeyError, ValueError):
        return False


def fax_wait(did) -> int:
    """Seconds to listen for a fax on this number; 0 = fax detection off.
    Off too when the people the number rings have no fax in their plan (a
    number that rings no one, e.g. a menu run by the admin, still gets it)."""
    try:
        with db() as c:
            r = route_for(c, did)
            if not r or not r["fax_detect"]:
                return 0
            who = recipients(c, r)
            if who and not any(fax_allowed(c, u) for u in who):
                return 0
            w = int(r["fax_wait"] or 4)
    except (sqlite3.OperationalError, IndexError, KeyError, ValueError):
        return 0
    return w if w in WAIT_CHOICES else 4


def recipients(c, route):
    """Logins a fax on this number belongs to: whoever the number rings."""
    if not route:
        return []
    extens = []
    try:
        extens = [str(x) for x in json.loads(route["ring_extens"] or "[]")]
    except (ValueError, TypeError):
        pass
    gid = route["group_id"] if "group_id" in route.keys() else None
    if gid:
        g = c.execute("SELECT members FROM ring_groups WHERE id=?", (gid,)).fetchone()
        if g:
            try:
                extens += [str(x) for x in json.loads(g["members"] or "[]")]
            except (ValueError, TypeError):
                pass
    out, seen = [], set()
    for e in extens:
        r = c.execute("SELECT * FROM logins WHERE exten=? AND enabled=1", (e,)).fetchone()
        if r and r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out


def to_pdf(tif):
    """TIFF -> PDF next to it (tiff2pdf). Returns the PDF path or ''."""
    pdf = os.path.splitext(tif)[0] + ".pdf"
    try:
        subprocess.run(["tiff2pdf", "-o", pdf, tif], check=True, timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return pdf if os.path.getsize(pdf) > 0 else ""
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("fax: PDF conversion failed for %s: %s", tif, e)
        return ""


def _inside(path):
    real = os.path.realpath(path or "")
    return real.startswith(os.path.realpath(FAX_DIR) + os.sep)


def fax_done(tif, status, pages, error, did, caller, station):
    """A fax call ended: record it (received or failed) and email it.
    Returns the new fax id (or None)."""
    ok = (status or "").upper() == "SUCCESS" and tif and _inside(tif) and os.path.isfile(tif)
    try:
        pages_n = int(pages or 0)
    except ValueError:
        pages_n = 0
    pdf = to_pdf(tif) if ok else ""
    if pdf:
        try:
            os.remove(tif)          # the PDF is what everyone uses
            tif = ""
        except OSError:
            pass
    keep = pdf or (tif if tif and _inside(tif) and os.path.isfile(tif) else "")
    size = os.path.getsize(keep) if keep else 0
    with db() as c:
        route = route_for(c, did)
        who = [u for u in recipients(c, route) if fax_allowed(c, u)]
        fid = c.execute(
            "INSERT INTO faxes (did, caller, remote_station, pages, status, error, path, bytes)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (did_norm(did) or did, caller or "", (station or "").strip()[:60], pages_n,
             "ok" if ok and keep else "failed", (error or "")[:200], keep, size)).lastrowid
        for u in who:
            c.execute("INSERT OR IGNORE INTO fax_recipients (fax_id, login_id) VALUES (?,?)", (fid, u["id"]))
        c.commit()
        to = []
        if route is not None and (route["fax_email"] or "").strip():
            to += [x.strip() for x in route["fax_email"].replace(";", ",").split(",") if x.strip()]
        if route is not None and route["fax_email_users"]:
            to += [u["vm_email"] for u in who if (u["vm_email"] or "").strip()]
    to = list(dict.fromkeys(to))
    log.info("fax %s from %s on %s: %s, %d page(s), %s", fid, caller, did,
             "received" if ok and keep else "FAILED", pages_n, error or "")
    if to:
        threading.Thread(target=_email, args=(fid, to), daemon=True, name="fax-email").start()
    return fid


def _email(fid, to):
    import mailer
    try:
        with db() as c:
            f = c.execute("SELECT * FROM faxes WHERE id=?", (fid,)).fetchone()
            cfg = mailer.settings(c)
    except Exception:
        log.exception("fax email: lookup failed")
        return
    if not f or not mailer.configured(cfg):
        return
    who = mailer.clean(f["caller"]) or "Unknown sender"
    if f["remote_station"]:
        who += f" ({mailer.clean(f['remote_station'])})"
    when = time.strftime("%a %b %d, %Y at %I:%M %p")
    link = (cfg.get("panel_url") or "").rstrip("/")
    if f["status"] == "ok":
        subject = f"Fax from {who} ({f['pages']} page{'s' if f['pages'] != 1 else ''})"
        text = (f"You received a fax on {f['did']}.\n\nFrom:     {who}\nReceived: {when}\n"
                f"Pages:    {f['pages']}\n\n"
                + (f"View it online: {link}/ucp/faxes\n" if link else "")
                + "The fax is attached as a PDF.\n")
        attach = None
        try:
            if f["path"] and os.path.getsize(f["path"]) <= mailer.MAX_ATTACH:
                with open(f["path"], "rb") as fh:
                    attach = (f"fax-{time.strftime('%Y%m%d-%H%M')}.pdf", fh.read(), "application/pdf")
            elif f["path"]:
                text += "\n(Too large to attach: open it online.)\n"
        except OSError:
            pass
    else:
        subject = f"Fax from {who} didn't come through"
        text = (f"A fax call to {f['did']} started but the fax wasn't received.\n\nFrom:     {who}\n"
                f"When:     {when}\nReason:   {f['error'] or 'unknown'}\n\nAsk the sender to try again.\n")
        attach = None
    for addr in to:
        try:
            mailer.send(cfg, addr, subject, text, attachment=attach)
            log.info("fax %s emailed to %s", fid, addr)
        except mailer.MailError as e:
            log.warning("fax %s email to %s failed: %s", fid, addr, e)
