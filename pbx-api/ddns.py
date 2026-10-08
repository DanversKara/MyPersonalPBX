# SPDX-License-Identifier: GPL-2.0-or-later
"""Dynamic DNS: keep a Cloudflare A record pointed at this site's public IP.

Runs inside pbx-api as a background thread (every ddns_interval seconds):
  1. detect the public IPv4 (several services, first valid answer wins)
  2. read the Cloudflare A record for the SIP domain
  3. if they differ, update the record (DNS only - never proxied - TTL 60)
  4. log the change (ip_changes table) and email the admin if set

Cloudflare's proxy can't carry SIP or RTP, so the record is always forced to
"DNS only". The Kamailio edge box updates its own advertised IP with its
pbx-edge-ip.timer (kamailio/update-public-ip.sh); both sit behind the same
router so they see the same IP.

Settings (kv_settings): sip_domain, cf_token, cf_record (default sip_domain),
ddns_enabled, ddns_interval, ddns_notify. Status: ddns_public_ip,
ddns_dns_ip, ddns_last_check, ddns_last_error, cf_zone_id, cf_record_id.
"""
import ipaddress
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CF_API = os.environ.get("CF_API_BASE", "https://api.cloudflare.com/client/v4")
IP_SERVICES = [u for u in os.environ.get(
    "PBX_IP_SERVICES",
    "https://api.ipify.org,https://ipv4.icanhazip.com,https://cloudflare.com/cdn-cgi/trace").split(",") if u]
TTL = 60
MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL = 60, 3600, 120

M = None
_lock = threading.Lock()


class DDNSError(Exception):
    pass


# ---------------------------------------------------------------- settings

def _kv(key, default=""):
    with M.db() as c:
        r = c.execute("SELECT value FROM kv_settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def _set(key, value):
    with M.db() as c:
        c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?,?)", (key, str(value)))
        c.commit()


def record_name():
    return (_kv("cf_record") or _kv("sip_domain")).strip().lower().rstrip(".")


def interval():
    try:
        v = int(_kv("ddns_interval") or DEFAULT_INTERVAL)
    except ValueError:
        v = DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, v))


# ---------------------------------------------------------------- public IP

def _valid_public_v4(s):
    try:
        ip = ipaddress.ip_address((s or "").strip())
    except ValueError:
        return None
    return str(ip) if ip.version == 4 and ip.is_global else None


def detect_public_ip(timeout=8):
    """First valid public IPv4 from the services. Raises DDNSError."""
    errors = []
    for url in IP_SERVICES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "own-pbx-ddns/1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read(4096).decode(errors="replace")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{urllib.parse.urlparse(url).netloc}: {e}")
            continue
        if "ip=" in text:  # cloudflare trace format
            text = next((ln[3:] for ln in text.splitlines() if ln.startswith("ip=")), "")
        ip = _valid_public_v4(text)
        if ip:
            return ip
        errors.append(f"{urllib.parse.urlparse(url).netloc}: not a public IPv4")
    raise DDNSError("Couldn't detect the public IP (" + "; ".join(errors)[:300] + ")")


# ---------------------------------------------------------------- Cloudflare

def _cf(method, path, token, body=None, params=None):
    url = CF_API.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            out = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read().decode() or "{}")
        except Exception:
            out = {}
        msg = "; ".join(x.get("message", "") for x in out.get("errors", []) if isinstance(x, dict)) or f"HTTP {e.code}"
        if e.code in (401, 403):
            msg = "Cloudflare refused the API token (" + msg + "). It needs Zone → DNS → Edit for this zone."
        raise DDNSError(msg) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DDNSError(f"Couldn't reach Cloudflare: {e}") from None
    if not out.get("success", False):
        msg = "; ".join(x.get("message", "") for x in out.get("errors", []) if isinstance(x, dict))
        raise DDNSError(msg or "Cloudflare returned an error")
    return out.get("result")


def verify_token(token):
    """Returns a short description of what the token can see. Raises DDNSError."""
    zones = _cf("GET", "/zones", token, params={"per_page": 50}) or []
    if not zones:
        raise DDNSError("The token works but can't see any zones. Give it access to your domain's zone.")
    return ", ".join(z.get("name", "?") for z in zones[:10])


def find_zone(token, name):
    """Zone id for a record name, trying sip.example.co.uk -> example.co.uk -> co.uk."""
    parts = name.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        res = _cf("GET", "/zones", token, params={"name": cand}) or []
        if res:
            return res[0]["id"], res[0]["name"]
    raise DDNSError(f"No Cloudflare zone found for {name}. Is the domain on this Cloudflare account?")


def get_record(token, zone_id, name):
    res = _cf("GET", f"/zones/{zone_id}/dns_records", token, params={"type": "A", "name": name}) or []
    return res[0] if res else None


def upsert_record(token, zone_id, name, ip, record=None):
    body = {"type": "A", "name": name, "content": ip, "ttl": TTL, "proxied": False,
            "comment": "own-pbx dynamic IP"}
    if record:
        return _cf("PATCH", f"/zones/{zone_id}/dns_records/{record['id']}", token, body)
    return _cf("POST", f"/zones/{zone_id}/dns_records", token, body)


# ---------------------------------------------------------------- one check

def check_now(force=False):
    """Run one check/update. Returns a dict describing what happened."""
    if not _lock.acquire(timeout=30):
        return {"ok": False, "error": "another check is running"}
    try:
        return _check(force)
    finally:
        _lock.release()


def _check(force):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    token, name = _kv("cf_token"), record_name()
    result = {"ok": False, "changed": False, "public_ip": "", "dns_ip": "", "at": now}
    try:
        ip = detect_public_ip()
        result["public_ip"] = ip
        _set("ddns_public_ip", ip)
        if not token or not name:
            raise DDNSError("Add your domain and a Cloudflare API token to update DNS automatically.")
        zone_id = _kv("cf_zone_id")
        zone_for = _kv("cf_zone_for")
        if not zone_id or zone_for != name:
            zone_id, _ = find_zone(token, name)
            _set("cf_zone_id", zone_id)
            _set("cf_zone_for", name)
        rec = get_record(token, zone_id, name)
        old = rec["content"] if rec else ""
        result["dns_ip"] = old
        needs = (not rec or old != ip or rec.get("proxied") or force)
        if needs:
            upsert_record(token, zone_id, name, ip, rec)
            result["dns_ip"] = ip
            if old != ip:
                result["changed"] = True
                _log_change(old, ip)
        _set("ddns_dns_ip", result["dns_ip"])
        _set("ddns_last_error", "")
        result["ok"] = True
    except DDNSError as e:
        result["error"] = str(e)
        _set("ddns_last_error", f"{now}: {e}")
    _set("ddns_last_check", now)
    return result


def _log_change(old, new):
    with M.db() as c:
        c.execute("INSERT INTO ip_changes (old_ip, new_ip) VALUES (?,?)", (old or "", new))
        c.execute("DELETE FROM ip_changes WHERE id NOT IN (SELECT id FROM ip_changes ORDER BY id DESC LIMIT 200)")
        c.commit()
    to = _kv("ddns_notify").strip()
    if not to:
        return
    try:
        import mailer
        with M.db() as c:
            cfg = mailer.settings(c)
        if mailer.configured(cfg):
            mailer.send(cfg, to, f"Public IP changed: {new}",
                        f"Your phone system's public IP changed from {old or '(none)'} to {new}.\n"
                        f"The DNS record {record_name()} now points to {new} (TTL {TTL}s).\n\n"
                        "Phones re-register on their own within a couple of minutes. If remote phones "
                        "have no audio, check that the Kamailio edge box picked up the new IP "
                        "(systemctl status pbx-edge-ip.timer).\n")
    except Exception:
        pass


# ---------------------------------------------------------------- background loop

def _loop():
    time.sleep(15)  # let the app finish starting
    while True:
        try:
            if _kv("ddns_enabled") == "1":
                check_now()
        except Exception:
            pass
        time.sleep(interval())


def install(app_module):
    global M
    M = app_module

    @M.app.on_event("startup")
    def _start_ddns():
        threading.Thread(target=_loop, daemon=True, name="ddns").start()
