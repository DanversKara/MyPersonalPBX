#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""voip.ms SMS/MMS helper for own-pbx.

Best-route design:
- Inbound: voip.ms -> URL Callback (webhook) -> did_sms_routes -> messages table
  -> pbx-brain deliver_message() -> Zoiper (SIP MESSAGE) + My Phone + ESP inbox
- Outbound: ext -> external PSTN number -> voip.ms REST API sendSMS/sendMMS

This module is imported by pbx-api (webhook + admin UI). A copy lives in
pbx-brain/ for the AGI outbound path; keep them in sync.
"""
import re
import urllib.parse
import urllib.request
import json
import logging

log = logging.getLogger("voipms-sms")

API_URL = "https://voip.ms/api/v1/rest.php"


def normalize_digits(phone: str) -> str:
    """Strip to digits only. '+15551234567' -> '15551234567'."""
    if not phone:
        return ""
    d = re.sub(r"\D", "", phone or "")
    return d


def normalize_did(did: str) -> str:
    """Normalize a DID for routing lookups.

    - 10 digits (NANPA) -> prepend 1 -> 11 digits
    - 11 digits starting with 1 -> as-is
    - else digits as-is
    """
    d = normalize_digits(did)
    if len(d) == 10:
        return "1" + d
    return d


def e164(digits: str) -> str:
    """Digits -> +E.164 for display, e.g. '15551234567' -> '+15551234567'."""
    d = normalize_digits(digits)
    if not d:
        return ""
    if len(d) == 10:
        d = "1" + d
    return "+" + d


def is_external_number(dest: str) -> bool:
    """True if dest looks like a PSTN number, not an internal exten.

    Internal exten in own-pbx are short (2-8 digits, e.g. 8801).
    External are 10-15 digits, optionally with leading +.
    """
    if not dest:
        return False
    d = normalize_digits(dest)
    # Must be all digits with optional leading + in original
    if not re.fullmatch(r"\+?[0-9]{10,15}", dest.strip()):
        # also accept plain 10-15 digits
        if not re.fullmatch(r"[0-9]{10,15}", d):
            return False
    # 10-15 digits -> external. Short codes (2-8 digits) are internal.
    return 10 <= len(d) <= 15


def is_internal_exten(dest: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{2,8}", normalize_digits(dest or "")))


def get_voipms_config(c) -> dict:
    """Read voip.ms credentials from kv_settings. Returns dict."""
    cfg = {}
    for k in ("voipms_api_username", "voipms_api_password",
              "voipms_webhook_token", "voipms_default_did"):
        try:
            r = c.execute("SELECT value FROM kv_settings WHERE key=?", (k,)).fetchone()
            cfg[k] = (r["value"] if r else "").strip() if r else ""
        except Exception:
            cfg[k] = ""
    return cfg


def voipms_api_call(api_username: str, api_password: str, params: dict, timeout=20) -> dict:
    """Call voip.ms REST API via GET. Returns parsed JSON dict."""
    q = {
        "api_username": api_username,
        "api_password": api_password,
    }
    q.update(params)
    url = API_URL + "?" + urllib.parse.urlencode(q)
    # Don't log password
    safe_q = {k: ("***" if "password" in k else v) for k, v in q.items()}
    log.info("voip.ms API %s %s", params.get("method"), safe_q.get("did") or safe_q.get("dst") or "")
    req = urllib.request.Request(url, headers={"User-Agent": "own-pbx/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(data)
    except Exception:
        return {"status": "error", "raw": data[:500]}


def send_sms_via_voipms(api_username: str, api_password: str, did: str,
                        dst: str, message: str) -> dict:
    """Send SMS via voip.ms. did/dst as digits, message up to ~1600 chars.

    voip.ms splits long SMS automatically. Returns API response dict.
    """
    did_d = normalize_digits(did)
    # voip.ms expects DID as 10 digits in examples; send 10 digits if NANPA
    if len(did_d) == 11 and did_d.startswith("1"):
        did_send = did_d[1:]
    else:
        did_send = did_d
    dst_d = normalize_digits(dst)
    # dst: voip.ms accepts 10 digits or 11 digits; send as 10 if NANPA
    body = (message or "")[:1600]
    if not body.strip():
        return {"status": "error", "message": "empty message"}
    return voipms_api_call(api_username, api_password, {
        "method": "sendSMS",
        "did": did_send,
        "dst": dst_d,
        "message": body,
    })


def send_mms_via_voipms(api_username: str, api_password: str, did: str,
                        dst: str, message: str, media_url: str = "") -> dict:
    """Send MMS via voip.ms. media_url optional (publicly reachable image)."""
    did_d = normalize_digits(did)
    if len(did_d) == 11 and did_d.startswith("1"):
        did_send = did_d[1:]
    else:
        did_send = did_d
    dst_d = normalize_digits(dst)
    params = {
        "method": "sendMMS",
        "did": did_send,
        "dst": dst_d,
        "message": (message or "")[:2048],
    }
    if media_url:
        params["media_url"] = media_url
    return voipms_api_call(api_username, api_password, params)


def split_dest_exten(s):
    """Comma-separated dest_exten -> list of extension strings."""
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def lookup_sms_route(c, did: str):
    """Find dest_exten for a DID. Tries normalized and raw forms."""
    did_norm = normalize_did(did)
    did_digits = normalize_digits(did)
    for cand in (did_norm, did_digits,
                 did_norm[1:] if len(did_norm) == 11 else None,
                 "1" + did_digits if len(did_digits) == 10 else None):
        if not cand:
            continue
        r = c.execute("SELECT dest_exten, did FROM did_sms_routes WHERE did=?", (cand,)).fetchone()
        if r:
            return r["dest_exten"]
    # fallback: try E.164 with +
    return None


def lookup_sender_did(c, from_exten: str):
    """Pick which DID to use when exten sends an outbound SMS.

    1. DID whose dest_exten == from_exten (inbound route reversed)
    2. voipms_default_did kv_setting
    3. First did_sms_routes row
    """
    digits = "".join(ch for ch in (from_exten or "") if ch.isdigit())
    r = c.execute("SELECT did FROM did_sms_routes WHERE (',' || dest_exten || ',') LIKE ? "
                  "ORDER BY did LIMIT 1",
                  ("%,{},%".format(digits),)).fetchone()
    if r:
        return r["did"]
    try:
        d = c.execute("SELECT value FROM kv_settings WHERE key='voipms_default_did'").fetchone()
        if d and d["value"].strip():
            return d["value"].strip()
    except Exception:
        pass
    r = c.execute("SELECT did FROM did_sms_routes ORDER BY did LIMIT 1").fetchone()
    return r["did"] if r else ""


def lookup_conversation_did(c, exten: str, contact_e164: str) -> str:
    """Which DID is this conversation with an external contact on?

    Checks the most recent message in either direction between the
    extension and the contact. Returns the DID (digits) or "" when unknown.
    Used so replies go out from the same DID the contact texted.
    """
    try:
        r = c.execute(
            "SELECT via_did FROM messages "
            "WHERE ((from_ext=? AND to_ext=?) OR (from_ext=? AND to_ext=?)) "
            "AND via_did IS NOT NULL AND via_did != '' "
            "ORDER BY id DESC LIMIT 1",
            (exten, contact_e164, contact_e164, exten)).fetchone()
        if r and r[0]:
            return str(r[0])
    except Exception:
        pass  # via_did column may not exist yet on old DBs
    return ""
