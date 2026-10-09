# SPDX-License-Identifier: GPL-2.0-or-later
"""Stripe billing: monthly plans that grant feature quotas (IVR menus).

Admin  /billing          Stripe keys, plans (published to Stripe), subscribers
User   /ucp/billing      pick a plan (Stripe Checkout), switch, cancel/resume,
                         manage card (Stripe Customer Portal)
Stripe /stripe/webhook   subscription changes -> quotas (signed, idempotent)

How access is granted: a subscription in an active-like state (active,
trialing, past_due) sets user_entitlements(ivr_menus) = plan.ivr_menus with
source 'plan:<id>'. Any other state (canceled, unpaid, incomplete...) removes
that plan-sourced row, so the user falls back to the default (0 = no free
menus). Admin-set rows (source 'admin') are only replaced by a purchase.

Three paths keep this in sync, so a missed webhook never strands a user:
1. Returning from Checkout (?session_id=...) syncs immediately.
2. The webhook applies every subscription change Stripe sends.
3. Opening the billing page re-syncs if the last sync is over a minute old,
   and admins can "Sync all from Stripe".
"""
import html as _html
import sqlite3
import time
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import entitlements as ent
from stripe_api import Stripe, StripeError, mode_of, verify_webhook

M = None
ACTIVE = ("active", "trialing", "past_due")
PLAN_FEATURES = ("call_minutes", "voicemail", "messages", "recording", "ivr_menus")
SYNC_EVERY = 60
STATUS_LABEL = {"active": ("Active", "ok"), "trialing": ("Trial", "ok"),
                "past_due": ("Payment overdue", "warn"), "unpaid": ("Unpaid", "bad"),
                "canceled": ("Cancelled", "bad"), "incomplete": ("Awaiting payment", "warn"),
                "incomplete_expired": ("Expired", "bad"), "paused": ("Paused", "warn")}
USER_FLASH = {
    "subscribed": ("ok", "Thanks! Your plan is active."),
    "pending": ("warn", "Payment received by Stripe; your plan will switch on in a moment. Refresh if it doesn't."),
    "switched": ("ok", "Plan changed. Any difference is prorated on your next invoice."),
    "canceled": ("ok", "Your plan will end at the end of the current period."),
    "resumed": ("ok", "Your plan will renew as normal."),
    "cancelled_checkout": ("warn", "Checkout cancelled, nothing was charged."),
    "notready": ("bad", "Billing isn't available yet. Please try again later."),
    "error": ("bad", "Something went wrong talking to the payment provider. Please try again."),
    "noplan": ("bad", "That plan isn't available."),
    "locked": ("bad", "Changes are disabled right now (the administrator has the safety lock on)."),
}


def esc(x):
    return M.esc(x)


# ---------------------------------------------------------------- settings / helpers

def _kv(key, default=""):
    with M.db() as c:
        r = c.execute("SELECT value FROM kv_settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def _set_kv(key, value):
    with M.db() as c:
        c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?,?)", (key, value))
        c.commit()


def _stripe():
    return Stripe(_kv("stripe_secret"))


def current_mode():
    return mode_of(_kv("stripe_secret"))


def ready():
    return bool(current_mode())


def money(cents, currency="usd"):
    cur = (currency or "usd").lower()
    sym = {"usd": "$", "cad": "CA$", "aud": "A$", "eur": "€", "gbp": "£"}.get(cur)
    amt = f"{int(cents) / 100:,.2f}"
    return f"{sym}{amt}" if sym else f"{amt} {cur.upper()}"


def _date(ts):
    try:
        return time.strftime("%b %d, %Y", time.localtime(int(ts))) if ts else ""
    except (TypeError, ValueError):
        return ""


def _base_url(request):
    u = _kv("billing_public_url").strip().rstrip("/")
    return u or str(request.base_url).rstrip("/")


def _plans(active_only=False):
    with M.db() as c:
        q = "SELECT * FROM billing_plans" + (" WHERE active=1" if active_only else "") + " ORDER BY sort_order, price_cents, id"
        return [dict(r) for r in c.execute(q)]


def _plan(pid):
    with M.db() as c:
        r = c.execute("SELECT * FROM billing_plans WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def plan_by_price(price_id):
    if not price_id:
        return None
    with M.db() as c:
        r = c.execute("SELECT * FROM billing_plans WHERE stripe_price_id=?", (price_id,)).fetchone()
        if not r:
            r = c.execute("SELECT p.* FROM billing_plan_prices x JOIN billing_plans p ON p.id=x.plan_id"
                          " WHERE x.stripe_price_id=?", (price_id,)).fetchone()
    return dict(r) if r else None


def _cust(login_id):
    with M.db() as c:
        r = c.execute("SELECT * FROM billing_customers WHERE login_id=?", (login_id,)).fetchone()
    return dict(r) if r else None


def _cust_by_stripe(customer_id):
    with M.db() as c:
        r = c.execute("SELECT * FROM billing_customers WHERE stripe_customer_id=?", (customer_id,)).fetchone()
    return dict(r) if r else None


def _audit(actor, action, detail=""):
    try:
        with M.db() as c:
            c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)", (actor, action, detail))
            c.commit()
    except Exception:
        pass


# ---------------------------------------------------------------- Stripe sync

def publish_plan(plan, old=None):
    """Make sure the plan exists in the current Stripe account with the right
    price. Prices are immutable: a new amount creates a new price and the
    old one is archived (existing subscribers keep paying the old price
    until they switch; billing_plan_prices maps it back to the plan)."""
    st = _stripe()
    mode = current_mode()
    pid = plan["id"]
    product = plan["stripe_product_id"] if plan["stripe_mode"] == mode else ""
    price = plan["stripe_price_id"] if plan["stripe_mode"] == mode else ""
    desc = plan["description"] or (", ".join(plan_features(plan)) or "Internal calls")
    if product:
        st.post(f"/v1/products/{product}", {"name": plan["name"], "description": desc})
    else:
        product = st.post("/v1/products", {"name": plan["name"], "description": desc,
                                           "metadata": {"pbx_plan_id": pid}},
                          idempotency_key=f"pbx-product-{mode}-{pid}")["id"]
        price = ""
    amount_changed = bool(old and (old["price_cents"] != plan["price_cents"] or old["currency"] != plan["currency"]))
    if not price or amount_changed:
        new_price = st.post("/v1/prices", {
            "product": product, "unit_amount": int(plan["price_cents"]),
            "currency": plan["currency"], "recurring": {"interval": "month"},
            "metadata": {"pbx_plan_id": pid}})["id"]
        if price:
            try:
                st.post(f"/v1/prices/{price}", {"active": False})
            except StripeError:
                pass
        price = new_price
    with M.db() as c:
        c.execute("UPDATE billing_plans SET stripe_mode=?, stripe_product_id=?, stripe_price_id=? WHERE id=?",
                  (mode, product, price, pid))
        c.execute("INSERT OR IGNORE INTO billing_plan_prices (stripe_price_id, plan_id) VALUES (?,?)", (price, pid))
        c.commit()


def _customer_for(login):
    """Stripe customer id for a login in the current mode (created if needed)."""
    mode = current_mode()
    row = _cust(login["id"])
    if row and row["stripe_customer_id"] and row["stripe_mode"] == mode:
        return row["stripe_customer_id"]
    params = {"name": login["display_name"] or login["username"],
              "metadata": {"pbx_login_id": login["id"], "pbx_username": login["username"]}}
    if login.get("vm_email"):
        params["email"] = login["vm_email"]
    cid = _stripe().post("/v1/customers", params,
                         idempotency_key=f"pbx-cust-{mode}-{login['id']}")["id"]
    with M.db() as c:
        c.execute("INSERT INTO billing_customers (login_id, stripe_mode, stripe_customer_id) VALUES (?,?,?)"
                  " ON CONFLICT(login_id) DO UPDATE SET stripe_mode=excluded.stripe_mode,"
                  " stripe_customer_id=excluded.stripe_customer_id, subscription_id='',"
                  " subscription_item_id='', plan_id=NULL, status='', current_period_end=0,"
                  " cancel_at_period_end=0, updated_at=datetime('now')",
                  (login["id"], mode, cid))
        c.commit()
    return cid


def _sub_fields(sub):
    items = ((sub.get("items") or {}).get("data") or [{}])
    item = items[0] if items else {}
    price = (item.get("price") or {}).get("id", "")
    period_end = sub.get("current_period_end") or item.get("current_period_end") or 0
    return item.get("id", ""), price, int(period_end or 0)


def plan_features(plan):
    """['500 outside call minutes / month', 'Voicemail', ...] for a plan."""
    return [d for d in (ent.describe(f, plan.get(f) or 0) for f in PLAN_FEATURES) if d]


def _grant_plan(c, login_id, plan):
    for f in PLAN_FEATURES:
        ent.set_quota(c, login_id, f, int(plan.get(f) or 0), source=f"plan:{plan['id']}")


def _revoke_plan_quota(c, login_id):
    """Remove plan-granted limits (admin-set ones stay)."""
    c.execute("DELETE FROM user_entitlements WHERE login_id=? AND source LIKE 'plan:%'", (login_id,))


def apply_subscription(login_id, sub):
    """Store a subscription and grant/revoke the plan's quotas."""
    item_id, price, period_end = _sub_fields(sub)
    status = sub.get("status", "")
    plan = plan_by_price(price)
    row = _cust(login_id) or {}
    # Ignore a stale non-active subscription when another one is active.
    if (status not in ACTIVE and row.get("subscription_id") and row["subscription_id"] != sub.get("id")
            and row.get("status") in ACTIVE):
        return
    with M.db() as c:
        c.execute("INSERT INTO billing_customers (login_id, stripe_mode, stripe_customer_id) VALUES (?,?,?)"
                  " ON CONFLICT(login_id) DO NOTHING",
                  (login_id, current_mode(), sub.get("customer", "")))
        c.execute("UPDATE billing_customers SET subscription_id=?, subscription_item_id=?, plan_id=?, status=?,"
                  " current_period_end=?, cancel_at_period_end=?, synced_at=?, updated_at=datetime('now')"
                  " WHERE login_id=?",
                  (sub.get("id", ""), item_id, plan["id"] if plan else None, status, period_end,
                   1 if sub.get("cancel_at_period_end") else 0, int(time.time()), login_id))
        if status in ACTIVE and plan:
            _grant_plan(c, login_id, plan)
        else:
            _revoke_plan_quota(c, login_id)
        c.commit()


def _apply_none(login_id):
    with M.db() as c:
        c.execute("UPDATE billing_customers SET subscription_id='', subscription_item_id='', plan_id=NULL,"
                  " status='', current_period_end=0, cancel_at_period_end=0, synced_at=? WHERE login_id=?",
                  (int(time.time()), login_id))
        _revoke_plan_quota(c, login_id)
        c.commit()


def sync_login(login_id):
    """Pull the customer's subscriptions from Stripe and apply the best one."""
    row = _cust(login_id)
    if not row or not row["stripe_customer_id"] or row["stripe_mode"] != current_mode():
        return
    subs = _stripe().get("/v1/subscriptions", {"customer": row["stripe_customer_id"],
                                                "status": "all", "limit": 20}).get("data", [])
    if not subs:
        _apply_none(login_id)
        return
    subs.sort(key=lambda s: (s.get("status") in ACTIVE, s.get("created", 0)), reverse=True)
    apply_subscription(login_id, subs[0])


# ---------------------------------------------------------------- webhook

async def webhook(request: Request):
    payload = await request.body()
    try:
        event = verify_webhook(payload, request.headers.get("stripe-signature", ""),
                               _kv("stripe_webhook_secret"))
    except StripeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    eid, etype = event.get("id", ""), event.get("type", "")
    with M.db() as c:
        if c.execute("SELECT 1 FROM billing_events WHERE id=?", (eid,)).fetchone():
            return {"ok": True, "duplicate": True}
    obj = (event.get("data") or {}).get("object") or {}
    try:
        if etype == "checkout.session.completed":
            login_id = int(obj.get("client_reference_id") or (obj.get("metadata") or {}).get("pbx_login_id") or 0)
            if login_id and obj.get("customer"):
                with M.db() as c:
                    c.execute("INSERT INTO billing_customers (login_id, stripe_mode, stripe_customer_id)"
                              " VALUES (?,?,?) ON CONFLICT(login_id) DO UPDATE SET"
                              " stripe_customer_id=excluded.stripe_customer_id, stripe_mode=excluded.stripe_mode",
                              (login_id, current_mode(), obj["customer"]))
                    c.commit()
                if obj.get("subscription"):
                    sub = _stripe().get(f"/v1/subscriptions/{obj['subscription']}")
                    apply_subscription(login_id, sub)
        elif etype.startswith("customer.subscription."):
            row = _cust_by_stripe(obj.get("customer", ""))
            login_id = row["login_id"] if row else int((obj.get("metadata") or {}).get("pbx_login_id") or 0)
            if login_id:
                apply_subscription(login_id, obj)
        elif etype in ("invoice.paid", "invoice.payment_failed"):
            row = _cust_by_stripe(obj.get("customer", ""))
            if row:
                sync_login(row["login_id"])
    except StripeError as e:
        # Let Stripe retry later.
        return JSONResponse({"error": str(e)}, status_code=502)
    with M.db() as c:
        try:
            c.execute("INSERT INTO billing_events (id, type) VALUES (?,?)", (eid, etype))
            c.commit()
        except sqlite3.IntegrityError:
            pass
    return {"ok": True}


# ---------------------------------------------------------------- user pages

def _status_pill(status):
    label, cls = STATUS_LABEL.get(status, (status or "None", ""))
    return f'<span class="pill {cls}">{esc(label)}</span>'


def user_page(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        return RedirectResponse("/login")
    sid = request.query_params.get("session_id")
    if sid and ready():
        msg = "pending"
        try:
            sess = _stripe().get(f"/v1/checkout/sessions/{urllib.parse.quote(sid)}",
                                 {"expand": ["subscription"]})
            if str(sess.get("client_reference_id")) == str(me["id"]) and isinstance(sess.get("subscription"), dict):
                apply_subscription(me["id"], sess["subscription"])
                if sess["subscription"].get("status") in ACTIVE:
                    msg = "subscribed"
        except StripeError:
            pass
        return RedirectResponse(f"/ucp/billing?bmsg={msg}", status_code=303)
    row = _cust(me["id"])
    if ready() and row and row["stripe_customer_id"] and time.time() - (row["synced_at"] or 0) > SYNC_EVERY:
        try:
            sync_login(me["id"])
            row = _cust(me["id"])
        except StripeError:
            pass
    flash = ""
    key = request.query_params.get("bmsg", "")
    if key in USER_FLASH:
        cls, text = USER_FLASH[key]
        flash = f'<div class="flash {"ok" if cls == "ok" else "bad" if cls == "bad" else ""}">{esc(text)}</div>'
    if not ready():
        return M.ucp._render(request, s, me, "ucp-bill", "Billing",
                             flash + '<p class="muted">Plans aren\'t available yet. The administrator still needs to set up billing.</p>')
    plans = _plans(active_only=True)
    csrf = M._csrf_field(s)
    subscribed = bool(row and row["status"] in ACTIVE and row["subscription_id"])
    cur_plan = _plan(row["plan_id"]) if row and row["plan_id"] else None
    usage_html = usage_panel(me)
    if subscribed:
        ends = _date(row["current_period_end"])
        when = (f"Ends on {ends}" if row["cancel_at_period_end"] else f"Renews on {ends}") if ends else ""
        action = (f'<form method="post" action="/ucp/billing/resume" class="inline">{csrf}<button class="btn">Keep my plan</button></form>'
                  if row["cancel_at_period_end"] else
                  f'<form method="post" action="/ucp/billing/cancel" class="inline" onsubmit="return confirm(\'Cancel at the end of this period? Outside calls, voicemail, texts, recording and IVR menus switch off then.\')">{csrf}'
                  f'<button class="btn ghost">Cancel plan</button></form>')
        current = f"""<section class="panel current-plan">
<div class="lbl muted">YOUR PLAN</div>
<h3>{esc(cur_plan["name"] if cur_plan else "Unknown plan")} {_status_pill(row["status"])}</h3>
<p>{esc(money(cur_plan["price_cents"], cur_plan["currency"])) + " / month · " if cur_plan else ""}{esc(when)}</p>
{usage_html}
{'<p class="bad">Your last payment failed. Update your card to keep your plan.</p>' if row["status"] == "past_due" else ""}
<div class="row-actions">{action}
<form method="post" action="/ucp/billing/portal" class="inline">{csrf}<button class="btn ghost">Payment method &amp; invoices</button></form></div>
</section>"""
    else:
        current = (f'<section class="panel current-plan"><div class="lbl muted">YOUR PLAN</div><h3>No plan</h3>'
                   f'<p class="muted">Without a plan you can call other extensions on this system. Pick a plan below for '
                   f'outside calls, voicemail, texts, call recording and IVR menus.</p>{usage_html}</section>')
    cards = ""
    for p in plans:
        is_cur = subscribed and cur_plan and cur_plan["id"] == p["id"]
        if is_cur:
            btn = '<button class="btn" disabled>Current plan</button>'
        else:
            label = "Switch to this plan" if subscribed else "Subscribe"
            js_name = esc(p["name"].replace("\\", "").replace("'", "\\'"))
            confirm = (f' onsubmit="return confirm(\'Switch to {js_name}? The price difference is prorated.\')"'
                       if subscribed else "")
            btn = (f'<form method="post" action="/ucp/billing/subscribe/{p["id"]}"{confirm}>{csrf}'
                   f'<button class="btn">{label}</button></form>')
        cards += f"""<div class="plan {'on' if is_cur else ''}">
<h3>{esc(p["name"])}</h3>
<div class="price">{esc(money(p["price_cents"], p["currency"]))}<span>/month</span></div>
<ul class="feat">{"".join("<li>" + esc(x) + "</li>" for x in (["Internal calls"] + plan_features(p)))}</ul>
<p class="muted">{esc(p["description"])}</p>
{btn}</div>"""
    if not cards:
        cards = '<p class="muted">No plans are offered right now.</p>'
    test_note = ('<p class="muted">Test mode: no real charges. Use card 4242 4242 4242 4242, any future date and any CVC.</p>'
                 if current_mode() == "test" else "")
    body = f"""{flash}{current}
<h3>Plans</h3>
<div class="plans">{cards}</div>
{test_note}
<p class="muted">Payments are handled securely by Stripe. Card details never touch this server.</p>"""
    return M.ucp._render(request, s, me, "ucp-bill", "Billing", body)


def usage_panel(me):
    """This month's limits and usage for a user (used on Billing + Overview)."""
    rows = []
    with M.db() as c:
        if ent.is_admin(c, me["id"]):
            return '<p class="muted">Admin account: no limits.</p>'
        for f in PLAN_FEATURES:
            q = ent.quota(c, me["id"], f)
            label, kind = ent.FEATURES[f]
            if kind == "bool":
                val = '<span class="ok">Included</span>' if q else '<span class="muted">Not included</span>'
            else:
                used = ent.usage(c, me["id"], f)
                unit = {"call_minutes": " min", "messages": "", "ivr_menus": ""}[f]
                if q == ent.UNLIMITED:
                    val = f"{used:,}{unit} used · unlimited"
                elif q <= 0:
                    val = '<span class="muted">Not included</span>'
                else:
                    pct = min(100, int(used * 100 / q)) if q else 0
                    cls = "bad" if used >= q else "warn" if pct >= 80 else ""
                    val = (f'<span class="{cls}">{used:,} of {q:,}{unit}</span>'
                           f'<span class="meter"><span style="width:{pct}%"></span></span>')
            rows.append(f"<tr><td>{esc(label)}</td><td>{val}</td></tr>")
    return ('<table class="kv usage"><caption class="muted">This month (resets on the 1st)</caption>'
            + "".join(rows) + "</table>")


async def _user_post(request):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    return s, me


def _back(key):
    return RedirectResponse(f"/ucp/billing?bmsg={key}", status_code=303)


async def user_subscribe(request: Request, plan_id: int):
    s, me = await _user_post(request)
    if M.ucp._locked():
        return _back("locked")
    if not ready():
        return _back("notready")
    plan = _plan(plan_id)
    if not plan or not plan["active"] or plan["stripe_mode"] != current_mode() or not plan["stripe_price_id"]:
        return _back("noplan")
    row = _cust(me["id"])
    try:
        if row and row["status"] in ACTIVE and row["subscription_id"] and row["stripe_mode"] == current_mode():
            # Switch plan on the existing subscription (prorated).
            sub = _stripe().post(f"/v1/subscriptions/{row['subscription_id']}", {
                "items": [{"id": row["subscription_item_id"], "price": plan["stripe_price_id"]}],
                "proration_behavior": "create_prorations", "cancel_at_period_end": False})
            apply_subscription(me["id"], sub)
            _audit(me["username"], "billing.switch", f"plan={plan_id}")
            return _back("switched")
        cust = _customer_for(me)
        base = _base_url(request)
        sess = _stripe().post("/v1/checkout/sessions", {
            "mode": "subscription", "customer": cust,
            "line_items": [{"price": plan["stripe_price_id"], "quantity": 1}],
            "client_reference_id": me["id"],
            "metadata": {"pbx_login_id": me["id"], "pbx_plan_id": plan_id},
            "subscription_data": {"metadata": {"pbx_login_id": me["id"], "pbx_plan_id": plan_id}},
            "allow_promotion_codes": True,
            "success_url": base + "/ucp/billing?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": base + "/ucp/billing?bmsg=cancelled_checkout"})
        _audit(me["username"], "billing.checkout", f"plan={plan_id}")
        return RedirectResponse(sess["url"], status_code=303)
    except StripeError as e:
        _audit(me["username"], "billing.error", str(e)[:200])
        return _back("error")


async def _set_cancel(request, flag):
    s, me = await _user_post(request)
    if M.ucp._locked():
        return _back("locked")
    row = _cust(me["id"])
    if not ready() or not row or not row["subscription_id"]:
        return _back("error")
    try:
        sub = _stripe().post(f"/v1/subscriptions/{row['subscription_id']}", {"cancel_at_period_end": flag})
        apply_subscription(me["id"], sub)
    except StripeError:
        return _back("error")
    _audit(me["username"], "billing.cancel" if flag else "billing.resume")
    return _back("canceled" if flag else "resumed")


async def user_cancel(request: Request):
    return await _set_cancel(request, True)


async def user_resume(request: Request):
    return await _set_cancel(request, False)


async def user_portal(request: Request):
    s, me = await _user_post(request)
    if M.ucp._locked():
        return _back("locked")
    row = _cust(me["id"])
    if not ready() or not row or not row["stripe_customer_id"]:
        return _back("error")
    try:
        p = _stripe().post("/v1/billing_portal/sessions", {"customer": row["stripe_customer_id"],
                                                           "return_url": _base_url(request) + "/ucp/billing"})
    except StripeError:
        return _back("error")
    return RedirectResponse(p["url"], status_code=303)


# ---------------------------------------------------------------- admin pages

def _admin(request):
    s = M._sess(request)
    return s if s and s["role"] == "admin" else None


def _mask(v):
    return (v[:8] + "…" + v[-4:]) if v and len(v) > 14 else ("set" if v else "")


def admin_page(request: Request):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    csrf = M._csrf_field(s)
    mode = current_mode()
    sk, wh = _kv("stripe_secret"), _kv("stripe_webhook_secret")
    msg = request.query_params.get("msg", "")
    err = request.query_params.get("err", "")
    flash = ""
    if msg:
        flash = f'<div class="flash ok">{esc(msg)}</div>'
    if err:
        flash += f'<div class="flash bad">{esc(err)}</div>'
    badge = {"test": '<span class="pill warn">TEST MODE</span>', "live": '<span class="pill ok">LIVE</span>'}.get(
        mode, '<span class="pill bad">Not set up</span>')
    hook = _base_url(request) + "/stripe/webhook"
    plans = _plans()
    prow = ""
    for p in plans:
        published = p["stripe_mode"] == mode and p["stripe_price_id"]
        offered = '<span class="pill ok">Offered</span>' if p["active"] else '<span class="pill">Hidden</span>'
        pub = ('<span class="pill ok">Published</span>' if published
               else '<span class="pill bad">Not in Stripe</span>')
        with M.db() as c:
            subs = c.execute("SELECT COUNT(*) FROM billing_customers WHERE plan_id=? AND status IN ('active','trialing','past_due')",
                             (p["id"],)).fetchone()[0]
        prow += (f'<tr><td><b>{esc(p["name"])}</b><br><span class="muted">{esc(p["description"])}</span></td>'
                 f'<td class="muted">{esc(", ".join(plan_features(p)))}</td><td>{esc(money(p["price_cents"], p["currency"]))}/mo</td>'
                 f'<td>{offered}</td><td>{pub}</td>'
                 f'<td>{subs}</td><td><a class="btn ghost" href="/billing/plans/{p["id"]}/edit">Edit</a>'
                 + ("" if published or not mode else
                    f' <form method="post" action="/billing/plans/{p["id"]}/publish" class="inline">{csrf}<button class="link-btn">Publish</button></form>')
                 + "</td></tr>")
    with M.db() as c:
        subs = [dict(r) for r in c.execute(
            "SELECT b.*, l.username, l.exten, p.name AS plan_name FROM billing_customers b"
            " JOIN logins l ON l.id=b.login_id LEFT JOIN billing_plans p ON p.id=b.plan_id"
            " WHERE b.subscription_id!='' ORDER BY l.username")]
    dash = "https://dashboard.stripe.com/" + ("test/" if mode == "test" else "")
    srow = "".join(
        f'<tr><td>{esc(x["username"])} <span class="muted">{esc(x["exten"])}</span></td><td>{esc(x["plan_name"] or "?")}</td>'
        f'<td>{_status_pill(x["status"])}{" <span class=muted>ends</span>" if x["cancel_at_period_end"] else ""}</td>'
        f'<td>{esc(_date(x["current_period_end"]))}</td>'
        f'<td><a href="{dash}customers/{esc(x["stripe_customer_id"])}" target="_blank" rel="noopener">Stripe ↗</a></td></tr>'
        for x in subs) or '<tr><td colspan="5" class="muted">No subscribers yet.</td></tr>'
    body = f"""{flash}<h2>Billing {badge}</h2>
<div class="grid2">
<section class="panel">
<h3>Stripe account</h3>
<form method="post" action="/billing/settings">{csrf}
<label>Secret key<br><input name="stripe_secret" type="password" autocomplete="off" placeholder="{esc(_mask(sk)) or 'sk_test_…'}"></label>
<p class="muted">Stripe Dashboard → Developers → API keys. Use the <b>test</b> key (sk_test_…) until you're ready to take real payments. Leave empty to keep the current key.</p>
<label>Webhook signing secret<br><input name="stripe_webhook_secret" type="password" autocomplete="off" placeholder="{esc(_mask(wh)) or 'whsec_…'}"></label>
<label>Public panel address (optional)<br><input name="billing_public_url" value="{esc(_kv('billing_public_url'))}" placeholder="https://pbx.example.com"></label>
<p class="muted">Where users return after paying, and where Stripe sends webhooks. Empty = the address you're using now.</p>
<button class="btn">Save</button>
</form>
<form method="post" action="/billing/test" class="inline">{csrf}<button class="btn ghost" {'disabled' if not mode else ''}>Test connection</button></form>
<form method="post" action="/billing/sync" class="inline">{csrf}<button class="btn ghost" {'disabled' if not mode else ''}>Sync all from Stripe</button></form>
</section>
<section class="panel">
<h3>Webhook</h3>
<p>In Stripe → Developers → Webhooks, add an endpoint:</p>
<p><code>{esc(hook)}</code></p>
<p>Events: <code>checkout.session.completed</code>, <code>customer.subscription.created</code>,
<code>customer.subscription.updated</code>, <code>customer.subscription.deleted</code>,
<code>invoice.paid</code>, <code>invoice.payment_failed</code>. Then paste its signing secret on the left.</p>
<p class="muted">Stripe must be able to reach that address over HTTPS, so forward only <code>/stripe/webhook</code>
through your reverse proxy (e.g. NPMplus). Until then, purchases still activate when users return from checkout,
and renewals/cancellations update when anyone opens the billing pages or you press "Sync all".</p>
<p class="muted">Also turn on the Customer Portal (Settings → Billing → Customer portal) so users can update cards and see invoices.</p>
</section>
</div>
<h3>Plans</h3>
<table><tr><th>Plan</th><th>Includes</th><th>Price</th><th>Users see it</th><th>Stripe</th><th>Subscribers</th><th></th></tr>
{prow or '<tr><td colspan="7" class="muted">No plans yet.</td></tr>'}</table>
<p><a class="btn" href="/billing/plans/new/edit">New plan</a></p>
<h3>Subscribers</h3>
<table><tr><th>User</th><th>Plan</th><th>Status</th><th>Renews / ends</th><th></th></tr>{srow}</table>
{access_section(s)}"""
    return HTMLResponse(M.page("Billing", body, s["username"], s["role"], "billing"))


def _lim_input(name, value, default_text):
    v = "" if value is None else ("unlimited" if value == ent.UNLIMITED else str(value))
    return f'<input name="{name}" value="{v}" placeholder="{esc(default_text)}" size="9">'


def _onoff_select(name, value):
    opts = [("", "default"), ("1", "on"), ("0", "off")]
    cur = "" if value is None else ("1" if value else "0")
    return f'<select name="{name}">' + "".join(
        f'<option value="{k}" {"selected" if k == cur else ""}>{t}</option>' for k, t in opts) + "</select>"


def access_section(s):
    """Defaults for users without a plan + per-user overrides (admin)."""
    csrf = M._csrf_field(s)
    with M.db() as c:
        d = {f: ent.default_quota(c, f) for f in PLAN_FEATURES}
        users = [dict(r) for r in c.execute("SELECT id, username, exten FROM logins WHERE role='user' ORDER BY exten, username")]
        rows = []
        for u in users:
            ov = {r[0]: (r[1], r[2]) for r in c.execute(
                "SELECT feature, quota, source FROM user_entitlements WHERE login_id=?", (u["id"],))}
            plan_src = next((src for (_, src) in ov.values() if str(src).startswith("plan:")), None)
            admin_ov = {f: v for f, (v, src) in ov.items() if not str(src).startswith("plan:")}
            if plan_src:
                pl = _plan(int(plan_src.split(":")[1]))
                cells = f'<td colspan="5" class="muted">On plan <b>{esc(pl["name"] if pl else "?")}</b> (limits come from the plan)</td><td></td>'
            else:
                cells = (f'<td>{_lim_input("call_minutes", admin_ov.get("call_minutes"), "default")}</td>'
                         f'<td>{_onoff_select("voicemail", admin_ov.get("voicemail"))}</td>'
                         f'<td>{_lim_input("messages", admin_ov.get("messages"), "default")}</td>'
                         f'<td>{_onoff_select("recording", admin_ov.get("recording"))}</td>'
                         f'<td>{_lim_input("ivr_menus", admin_ov.get("ivr_menus"), "default")}</td>'
                         f'<td><button class="link-btn">Save</button></td>')
            if not plan_src:
                fid = f'acc{u["id"]}'
                cells = (cells.replace('<input name=', f'<input form="{fid}" name=')
                              .replace('<select name=', f'<select form="{fid}" name=')
                              .replace('<button class="link-btn">', f'<button class="link-btn" form="{fid}">'))
                rows.append(f'<tr><td>{esc(u["username"])} <span class="muted">{esc(u["exten"])}</span>'
                            f'<form id="{fid}" method="post" action="/billing/access/{u["id"]}">{csrf}</form></td>{cells}</tr>')
            else:
                rows.append(f'<tr><td>{esc(u["username"])} <span class="muted">{esc(u["exten"])}</span></td>{cells}</tr>')
    fmt = lambda f: ("unlimited" if d[f] == ent.UNLIMITED else str(d[f]))
    return f"""<h3>User access</h3>
<p class="muted">What users get <b>without a plan</b>, and per-user exceptions (e.g. staff or free accounts).
Calls between extensions are always free; admins are never limited. Numbers: 0 = not included, "unlimited" = no limit.
Empty = use the default. While a user has an active plan, the plan decides.</p>
<form method="post" action="/billing/access-defaults" class="panel access-defaults">{csrf}
<b>Defaults (no plan):</b>
<label>Outside minutes {_lim_input("call_minutes", d["call_minutes"], "0")}</label>
<label>Voicemail <select name="voicemail"><option value="1" {"selected" if d["voicemail"] else ""}>on</option><option value="0" {"" if d["voicemail"] else "selected"}>off</option></select></label>
<label>Texts {_lim_input("messages", d["messages"], "0")}</label>
<label>Recording <select name="recording"><option value="1" {"selected" if d["recording"] else ""}>on</option><option value="0" {"" if d["recording"] else "selected"}>off</option></select></label>
<label>IVR menus {_lim_input("ivr_menus", d["ivr_menus"], "0")}</label>
<button class="btn ghost">Save defaults</button>
</form>
<div class="scrollbox"><table class="access"><tr><th>User</th><th>Outside min/mo</th><th>Voicemail</th><th>Texts/mo</th><th>Recording</th><th>IVR menus</th><th></th></tr>
{"".join(rows) or '<tr><td colspan="7" class="muted">No users</td></tr>'}</table></div>"""


_LIM_LABEL = {"call_minutes": "Outside minutes", "messages": "Texts", "ivr_menus": "IVR menus"}


def _parse_lim(v, allow_unlimited=True, field=""):
    v = (v or "").strip().lower().replace(",", "")
    if v == "":
        return None
    if v in ("unlimited", "-1", "u"):
        if allow_unlimited:
            return ent.UNLIMITED
        raise ValueError(f"{_LIM_LABEL.get(field, field)} needs a number (e.g. 3), not \"unlimited\"")
    if v.isdigit() and int(v) <= 1000000:
        return int(v)
    raise ValueError(f"{_LIM_LABEL.get(field, field)}: \"{v}\" isn't a number or \"unlimited\"")


async def admin_access_defaults(request: Request):
    s = await _admin_post(request)
    if M._get_setting("safety_lock") == "1":
        return M._panel_locked(s, "billing")
    f = await request.form()
    try:
        vals = {"call_minutes": _parse_lim(f.get("call_minutes"), field="call_minutes") or 0,
                "messages": _parse_lim(f.get("messages"), field="messages") or 0,
                "ivr_menus": _parse_lim(f.get("ivr_menus"), False, field="ivr_menus") or 0,
                "voicemail": 1 if f.get("voicemail") == "1" else 0,
                "recording": 1 if f.get("recording") == "1" else 0}
    except ValueError as e:
        return _admin_back(err=str(e))
    with M.db() as c:
        for k, v in vals.items():
            ent.set_default_quota(c, k, v)
    _audit(s["username"], "billing.access.defaults", str(vals))
    return _admin_back(msg="Defaults saved.")


async def admin_access_user(request: Request, login_id: int):
    s = await _admin_post(request)
    if M._get_setting("safety_lock") == "1":
        return M._panel_locked(s, "billing")
    f = await request.form()
    try:
        vals = {"call_minutes": _parse_lim(f.get("call_minutes"), field="call_minutes"),
                "messages": _parse_lim(f.get("messages"), field="messages"),
                "ivr_menus": _parse_lim(f.get("ivr_menus"), False, field="ivr_menus"),
                "voicemail": None if f.get("voicemail", "") == "" else (1 if f.get("voicemail") == "1" else 0),
                "recording": None if f.get("recording", "") == "" else (1 if f.get("recording") == "1" else 0)}
    except ValueError as e:
        return _admin_back(err=str(e))
    with M.db() as c:
        if not c.execute("SELECT 1 FROM logins WHERE id=?", (login_id,)).fetchone():
            return _admin_back(err="No such user.")
        r = c.execute("SELECT 1 FROM user_entitlements WHERE login_id=? AND source LIKE 'plan:%'", (login_id,)).fetchone()
        if r:
            return _admin_back(err="That user is on a paid plan; their limits come from the plan.")
        for k, v in vals.items():
            ent.set_quota(c, login_id, k, v)
    _audit(s["username"], "billing.access.user", f"login={login_id} {vals}")
    return _admin_back(msg="User access saved.")


def _admin_back(msg="", err=""):
    q = urllib.parse.urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse("/billing" + ("?" + q if q else ""), status_code=303)


async def _admin_post(request):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    return s


async def admin_settings(request: Request):
    s = await _admin_post(request)
    if M._get_setting("safety_lock") == "1":
        return M._panel_locked(s, "billing")
    f = await request.form()
    sk = (f.get("stripe_secret") or "").strip()
    wh = (f.get("stripe_webhook_secret") or "").strip()
    url = (f.get("billing_public_url") or "").strip().rstrip("/")
    if sk:
        if not mode_of(sk):
            return _admin_back(err="That doesn't look like a Stripe secret key (sk_test_… or sk_live_…).")
        _set_kv("stripe_secret", sk)
    if wh:
        if not wh.startswith("whsec_"):
            return _admin_back(err="Webhook signing secrets start with whsec_.")
        _set_kv("stripe_webhook_secret", wh)
    if url and not url.startswith(("http://", "https://")):
        return _admin_back(err="Public address must start with https:// (or http://).")
    _set_kv("billing_public_url", url)
    _audit(s["username"], "billing.settings", f"mode={current_mode()}")
    note = ""
    if sk and any(p["stripe_mode"] and p["stripe_mode"] != current_mode() for p in _plans()):
        note = " You switched Stripe accounts/modes: press Publish on each plan."
    return _admin_back(msg="Saved." + note)


async def admin_test(request: Request):
    await _admin_post(request)
    try:
        a = _stripe().account()
        name = (a.get("settings") or {}).get("dashboard", {}).get("display_name") or a.get("email") or a.get("id")
        return _admin_back(msg=f"Connected to Stripe account {name} ({current_mode()} mode).")
    except StripeError as e:
        return _admin_back(err=f"Stripe connection failed: {e}")


async def admin_sync(request: Request):
    await _admin_post(request)
    with M.db() as c:
        ids = [r[0] for r in c.execute("SELECT login_id FROM billing_customers WHERE stripe_customer_id!=''")]
    n, errs = 0, 0
    for lid in ids:
        try:
            sync_login(lid)
            n += 1
        except StripeError:
            errs += 1
    return _admin_back(msg=f"Synced {n} customer(s)." + (f" {errs} failed." if errs else ""))


def _plan_form(s, p, err=""):
    p = p or {"id": None, "name": "", "description": "", "ivr_menus": 0, "call_minutes": 500, "voicemail": 1,
              "messages": 1000, "recording": 0, "price_cents": 500, "currency": "usd", "active": 1, "sort_order": 0}
    csrf = M._csrf_field(s)

    def num(name, label, hint):
        v = int(p.get(name) or 0)
        return (f'<label>{label}<br><input name="{name}" value="{"" if v == -1 else v}" inputmode="numeric" '
                f'style="width:120px"> <label class="inline-chk"><input type="checkbox" name="{name}_unl" value="1" '
                f'{"checked" if v == -1 else ""}> Unlimited</label></label><p class="muted">{hint}</p>')
    chk = lambda name, label: (f'<label class="switch"><input type="checkbox" name="{name}" value="1" '
                               f'{"checked" if p.get(name) else ""}> <span>{label}</span></label>')
    return f"""{'<div class="flash bad">' + esc(err) + '</div>' if err else ''}
<h2>{'Edit' if p['id'] else 'New'} plan</h2>
<form method="post" action="/billing/plans/{p['id'] or 'new'}/edit" class="panel" style="max-width:560px">{csrf}
<label>Name<br><input name="name" value="{esc(p['name'])}" required maxlength="40" placeholder="e.g. Pro"></label>
<label>Short description<br><input name="description" value="{esc(p['description'])}" maxlength="120" placeholder="e.g. Great for small teams"></label>
<h3>Included every month</h3>
<p class="muted">Calls between extensions are always free. 0 = not included.</p>
{num("call_minutes", "Outside call minutes", "Calls to and from outside numbers, rounded up per call.")}
{num("messages", "Text messages", "Texts the user can send.")}
{chk("voicemail", "Voicemail")}
{chk("recording", "Call recording (users must accept the recording-law notice)")}
<label>IVR menus<br><input name="ivr_menus" value="{int(p.get('ivr_menus') or 0)}" inputmode="numeric" style="width:120px"></label>
<h3>Price</h3>
<label>Price per month<br><input name="price" value="{p['price_cents'] / 100:.2f}" inputmode="decimal" required>
<select name="currency">{''.join(f'<option value="{c}" {"selected" if c == p["currency"] else ""}>{c.upper()}</option>' for c in ("usd", "cad", "eur", "gbp", "aud"))}</select></label>
<label>Order on the page<br><input name="sort_order" value="{p['sort_order']}" inputmode="numeric"></label>
<label class="switch"><input type="checkbox" name="active" value="1" {'checked' if p['active'] else ''}> <span>Offer to users</span></label>
<p class="muted">Changing the price creates a new Stripe price for new subscribers; existing subscribers keep their price until they switch plans.
Changing what's included applies to current subscribers right away.</p>
<button class="btn">Save &amp; publish to Stripe</button> <a class="btn ghost" href="/billing">Cancel</a>
</form>"""


def admin_plan_edit(request: Request, plan_id: str):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    p = None if plan_id == "new" else _plan(int(plan_id))
    if plan_id != "new" and not p:
        return RedirectResponse("/billing")
    return HTMLResponse(M.page("Billing", _plan_form(s, p), s["username"], s["role"], "billing"))


async def admin_plan_save(request: Request, plan_id: str):
    s = await _admin_post(request)
    if M._get_setting("safety_lock") == "1":
        return M._panel_locked(s, "billing")
    old = None if plan_id == "new" else _plan(int(plan_id))
    if plan_id != "new" and not old:
        return RedirectResponse("/billing", status_code=303)
    f = await request.form()
    name = (f.get("name") or "").strip()
    desc = (f.get("description") or "").strip()[:120]
    cur = f.get("currency") if f.get("currency") in ("usd", "cad", "eur", "gbp", "aud") else "usd"
    def lim(name):
        if f.get(name + "_unl"):
            return ent.UNLIMITED
        v = (f.get(name) or "0").replace(",", "").strip()
        return int(v) if v.isdigit() else -2
    try:
        menus = int(f.get("ivr_menus") or 0)
        cents = int(round(float((f.get("price") or "0").replace("$", "").replace(",", "")) * 100))
        sort = int(f.get("sort_order") or 0)
    except ValueError:
        menus, cents, sort = -1, -1, 0
    feats = {"call_minutes": lim("call_minutes"), "messages": lim("messages"),
             "voicemail": 1 if f.get("voicemail") else 0, "recording": 1 if f.get("recording") else 0,
             "ivr_menus": menus}
    draft = dict({"id": old["id"] if old else None, "name": name, "description": desc,
                  "price_cents": max(cents, 0), "currency": cur, "active": 1 if f.get("active") else 0,
                  "sort_order": sort}, **{k: max(v, -1) for k, v in feats.items()})
    err = ""
    if not name or len(name) > 40:
        err = "Give the plan a name."
    elif feats["call_minutes"] < -1 or feats["call_minutes"] > 1000000:
        err = "Outside call minutes must be a whole number (or tick Unlimited)."
    elif feats["messages"] < -1 or feats["messages"] > 1000000:
        err = "Text messages must be a whole number (or tick Unlimited)."
    elif not 0 <= menus <= 100:
        err = "IVR menus must be between 0 and 100."
    elif not plan_features(draft):
        err = "Include at least one feature (minutes, texts, voicemail, recording or IVR menus)."
    elif cents < 50:
        err = "Price must be at least 0.50 (Stripe's minimum)."
    if err:
        return HTMLResponse(M.page("Billing", _plan_form(s, draft, err), s["username"], s["role"], "billing"),
                            status_code=400)
    with M.db() as c:
        if old:
            c.execute("UPDATE billing_plans SET name=?, description=?, ivr_menus=?, call_minutes=?, voicemail=?,"
                      " messages=?, recording=?, price_cents=?, currency=?, active=?, sort_order=? WHERE id=?",
                      (name, desc, menus, feats["call_minutes"], feats["voicemail"], feats["messages"],
                       feats["recording"], cents, cur, draft["active"], sort, old["id"]))
            pid = old["id"]
        else:
            pid = c.execute("INSERT INTO billing_plans (name, description, ivr_menus, call_minutes, voicemail,"
                            " messages, recording, price_cents, currency, active, sort_order)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (name, desc, menus, feats["call_minutes"], feats["voicemail"], feats["messages"],
                             feats["recording"], cents, cur, draft["active"], sort)).lastrowid
        c.commit()
        # Current subscribers get the new allowances right away.
        if old and any(old.get(k) != v for k, v in feats.items()):
            newp = dict(feats, id=pid)
            for (lid,) in c.execute("SELECT login_id FROM billing_customers WHERE plan_id=? AND status IN ('active','trialing','past_due')", (pid,)).fetchall():
                _grant_plan(c, lid, newp)
    _audit(s["username"], "billing.plan.save", f"id={pid}")
    if not ready():
        return _admin_back(msg="Plan saved. Add your Stripe key, then press Publish.")
    try:
        publish_plan(_plan(pid), old)
    except StripeError as e:
        return _admin_back(err=f"Plan saved but not published to Stripe: {e}")
    return _admin_back(msg=f"Plan '{name}' saved and published to Stripe.")


async def admin_plan_publish(request: Request, plan_id: int):
    s = await _admin_post(request)
    if M._get_setting("safety_lock") == "1":
        return M._panel_locked(s, "billing")
    p = _plan(plan_id)
    if not p:
        return _admin_back()
    try:
        publish_plan(p)
    except StripeError as e:
        return _admin_back(err=f"Publish failed: {e}")
    return _admin_back(msg=f"Plan '{p['name']}' published to Stripe.")


# ---------------------------------------------------------------- install

def install(app_module):
    global M
    M = app_module
    app = M.app
    html = dict(response_class=HTMLResponse)
    app.post("/stripe/webhook")(webhook)
    app.get("/billing", **html)(admin_page)
    app.post("/billing/settings")(admin_settings)
    app.post("/billing/test")(admin_test)
    app.post("/billing/sync")(admin_sync)
    app.post("/billing/access-defaults")(admin_access_defaults)
    app.post("/billing/access/{login_id}")(admin_access_user)
    app.get("/billing/plans/{plan_id}/edit", **html)(admin_plan_edit)
    app.post("/billing/plans/{plan_id}/edit")(admin_plan_save)
    app.post("/billing/plans/{plan_id}/publish")(admin_plan_publish)
    app.get("/ucp/billing", **html)(user_page)
    app.post("/ucp/billing/subscribe/{plan_id}")(user_subscribe)
    app.post("/ucp/billing/cancel")(user_cancel)
    app.post("/ucp/billing/resume")(user_resume)
    app.post("/ucp/billing/portal")(user_portal)
