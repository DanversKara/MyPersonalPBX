# SPDX-License-Identifier: GPL-2.0-or-later
"""Outgoing email over SMTP (standard library only).

Settings live in kv_settings (edited on the admin Email page):
  smtp_host, smtp_port, smtp_security (starttls | ssl | none), smtp_user,
  smtp_password, smtp_from, smtp_from_name, vm_email_attach (1/0),
  panel_url (for links in emails)

This file is shipped twice, identical: pbx-api/mailer.py (test emails) and
pbx-brain/mailer.py (voicemail notifications). Keep them in sync.
"""
import os
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

KEYS = ("smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password",
        "smtp_from", "smtp_from_name", "vm_email_attach", "panel_url")
MAX_ATTACH = 10 * 1024 * 1024
_CTRL = re.compile(r"[\r\n\x00-\x1f\x7f]")


class MailError(Exception):
    pass


def settings(c):
    """Read SMTP settings from an sqlite3 connection."""
    rows = c.execute("SELECT key, value FROM kv_settings WHERE key IN (%s)" % ",".join("?" * len(KEYS)),
                     KEYS).fetchall()
    d = {k: "" for k in KEYS}
    d.update({r[0]: r[1] for r in rows})
    return d


def configured(cfg):
    return bool(cfg.get("smtp_host") and cfg.get("smtp_from"))


def clean(v):
    """Strip control characters (header injection) from a value."""
    return _CTRL.sub(" ", str(v or "")).strip()


def send(cfg, to, subject, text, html=None, attachment=None, timeout=20):
    """Send one email. attachment = (filename, bytes, mimetype) or None.
    Raises MailError with a readable reason."""
    if not configured(cfg):
        raise MailError("Email isn't set up (no SMTP server / from address).")
    to = clean(to)
    if not to or "@" not in to:
        raise MailError("No valid recipient address.")
    msg = EmailMessage()
    msg["Subject"] = clean(subject)[:200]
    msg["From"] = formataddr((clean(cfg.get("smtp_from_name")) or "PBX", clean(cfg["smtp_from"])))
    msg["To"] = to
    msg["Message-ID"] = make_msgid(domain=clean(cfg["smtp_from"]).split("@")[-1] or None)
    msg["Date"] = time.strftime("%a, %d %b %Y %H:%M:%S %z")
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    if attachment:
        name, data, mime = attachment
        if data and len(data) <= MAX_ATTACH:
            maintype, _, subtype = (mime or "application/octet-stream").partition("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream",
                               filename=clean(os.path.basename(name)))
    host = clean(cfg["smtp_host"])
    sec = (cfg.get("smtp_security") or "starttls").lower()
    try:
        port = int(cfg.get("smtp_port") or (465 if sec == "ssl" else 587 if sec == "starttls" else 25))
    except ValueError:
        raise MailError("SMTP port must be a number.") from None
    ctx = ssl.create_default_context()
    try:
        if sec == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        with server:
            server.ehlo()
            if sec == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
            if cfg.get("smtp_user"):
                server.login(cfg["smtp_user"], cfg.get("smtp_password") or "")
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise MailError("The SMTP server rejected the username/password.") from None
    except smtplib.SMTPRecipientsRefused:
        raise MailError(f"The SMTP server refused the recipient {to}.") from None
    except smtplib.SMTPNotSupportedError as e:
        raise MailError(f"SMTP server doesn't support that: {e}. Try another security setting.") from None
    except ssl.SSLError as e:
        raise MailError(f"TLS problem ({e.reason or e}). Check the port/security setting.") from None
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"Couldn't send: {e}") from None
