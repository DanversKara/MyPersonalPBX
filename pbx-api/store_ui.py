# SPDX-License-Identifier: GPL-2.0-or-later
"""Storefront: new customers pick a plan (and a phone number), enter their
card on Stripe's own page, and wait for the admin's approval.

Public  /store              plans, numbers for sale, order form
        /store/order        POST: order -> Stripe Checkout (setup mode)
        /store/thanks       back from Stripe: card saved, order awaits approval
Admin   /storefront         settings, orders, Approve / Reject

Nobody is charged and nothing is created until the admin approves. Checkout
runs in *setup* mode: it only saves the card. Approve then:
  1. checks a free extension (the Invites range) and the chosen number,
  2. starts the subscription on the saved card (charged now; a declined
     card stops here, nothing is created),
  3. creates the login (username = extension, generated password and SIP
     secret, voicemail box), routes the number's calls and texts to it,
  4. links the subscription so the plan's features switch on,
  5. emails the customer their login.
Reject only marks the order: the card was never charged.

The public form has no session, so it carries a signed, time-limited token
instead of a CSRF token, plus a honeypot field and the sign-in rate limits.
"""
import hashlib
import hmac
import re
import secrets
import time
import urllib.parse

import bcrypt
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

M = None
B = None            # billing module
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
RESERVE_SECS = 3600  # an unfinished checkout holds its number this long
STATUS = {"started": ("Card not entered", "warn"), "awaiting": ("Awaiting approval", "warn"),
          "approved": ("Approved", "ok"), "rejected": ("Rejected", "bad"), "failed": ("Payment failed", "bad")}


def esc(x):
    return M.esc(x)


def _kv(key, default=""):
    with M.db() as c:
        r = c.execute("SELECT value FROM kv_settings WHERE key=?", (key,)).fetchone()
    return r[0] if r and r[0] is not None else default


def _set(key, value):
    with M.db() as c:
        c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?,?)", (key, str(value)))
        c.commit()


def ensure_tables(c):
    c.execute("""CREATE TABLE IF NOT EXISTS store_orders (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      token TEXT NOT NULL UNIQUE,                -- public reference in the return URL
      status TEXT NOT NULL DEFAULT 'started',    -- started|awaiting|approved|rejected|failed
      name TEXT NOT NULL,
      email TEXT NOT NULL,
      plan_id INTEGER NOT NULL,
      did TEXT NOT NULL DEFAULT '',
      stripe_mode TEXT NOT NULL DEFAULT '',
      customer_id TEXT NOT NULL DEFAULT '',
      checkout_id TEXT NOT NULL DEFAULT '',
      payment_method TEXT NOT NULL DEFAULT '',
      login_id INTEGER DEFAULT NULL,
      exten TEXT NOT NULL DEFAULT '',
      note TEXT NOT NULL DEFAULT '',
      ip TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      decided_at TEXT DEFAULT NULL,
      decided_by TEXT NOT NULL DEFAULT ''
    )""")


def _digits(s):
    return re.sub(r"\D", "", s or "")


def numbers_for_sale():
    return [d for d in (_digits(x) for x in re.split(r"[\s,;]+", _kv("store_dids"))) if d]


def available_numbers():
    """Numbers for sale that no extension, menu or group answers yet and no
    live order holds."""
    out = []
    with M.db() as c:
        held = {r[0] for r in c.execute(
            "SELECT did FROM store_orders WHERE did != '' AND (status='awaiting'"
            " OR (status='started' AND created_at > datetime('now', ?)))", (f"-{RESERVE_SECS} seconds",))}
        for d in numbers_for_sale():
            if d in held:
                continue
            r = c.execute("SELECT ring_extens, ivr_id, group_id FROM inbound_routes WHERE did=?", (d,)).fetchone()
            if r and ((r["ring_extens"] or "[]") not in ("[]", "") or r["ivr_id"] or r["group_id"]):
                continue
            if c.execute("SELECT 1 FROM did_sms_routes WHERE did=? AND dest_exten != ''", (d,)).fetchone():
                continue
            out.append(d)
    return out


def _plans():
    if not B.ready():
        return []
    mode = B.current_mode()
    return [p for p in B._plans(active_only=True) if p["stripe_mode"] == mode and p["stripe_price_id"]]


def _site():
    import panel_templates
    return (panel_templates.get_brand().get("site_name") or "").strip() or "Phone service"


def _base(request):
    return M._invite_base_url(request)


def _secret():
    s = _kv("store_secret")
    if not s:
        s = secrets.token_hex(32)
        _set("store_secret", s)
    return s


def _form_token():
    ts = str(int(time.time()))
    return ts + "." + hmac.new(_secret().encode(), ts.encode(), hashlib.sha256).hexdigest()[:32]


def _form_token_ok(tok):
    ts, _, sig = (tok or "").partition(".")
    if not ts.isdigit() or not 3 <= time.time() - int(ts) <= 3 * 3600:   # bots post instantly
        return False
    want = hmac.new(_secret().encode(), ts.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, want)


def _mail(to, subject, text):
    try:
        import mailer
        with M.db() as c:
            cfg = mailer.settings(c)
        mailer.send(cfg, to, subject, text)
        return True
    except Exception:
        return False


def _notify_admin(subject, text):
    to = (_kv("store_notify") or _kv("sec_alert_to")).strip()
    if to:
        _mail(to, subject, text)


# ------------------------------------------------------------------ public

def _public(title, body, status=200):
    return HTMLResponse(M.page(title, f'<div class="store">{body}</div>'), status_code=status)


def store_page(request: Request):
    err = "Checkout was cancelled — nothing was charged." if request.query_params.get("cancelled") else ""
    return _render(request, err)


def _pretty(d):
    d = _digits(d)
    n = d[1:] if len(d) == 11 and d.startswith("1") else d
    return f"({n[:3]}) {n[3:6]}-{n[6:]}" if len(n) == 10 else d


def _render(request: Request, err: str = "", form: dict | None = None):
    if _kv("store_enabled") != "1":
        return _public("Store", "<h2>Not available</h2><p class='muted'>Sign-ups are closed right now.</p>", 404)
    plans = _plans()
    if not plans:
        return _public("Store", "<h2>Not available</h2><p class='muted'>No plans are on offer right now.</p>", 404)
    form = form or {}
    nums = available_numbers()
    intro = _kv("store_intro").strip()
    terms = _kv("store_terms").strip()
    cards = "".join(
        f'<label class="plan-card"><input type="radio" name="plan" value="{p["id"]}" required '
        f'{"checked" if str(form.get("plan")) == str(p["id"]) or (not form.get("plan") and i == 0) else ""}>'
        f'<span><b>{esc(p["name"])}</b> — {esc(B.money(p["price_cents"], p["currency"]))} / month'
        f'{"<br><span class=muted>" + esc(p["description"]) + "</span>" if p["description"] else ""}'
        f'<ul>{"".join("<li>" + esc(x) + "</li>" for x in B.plan_features(p))}</ul></span></label>'
        for i, p in enumerate(plans))
    num_html = ""
    if numbers_for_sale():
        if nums:
            opts = "".join(f'<option value="{esc(d)}" {"selected" if form.get("did") == d else ""}>'
                           f'{esc(_pretty(d))}</option>' for d in nums)
            num_html = (f'<label>Phone number<br><select name="did"><option value="">No number (extension only)</option>{opts}</select></label>'
                        '<p class="muted">Calls and texts to this number reach your new line.</p>')
        else:
            num_html = '<p class="muted">All phone numbers are taken right now; you can still sign up for an extension.</p>'
    body = f"""<h2>{esc(_kv('store_title') or ('Sign up for ' + _site()))}</h2>
{'<p>' + esc(intro) + '</p>' if intro else ''}
{'<div class="flash bad">' + esc(err) + '</div>' if err else ''}
<form method="post" action="/store/order" class="panel" style="max-width:640px">
<input type="hidden" name="t" value="{_form_token()}">
<div style="position:absolute;left:-5000px" aria-hidden="true"><input name="website" tabindex="-1" autocomplete="off"></div>
<h3>Plan</h3>
<div class="plan-cards">{cards}</div>
{('<h3>Number</h3>' + num_html) if num_html else ''}
<h3>You</h3>
<label>Your name<br><input name="name" value="{esc(form.get('name', ''))}" required maxlength="64" autocomplete="name"></label>
<label>Email<br><input name="email" type="email" value="{esc(form.get('email', ''))}" required maxlength="254" autocomplete="email"></label>
<p class="muted">Your login is sent here once your order is approved.</p>
{('<label class="inline-chk"><input type="checkbox" name="agree" value="1" required> I agree to the terms: ' + esc(terms) + '</label>') if terms else ''}
<p class="muted">Next you enter your card on Stripe's secure page. <b>You aren't charged until your order is approved</b>;
then the plan renews monthly and you can cancel any time from My Phone.</p>
<button class="btn">Continue to secure payment</button>
</form>
<style>.plan-card{{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--border,#444);border-radius:10px;padding:10px 12px;margin:8px 0;cursor:pointer}}
.plan-card ul{{margin:6px 0 0 18px;padding:0}}.plan-card input{{margin:4px 4px 0 0;flex:none;width:auto}}</style>"""
    return _public("Store", body)


async def order(request: Request):
    if _kv("store_enabled") != "1":
        return RedirectResponse("/store", status_code=303)
    ip = M.client_ip(request)
    if not M._login_allowed(ip, "store"):
        return _public("Store", "<h2>Too many attempts</h2><p>Please try again in 15 minutes.</p>", 429)
    f = await request.form()
    form = {k: (f.get(k) or "").strip() for k in ("name", "email", "plan", "did")}
    M._login_failed(ip, "store")          # every order counts toward the per-address limit
    if f.get("website") or not _form_token_ok(f.get("t")):
        return _render(request, "That form expired. Please try again.", form)
    plans = {str(p["id"]): p for p in _plans()}
    plan = plans.get(form["plan"])
    if not plan:
        return _render(request, "Pick a plan.", form)
    if not 1 <= len(form["name"]) <= 64:
        return _render(request, "Enter your name.", form)
    email = form["email"].lower()
    if not EMAIL_RE.match(email):
        return _render(request, "That email address doesn't look right.", form)
    did = _digits(form["did"])
    if did and did not in available_numbers():
        return _render(request, "That number was just taken. Pick another.", form)
    if _kv("store_terms").strip() and not f.get("agree"):
        return _render(request, "Please agree to the terms.", form)
    with M.db() as c:
        if c.execute("SELECT 1 FROM store_orders WHERE lower(email)=? AND status='awaiting'", (email,)).fetchone():
            return _render(request, "There's already an order for this email waiting for approval.", form)
        tok = secrets.token_urlsafe(18)
        oid = c.execute("INSERT INTO store_orders (token, name, email, plan_id, did, stripe_mode, ip) VALUES (?,?,?,?,?,?,?)",
                        (tok, form["name"], email, plan["id"], did, B.current_mode(), ip)).lastrowid
        c.commit()
    base = _base(request)
    try:
        st = B._stripe()
        cust = st.post("/v1/customers", {"email": email, "name": form["name"],
                                          "metadata": {"pbx_store_order": oid}},
                       idempotency_key=f"pbx-store-cust-{tok}")["id"]
        sess = st.post("/v1/checkout/sessions", {
            "mode": "setup", "customer": cust, "payment_method_types": ["card"],
            "metadata": {"pbx_store_order": oid},
            "setup_intent_data": {"metadata": {"pbx_store_order": oid}},
            "success_url": f"{base}/store/thanks?o={tok}",
            "cancel_url": f"{base}/store?cancelled=1"})
    except Exception as e:  # noqa: BLE001 - StripeError or network
        M._audit_log("store", "store.error", str(e)[:200])
        return _render(request, "The payment page couldn't be opened. Please try again later.", form)
    with M.db() as c:
        c.execute("UPDATE store_orders SET customer_id=?, checkout_id=? WHERE id=?", (cust, sess["id"], oid))
        c.commit()
    return RedirectResponse(sess["url"], status_code=303)


def _finish_checkout(order_row, session):
    """Card saved at Stripe: the order waits for approval. Idempotent."""
    if order_row["status"] != "started" or session.get("status") != "complete":
        return False
    pm = ""
    si = session.get("setup_intent")
    if si:
        si_obj = si if isinstance(si, dict) else B._stripe().get(f"/v1/setup_intents/{si}")
        pm = si_obj.get("payment_method") or ""
        if isinstance(pm, dict):
            pm = pm.get("id", "")
    with M.db() as c:
        n = c.execute("UPDATE store_orders SET status='awaiting', payment_method=? WHERE id=? AND status='started'",
                      (pm, order_row["id"])).rowcount
        c.commit()
    if not n:
        return False
    plan = B._plan(order_row["plan_id"]) or {"name": "?"}
    _notify_admin(f"New sign-up waiting: {order_row['name']}",
                  f"{order_row['name']} <{order_row['email']}> ordered {plan['name']}"
                  f"{' with number ' + order_row['did'] if order_row['did'] else ''}.\n\n"
                  "Approve or reject it in the admin panel: Store.\nThe card is saved but not charged until you approve.\n")
    _mail(order_row["email"], f"We got your order — {_site()}",
          f"Hi {order_row['name']},\n\nThanks for signing up for {plan['name']}. Your order is waiting for approval.\n"
          "You haven't been charged yet: the first payment is taken when your account is approved, "
          "and we'll email your login then.\n")
    return True


def thanks(request: Request):
    tok = request.query_params.get("o") or ""
    with M.db() as c:
        o = c.execute("SELECT * FROM store_orders WHERE token=?", (tok,)).fetchone()
    if not o:
        return _public("Store", "<h2>Order not found</h2>", 404)
    if o["status"] == "started" and o["checkout_id"]:
        try:
            _finish_checkout(o, B._stripe().get(f"/v1/checkout/sessions/{o['checkout_id']}"))
        except Exception:  # noqa: BLE001 - the webhook will catch up
            pass
        with M.db() as c:
            o = c.execute("SELECT * FROM store_orders WHERE id=?", (o["id"],)).fetchone()
    if o["status"] == "started":
        return _public("Store", "<h2>Almost there</h2><p>We're waiting for Stripe to confirm your card. "
                                "Refresh this page in a moment.</p>")
    return _public("Store", f"""<h2>Thanks, {esc(o['name'])}!</h2>
<p>Your order is <b>waiting for approval</b>. <b>You haven't been charged yet</b> — the first payment is taken when
your account is approved, and we'll email your login to <b>{esc(o['email'])}</b>.</p>""")


def on_webhook(etype, obj):
    """Called by billing's webhook for store Checkout sessions. True = handled."""
    oid = (obj.get("metadata") or {}).get("pbx_store_order")
    if etype != "checkout.session.completed" or not oid:
        return False
    with M.db() as c:
        o = c.execute("SELECT * FROM store_orders WHERE id=?", (int(oid),)).fetchone()
    if o:
        _finish_checkout(o, obj)
    return True


# ------------------------------------------------------------------ admin

def _admin(request):
    s = M._sess(request)
    return s if s and s["role"] == "admin" else None


def _back(msg="", err=""):
    q = urllib.parse.urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse("/storefront" + ("?" + q if q else ""), status_code=303)


def admin_page(request: Request):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    csrf = M._csrf_field(s)
    flash = ""
    if request.query_params.get("msg"):
        flash += f'<div class="flash ok">{esc(request.query_params["msg"])}</div>'
    if request.query_params.get("err"):
        flash += f'<div class="flash bad">{esc(request.query_params["err"])}</div>'
    on = _kv("store_enabled") == "1"
    plans = _plans()
    with M.db() as c:
        orders = [dict(r) for r in c.execute(
            "SELECT * FROM store_orders WHERE status != 'started' OR created_at > datetime('now','-2 days')"
            " ORDER BY (status='awaiting') DESC, id DESC LIMIT 200")]
    names = {p["id"]: p["name"] for p in B._plans()}
    rows = []
    for o in orders:
        label, cls = STATUS.get(o["status"], (o["status"], ""))
        act = ""
        if o["status"] == "awaiting":
            act = (f"<form method='post' action='/storefront/{o['id']}/approve' class='inline'>{csrf}"
                   f"<button class='btn' onclick=\"return confirm('Charge the card and create the account?')\">Approve</button></form> "
                   f"<form method='post' action='/storefront/{o['id']}/reject' class='inline'>{csrf}"
                   f"<input name='reason' placeholder='Reason (emailed, optional)' style='width:180px'>"
                   f"<button class='btn ghost'>Reject</button></form>")
        elif o["status"] == "approved" and o["exten"]:
            act = f"Ext <b>{esc(o['exten'])}</b>"
        rows.append(f"<tr><td>{esc(M.fmt_ts(o['created_at']))}</td>"
                    f"<td>{esc(o['name'])}<br><span class='muted'>{esc(o['email'])}</span></td>"
                    f"<td>{esc(names.get(o['plan_id'], '?'))}</td><td>{esc(o['did'] or '-')}</td>"
                    f"<td><span class='pill {cls}'>{esc(label)}</span>"
                    f"{'<br><span class=muted>' + esc(o['note']) + '</span>' if o['note'] else ''}</td><td>{act}</td></tr>")
    table = "".join(rows) or "<tr><td colspan='6' class='muted'>No orders yet.</td></tr>"
    link = _base(request) + "/store"
    warn = ""
    if not B.ready():
        warn = '<div class="flash bad">Add your Stripe keys under Billing first.</div>'
    elif not plans:
        warn = '<div class="flash bad">No plans are published to Stripe yet (Billing → plans → Publish).</div>'
    body = f"""{flash}{warn}<h2>Store {'<span class="pill ok">Open</span>' if on else '<span class="pill">Closed</span>'}</h2>
<p class="muted">A public sign-up page: people pick a plan and a number, and enter their card on Stripe's page.
<b>Nobody is charged and nothing is created until you approve.</b> Approve charges the first month, creates the login
(extension from the Invites range, generated password) and routes the number to it, then emails the customer.</p>
<p>Page: <a href="/store" target="_blank">{esc(link)}</a></p>
<h3>Orders</h3>
<div class="scrollbox"><table><tr><th>When</th><th>Who</th><th>Plan</th><th>Number</th><th>Status</th><th></th></tr>{table}</table></div>
<h3 style="margin-top:22px">Settings</h3>
<form method="post" action="/storefront/settings" class="panel" style="max-width:640px">{csrf}
<label class="switch"><input type="checkbox" name="enabled" value="1" {'checked' if on else ''}> <span>Store open</span></label>
<label>Title<br><input name="title" value="{esc(_kv('store_title'))}" placeholder="Sign up for {esc(_site())}" maxlength="80"></label>
<label>Intro (optional)<br><input name="intro" value="{esc(_kv('store_intro'))}" maxlength="300"></label>
<label>Phone numbers for sale<br><input name="dids" value="{esc(', '.join(numbers_for_sale()))}" placeholder="15551234567, 15557654321"></label>
<p class="muted">Your voip.ms numbers, separated by commas. A number is offered only while no extension, menu or group answers it.
Available now: {esc(', '.join(available_numbers()) or 'none')}</p>
<label>Terms the customer must accept (optional)<br><input name="terms" value="{esc(_kv('store_terms'))}" maxlength="300" placeholder="e.g. No 911 without a registered address; fair use applies"></label>
<label>Email new orders to<br><input name="notify" type="email" value="{esc(_kv('store_notify'))}" placeholder="{esc(_kv('sec_alert_to') or 'you@example.com')}"></label>
<p class="muted">Empty = the security alert address (Email page). Customer emails need SMTP set up there too.</p>
<button class="btn">Save</button>
</form>
<p class="muted">Plans offered are the ones marked "Offer to users" and published to Stripe. Extensions come from the Invites range.
New accounts get star codes by plan (Features).</p>"""
    return HTMLResponse(M.page("Store", body, s["username"], s["role"], "store"))


async def _post(request):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    if M._admin_locked():
        return s, M._panel_locked(s, "store")
    return s, None


async def save(request: Request):
    s, locked = await _post(request)
    if locked:
        return locked
    f = await request.form()
    dids = [d for d in (_digits(x) for x in re.split(r"[\s,;]+", f.get("dids") or "")) if d]
    if any(not 7 <= len(d) <= 15 for d in dids):
        return _back(err="Phone numbers should be 7-15 digits, e.g. 15551234567.")
    notify = (f.get("notify") or "").strip()
    if notify and not EMAIL_RE.match(notify):
        return _back(err="The notification email doesn't look right.")
    _set("store_enabled", "1" if f.get("enabled") else "0")
    _set("store_title", (f.get("title") or "").strip()[:80])
    _set("store_intro", (f.get("intro") or "").strip()[:300])
    _set("store_terms", (f.get("terms") or "").strip()[:300])
    _set("store_notify", notify)
    _set("store_dids", ",".join(dict.fromkeys(dids)))
    M._audit_log(s["username"], "store.settings", f"open={bool(f.get('enabled'))} numbers={len(dids)}")
    return _back(msg="Saved.")


def _order(oid):
    with M.db() as c:
        r = c.execute("SELECT * FROM store_orders WHERE id=?", (oid,)).fetchone()
    return dict(r) if r else None


async def approve(request: Request, oid: int):
    s, locked = await _post(request)
    if locked:
        return locked
    o = _order(oid)
    if not o or o["status"] != "awaiting":
        return _back(err="That order isn't waiting for approval.")
    plan = B._plan(o["plan_id"])
    if not plan or not plan["stripe_price_id"] or plan["stripe_mode"] != B.current_mode():
        return _back(err="That plan isn't published to Stripe any more. Reject the order or republish the plan.")
    with M.db() as c:
        exten, sip_u = M._invite_allocate(c)
    if not exten:
        return _back(err="No free extensions in the Invites range. Widen it, then approve again.")
    if o["did"]:
        free = set(available_numbers()) | {o["did"]}
        with M.db() as c:
            r = c.execute("SELECT ring_extens, ivr_id, group_id FROM inbound_routes WHERE did=?", (o["did"],)).fetchone()
        if r and ((r["ring_extens"] or "[]") not in ("[]", "") or r["ivr_id"] or r["group_id"]) or o["did"] not in free:
            return _back(err=f"Number {o['did']} is already in use. Free it (Routes) or reject the order.")
    # 1. Charge: start the subscription on the saved card.
    try:
        params = {"customer": o["customer_id"], "items": [{"price": plan["stripe_price_id"]}],
                  "payment_behavior": "error_if_incomplete",
                  "metadata": {"pbx_store_order": o["id"], "pbx_plan_id": plan["id"]}}
        if o["payment_method"]:
            params["default_payment_method"] = o["payment_method"]
        sub = B._stripe().post("/v1/subscriptions", params, idempotency_key=f"pbx-store-sub-{o['token']}")
    except Exception as e:  # noqa: BLE001
        with M.db() as c:
            c.execute("UPDATE store_orders SET status='failed', note=?, decided_at=datetime('now'), decided_by=?"
                      " WHERE id=?", (("Card declined: " + str(e))[:200], s["username"], o["id"]))
            c.commit()
        M._audit_log(s["username"], "store.charge_failed", f"order={o['id']}: {str(e)[:150]}")
        _mail(o["email"], f"Your order couldn't be completed — {_site()}",
              f"Hi {o['name']},\n\nWe couldn't take the first payment for your order, so no account was created.\n"
              "Please sign up again with another card.\n")
        return _back(err=f"The card was declined, so nothing was created: {str(e)[:150]}")
    if sub.get("status") not in B.ACTIVE:
        M._audit_log(s["username"], "store.charge_incomplete", f"order={o['id']} status={sub.get('status')}")
    # 2. The account.
    password = secrets.token_urlsafe(9)
    sip_secret = secrets.token_urlsafe(24)
    pwh = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with M.db() as c:
        lid = c.execute("INSERT INTO logins (username, pwhash, role, exten, sip_username, sip_secret,"
                        " display_name, vm_email, record_admin, user_record, enabled)"
                        " VALUES (?,?,?,?,?,?,?,?,0,0,1)",
                        (exten, pwh, "user", exten, sip_u, sip_secret, o["name"], o["email"])).lastrowid
        c.execute("INSERT OR IGNORE INTO voicemail_boxes (mailbox, login_id) VALUES (?,?)", (f"vm-{exten}", lid))
        if o["did"]:
            import json
            if c.execute("SELECT 1 FROM inbound_routes WHERE did=?", (o["did"],)).fetchone():
                c.execute("UPDATE inbound_routes SET ring_extens=?, ivr_id=NULL, group_id=NULL, enabled=1 WHERE did=?",
                          (json.dumps([exten]), o["did"]))
            else:
                c.execute("INSERT INTO inbound_routes (did, ring_extens) VALUES (?,?)", (o["did"], json.dumps([exten])))
            c.execute("INSERT INTO did_sms_routes (did, dest_exten, label) VALUES (?,?,?)"
                      " ON CONFLICT(did) DO UPDATE SET dest_exten=excluded.dest_exten, label=excluded.label",
                      (o["did"], exten, o["name"][:40]))
        c.execute("INSERT INTO billing_customers (login_id, stripe_mode, stripe_customer_id) VALUES (?,?,?)"
                  " ON CONFLICT(login_id) DO UPDATE SET stripe_mode=excluded.stripe_mode,"
                  " stripe_customer_id=excluded.stripe_customer_id",
                  (lid, o["stripe_mode"] or B.current_mode(), o["customer_id"]))
        c.execute("UPDATE store_orders SET status='approved', login_id=?, exten=?, decided_at=datetime('now'),"
                  " decided_by=? WHERE id=?", (lid, exten, s["username"], o["id"]))
        c.commit()
    M.ucp.purge_extension_data([exten])
    # 3. Plan features on (also keeps the subscription linked for renewals).
    try:
        B._stripe().post(f"/v1/subscriptions/{sub['id']}", {"metadata": {"pbx_login_id": lid}})
    except Exception:  # noqa: BLE001 - linking by customer id still works
        pass
    B.apply_subscription(lid, sub)
    M._best_effort_apply()
    base = _base(request)
    mailed = _mail(o["email"], f"Your {_site()} account is ready",
                   f"Hi {o['name']},\n\nYour account is approved and your plan ({plan['name']}) is active.\n\n"
                   f"Sign in: {base}/login\nUsername: {exten}\nPassword: {password}\n\n"
                   f"Your extension is {exten}" + (f" and your phone number is {o['did']}" if o["did"] else "") + ".\n"
                   "Please change your password after signing in (My Phone > Settings).\n"
                   "To use the My Phone app or a desk phone, open My Phone > App or Settings > Phone setup.\n")
    M._audit_log(s["username"], "store.approved", f"order={o['id']} ext={exten} did={o['did'] or '-'} mailed={mailed}")
    note = "" if mailed else f" Email failed — give them: username {exten}, password {password}"
    return _back(msg=f"Approved: {o['name']} is extension {exten}" + (f" on {o['did']}" if o["did"] else "") + "." + note)


async def reject(request: Request, oid: int):
    s, locked = await _post(request)
    if locked:
        return locked
    o = _order(oid)
    if not o or o["status"] != "awaiting":
        return _back(err="That order isn't waiting for approval.")
    f = await request.form()
    reason = (f.get("reason") or "").strip()[:300]
    with M.db() as c:
        c.execute("UPDATE store_orders SET status='rejected', note=?, decided_at=datetime('now'), decided_by=?"
                  " WHERE id=?", (reason, s["username"], o["id"]))
        c.commit()
    _mail(o["email"], f"About your order — {_site()}",
          f"Hi {o['name']},\n\nSorry, we can't approve your order"
          + (f": {reason}" if reason else ".") + "\nYour card was not charged.\n")
    M._audit_log(s["username"], "store.rejected", f"order={o['id']} {reason[:80]}")
    return _back(msg=f"Rejected {o['name']}'s order. Their card was never charged.")


def install(app_module, billing_module):
    global M, B
    M, B = app_module, billing_module
    app = M.app
    app.get("/store", response_class=HTMLResponse)(store_page)
    app.post("/store/order")(order)
    app.get("/store/thanks", response_class=HTMLResponse)(thanks)
    app.get("/storefront", response_class=HTMLResponse)(admin_page)
    app.post("/storefront/settings")(save)
    app.post("/storefront/{oid}/approve")(approve)
    app.post("/storefront/{oid}/reject")(reject)
