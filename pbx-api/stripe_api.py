# SPDX-License-Identifier: GPL-2.0-or-later
"""Minimal Stripe REST client (standard library only, no stripe SDK).

Covers what billing.py needs: products, prices, customers, Checkout
Sessions, subscriptions, Customer Portal sessions, and webhook signature
verification.

STRIPE_API_BASE can point at a local fake for testing.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com")
API_VERSION = "2024-06-20"   # pinned: response shapes stay stable


class StripeError(Exception):
    pass


def mode_of(secret_key: str) -> str:
    """'test' | 'live' | '' from the key prefix."""
    k = secret_key or ""
    if k.startswith(("sk_test_", "rk_test_")):
        return "test"
    if k.startswith(("sk_live_", "rk_live_")):
        return "live"
    return ""


def _flatten(params, prefix=""):
    """{'a': {'b': 1}, 'l': [{'x': 2}]} -> [('a[b]','1'), ('l[0][x]','2')]"""
    out = []
    if isinstance(params, dict):
        for k, v in params.items():
            key = f"{prefix}[{k}]" if prefix else str(k)
            out += _flatten(v, key)
    elif isinstance(params, (list, tuple)):
        for i, v in enumerate(params):
            out += _flatten(v, f"{prefix}[{i}]")
    elif params is None:
        pass
    elif isinstance(params, bool):
        out.append((prefix, "true" if params else "false"))
    else:
        out.append((prefix, str(params)))
    return out


class Stripe:
    def __init__(self, secret_key: str):
        if not secret_key:
            raise StripeError("Stripe isn't set up: add your secret key on the Billing page.")
        self.key = secret_key

    def _req(self, method, path, params=None, idempotency_key=None):
        url = API_BASE.rstrip("/") + path
        data = None
        if method == "GET" and params:
            url += "?" + urllib.parse.urlencode(_flatten(params))
        elif params is not None:
            data = urllib.parse.urlencode(_flatten(params)).encode()
        req = urllib.request.Request(url, data=data, method=method)
        auth = base64.b64encode(f"{self.key}:".encode()).decode()
        req.add_header("Authorization", "Basic " + auth)
        req.add_header("Stripe-Version", API_VERSION)
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        if idempotency_key:
            req.add_header("Idempotency-Key", idempotency_key)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get("error", {}).get("message", "")
            except Exception:
                msg = ""
            raise StripeError(msg or f"Stripe returned HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise StripeError(f"Couldn't reach Stripe: {e}") from None

    def get(self, path, params=None):
        return self._req("GET", path, params)

    def post(self, path, params=None, idempotency_key=None):
        return self._req("POST", path, params or {}, idempotency_key)

    # convenience
    def account(self):
        return self.get("/v1/account")


def verify_webhook(payload: bytes, sig_header: str, secret: str, tolerance=300) -> dict:
    """Verify a Stripe-Signature header and return the parsed event.
    Raises StripeError if invalid."""
    if not secret:
        raise StripeError("webhook secret not configured")
    parts = {}
    for item in (sig_header or "").split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts.get("t", ["0"])[0])
    except ValueError:
        raise StripeError("bad signature header") from None
    if abs(time.time() - ts) > tolerance:
        raise StripeError("signature timestamp outside tolerance")
    signed = f"{ts}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, v) for v in parts.get("v1", [])):
        raise StripeError("signature mismatch")
    try:
        return json.loads(payload.decode())
    except Exception:
        raise StripeError("bad payload") from None
