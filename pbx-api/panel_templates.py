# SPDX-License-Identifier: GPL-2.0-or-later
"""HTML templates for the pbx panel (admin dashboard + /me)."""
# Simple string templates; no Jinja dependency beyond what FastAPI has.
# NOTE: every interpolated value MUST be html-escaped by the caller
# (see esc() in app.py) — caller ID, message bodies etc. are attacker input.

BASE_CSS = """
:root{
  --bg:#f5f5f5;--fg:#222;--card:#fff;--border:#eee;--th-bg:#f8f8f8;
  --input-border:#ddd;--input-bg:#fff;--muted-fg:#666;--muted2:#888;
  --pill-bg:#eee;--hover-bg:#eef;--nav-bg:#1a1a2e;--accent:#1a1a2e;
  --accent-hover:#2a2a4e;--link:#2856c7;--nav-link:#aaccff;
}
[data-theme="dark"]{
  --bg:#111119;--fg:#e9e9f2;--card:#1b1b27;--border:#2d2d42;
  --th-bg:#222230;--input-border:#3b3b55;--input-bg:#1b1b27;
  --muted-fg:#9c9cb4;--muted2:#8a8aa2;--pill-bg:#2b2b3d;--hover-bg:#2e2e44;
  --accent:#41417a;--accent-hover:#52528f;--link:#8ab4ff;
}
body{font-family:system-ui,sans-serif;margin:0;background:var(--bg);color:var(--fg)}
.nav{background:var(--nav-bg);color:#fff;padding:12px 20px;display:flex;gap:20px;align-items:center}
.nav a{color:var(--nav-link);text-decoration:none}
.nav a:hover{color:#fff}
.nav .sp{flex:1}
.theme-toggle{background:none;border:0;font-size:18px;cursor:pointer;padding:4px 8px;border-radius:6px;line-height:1}
.theme-toggle:hover{background:rgba(255,255,255,.14)}
/* theme-aware logos: dark variant shows only in dark mode */
.brand-logo-dark,.login-logo-dark{display:none}
[data-theme="dark"] .brand-logo-dark{display:inline-block}
[data-theme="dark"] .brand-logo-light{display:none}
[data-theme="dark"] .login-logo-dark{display:inline-block}
[data-theme="dark"] .login-logo-light{display:none}
/* animated gradient header (uses brand colors) */
@keyframes navslide{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
.nav.animated{background:linear-gradient(120deg,var(--nav-bg),var(--accent),var(--nav-bg));background-size:250% 250%;animation:navslide 10s ease infinite}
/* subtle entrance */
@keyframes fadein{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.wrap>.card{animation:fadein .3s ease}
@media (prefers-reduced-motion:reduce){.nav.animated{animation:none}.wrap>.card{animation:none}}
.wrap{max-width:1100px;margin:20px auto;padding:0 20px}
.card{background:var(--card);border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--border)}
th{background:var(--th-bg)}
.btn{background:var(--accent);color:#fff;border:0;padding:8px 16px;border-radius:4px;cursor:pointer}
.btn:hover{background:var(--accent-hover)}
input,select{padding:8px;border:1px solid var(--input-border);border-radius:4px;margin:4px 0;background:var(--input-bg);color:var(--fg)}
input::placeholder{color:var(--muted2)}
.tabs{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}
.tabs a{padding:8px 16px;background:var(--card);border-radius:4px;text-decoration:none;color:var(--fg)}
.tabs a.on{background:var(--accent);color:#fff}
audio{width:100%;max-width:400px}
td audio{min-width:240px;height:36px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.stat{background:var(--th-bg);border-radius:8px;padding:14px;text-align:center}
.stat b{font-size:26px;display:block}
.stat span{color:var(--muted-fg);font-size:12px}
.muted{color:var(--muted2);font-size:12px}
.ok{color:#0a7d2c}.bad{color:#c22}.warn{color:#8a5a00}
[data-theme="dark"] .ok{color:#4ade80}[data-theme="dark"] .bad{color:#f87171}[data-theme="dark"] .warn{color:#fbbf24}
/* ---- user control panel ---- */
.ucp-head{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:16px}
.ucp-head h2{margin:0 0 2px}
.subtabs{display:flex;gap:4px;flex-wrap:wrap;margin:-4px 0 18px;border-bottom:1px solid var(--border)}
.subtabs a{padding:8px 12px;text-decoration:none;color:var(--muted-fg);border-bottom:2px solid transparent;margin-bottom:-1px}
.subtabs a.on{color:var(--accent);border-color:var(--accent);font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:22px}
.tile{background:var(--th-bg);border-radius:8px;padding:14px;color:inherit;text-decoration:none;display:block}
.tile .lbl{display:block;color:var(--muted-fg);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px}
.tile b{font-size:28px}
.tile.link:hover{background:var(--hover-bg)}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:var(--pill-bg);color:var(--fg)}
.pill.ok{background:#e3f5e8;color:#0a7d2c}.pill.bad{background:#fde8e8;color:#b31d1d}.pill.warn{background:#fff3d6;color:#8a5a00}
[data-theme="dark"] .pill.ok{background:#12351f;color:#4ade80}
[data-theme="dark"] .pill.bad{background:#3d1414;color:#f87171}
[data-theme="dark"] .pill.warn{background:#3a2c10;color:#fbbf24}
.flash{padding:10px 14px;border-radius:6px;margin-bottom:16px;background:var(--th-bg)}
.flash.ok{background:#e3f5e8;color:#0a5a22}.flash.bad{background:#fde8e8;color:#8d1616}
[data-theme="dark"] .flash.ok{background:#12351f;color:#4ade80}
[data-theme="dark"] .flash.bad{background:#3d1414;color:#f87171}
.flash.secret code{font-size:15px;background:var(--card);padding:3px 8px;border-radius:4px;margin:0 6px;word-break:break-all}
.filters{display:flex;gap:6px;margin-bottom:12px}
.filters a{padding:5px 12px;border-radius:999px;background:var(--pill-bg);color:var(--fg);text-decoration:none;font-size:14px}
.filters a.on{background:var(--accent);color:#fff}
form.inline{display:inline;margin:0}
.link-btn{background:none;border:0;color:var(--link);cursor:pointer;padding:0 6px;font:inherit}
.link-btn.bad{color:#c22}
.actions a{margin-right:6px}
.btn.ghost{background:var(--card);color:var(--accent);border:1px solid var(--accent)}
tr.unread td{font-weight:600}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--link);margin-right:6px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.panel{border:1px solid var(--border);border-radius:8px;padding:16px}
.panel h3{margin-top:0}
.panel label{display:block;margin:10px 0;font-size:14px}
.panel input[type=text],.panel input:not([type]),.panel input[type=email],.panel input[type=password]{width:100%;box-sizing:border-box}
.switch{display:flex!important;align-items:center;gap:8px;font-weight:600}
.switch input{width:18px;height:18px}
table.kv td:first-child{color:var(--muted-fg);width:110px}
.msgs{display:grid;grid-template-columns:220px 1fr;gap:16px;min-height:300px}
.convs{border-right:1px solid var(--border);padding-right:8px}
.conv{display:block;padding:8px;border-radius:6px;text-decoration:none;color:inherit}
.conv span{display:block;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.conv.on{background:var(--hover-bg)}
.thread{display:flex;flex-direction:column;gap:8px}
.bubble{max-width:70%;background:var(--pill-bg);padding:8px 12px;border-radius:12px;align-self:flex-start}
.bubble.mine{background:var(--accent);color:#fff;align-self:flex-end}
.bubble .muted{display:block;font-size:11px;margin-top:2px}
.bubble.mine .muted{color:#bbc}
table.keys td{padding:5px 6px}
table.keys td.key{font-weight:700;font-size:18px;width:34px;text-align:center}
.dest{display:flex;gap:6px;flex-wrap:wrap}
.dest select,.dest input{min-width:0;max-width:100%}
.greet{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:6px 0}
.inline-chk{display:inline-flex!important;gap:6px;align-items:center;margin:0!important}
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin-bottom:12px}
.plan{border:1px solid var(--border);border-radius:10px;padding:16px;display:flex;flex-direction:column}
.plan.on{border:2px solid var(--accent)}
.plan h3{margin:0 0 6px}
.plan .price{font-size:28px;font-weight:700;margin-bottom:6px}
.plan .price span{font-size:14px;font-weight:400;color:var(--muted-fg)}
.plan form,.plan>button{margin-top:auto}
.current-plan{margin-bottom:20px}
.current-plan h3{margin:4px 0 6px;display:flex;gap:10px;align-items:center}
.row-actions{display:flex;gap:8px;flex-wrap:wrap}
.btn[disabled]{opacity:.55;cursor:default}
.vm-greeting{margin-bottom:18px}
.greet-up{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
.bulkbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0 10px}
.btn.danger{color:#b31d1d;border-color:#b31d1d}
[data-theme="dark"] .btn.danger{color:#f87171;border-color:#f87171}
.scrollbox{max-height:420px;overflow-y:auto;border:1px solid var(--border);border-radius:6px}
.scrollbox th{position:sticky;top:0;z-index:1}
th input[type=checkbox],td input.sel,td input.si{width:16px;height:16px;margin:0}
.conv-row{display:flex;align-items:center;gap:4px}
.conv-row .conv{flex:1;min-width:0}
.bubble input.sel{margin:0 6px 0 0;vertical-align:middle}
td.msgbody{max-width:420px;overflow-wrap:anywhere}
.flash.warn-note{background:#fff6dd;color:#6b4a00}
[data-theme="dark"] .flash.warn-note{background:#3a2f14;color:#fbbf24}
.legal{background:var(--th-bg);border-left:3px solid #8a5a00;padding:10px 12px;font-size:13px;margin:8px 0;line-height:1.45}
label.ack{display:flex!important;gap:8px;align-items:flex-start;font-size:13px;font-weight:600}
label.ack input{margin-top:2px;width:16px;height:16px;flex:none}
ul.feat{padding-left:18px;margin:4px 0 10px}ul.feat li{margin:2px 0}
table.usage caption{text-align:left;padding:4px 0}
.meter{display:inline-block;width:90px;height:6px;background:var(--pill-bg);border-radius:3px;margin-left:8px;vertical-align:middle;overflow:hidden}
.meter span{display:block;height:100%;background:var(--accent)}
.warn{color:#8a5a00}
.access-defaults{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:8px 0 12px}
.access-defaults label{margin:0!important}
table.access input{width:90px}
ul.checklist{padding-left:18px;font-size:13px;line-height:1.5}
ul.checklist code{display:block;margin:4px 0 6px;word-break:break-all}
.setup-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.setup-grid h4{margin:0 0 6px}
.safety-bar{border:2px solid var(--border);border-radius:8px;padding:12px;background:var(--th-bg)}
.safety-bar.engaged{border-color:#c22;background:#fff0f0}
[data-theme="dark"] .safety-bar.engaged{background:#3d1414;border-color:#f87171}
pre.token{background:var(--pill-bg);padding:12px;word-break:break-all;border-radius:6px}
@media (max-width:640px){.msgs{grid-template-columns:1fr}.convs{border:0}
  table{display:block;overflow-x:auto}.wrap{padding:0 10px}.card{padding:14px}}
"""

import contextvars
from zoneinfo import ZoneInfo
from datetime import datetime as _dt

# Set per request by app._remote_guard: True when admin pages are blocked for
# this visitor (internet, "office network only"). Admins then get the same
# menu as users - the admin tabs would only lead to "Office network only".
HIDE_ADMIN = contextvars.ContextVar("hide_admin", default=False)

# Branding dict for this request (set by app middleware from kv_settings).
# Keys: site_name, header_mode, logo, header_color, accent_color,
#       login_mode, login_title, login_subtitle, favicon,
#       footer_show, footer_text. Missing keys fall back to BRAND_DEFAULTS.
BRANDING = contextvars.ContextVar("branding", default=None)

BRAND_DEFAULTS = {
    "site_name": "PBX Panel",
    "header_mode": "text",      # text | logo | both
    "logo": "",                 # data URI or https URL (light theme / fallback)
    "logo_dark": "",            # shown in dark theme; falls back to logo
    "header_color": "#1a1a2e",
    "accent_color": "#1a1a2e",
    "header_animated": "0",     # 1 = animated gradient header
    "login_mode": "text",       # text | logo | both
    "login_title": "",
    "login_subtitle": "",
    "login_logo": "",           # data URI or https URL; falls back to logo
    "login_logo_dark": "",      # shown in dark theme; falls back to login_logo
    "favicon": "",
    "footer_show": "0",
    "footer_text": "",
    "theme_default": "dark",    # dark | light — used when visitor has no saved choice
    "timezone": "America/Los_Angeles",
}

import re as _re
def _brand_color(v, fallback):
    v = (v or "").strip()
    return v if _re.fullmatch(r"#[0-9a-fA-F]{6}", v) else fallback


def get_brand():
    b = dict(BRAND_DEFAULTS)
    cur = BRANDING.get()
    if cur:
        for k in b:
            if k in cur and cur[k] is not None:
                b[k] = cur[k]
    b["header_color"] = _brand_color(b["header_color"], BRAND_DEFAULTS["header_color"])
    b["accent_color"] = _brand_color(b["accent_color"], BRAND_DEFAULTS["accent_color"])
    if b["header_mode"] not in ("text", "logo", "both"):
        b["header_mode"] = "text"
    if b["login_mode"] not in ("text", "logo", "both"):
        b["login_mode"] = "text"
    if b["theme_default"] not in ("dark", "light"):
        b["theme_default"] = "dark"
    if b["header_animated"] != "1":
        b["header_animated"] = "0"
    try:
        ZoneInfo(b["timezone"] or "America/Los_Angeles")
    except Exception:
        b["timezone"] = "America/Los_Angeles"
    return b


def fmt_ts(ts, from_utc=True):
    """'YYYY-MM-DD HH:MM:SS' -> 'YYYY-MM-DD h:MM AM/PM' in the brand timezone.

    from_utc=True: the stored value is UTC (SQLite datetime('now') defaults,
    voip.ms). False: it's already server-local wall time (CDR); formatted
    as-is on the assumption the server sits in the brand timezone.
    """
    import html as _html
    s = (ts or "").strip()[:19]
    try:
        dt = _dt.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return _html.escape(str(ts or ""))
    brand = get_brand()
    try:
        tz = ZoneInfo(brand.get("timezone") or "America/Los_Angeles")
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")
    try:
        if from_utc:
            dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        else:
            dt = dt.replace(tzinfo=tz)
    except Exception:
        pass
    h = dt.strftime("%I").lstrip("0") or "12"
    return _html.escape(f"{dt:%Y-%m-%d} {h}:{dt:%M} {dt:%p}")


def theme_logo_imgs(light_src, dark_src, css_class, style):
    """Render one <img>, or a light/dark pair that CSS swaps per data-theme.

    dark_src falls back to light_src; when they match only one tag is emitted.
    """
    import html as _html
    light_src = (light_src or "").strip()
    dark_src = (dark_src or "").strip() or light_src
    if not light_src:
        return ""
    base = (f'<img src="{_html.escape(light_src, quote=True)}" alt="" '
            f'class="{css_class}" style="{style}">')
    if dark_src != light_src:
        return (f'<img src="{_html.escape(light_src, quote=True)}" alt="" '
                f'class="{css_class} {css_class}-light" style="{style}">'
                f'<img src="{_html.escape(dark_src, quote=True)}" alt="" '
                f'class="{css_class} {css_class}-dark" style="{style}">')
    return base


def page(title, body, username="", role="", tab=""):
    tabs = ""
    hide_admin = HIDE_ADMIN.get()
    if role == "admin" and not hide_admin:
        tabs = """<div class="tabs">
        <a href="/" class="{d}">Dashboard</a>
        <a href="/logins" class="{l}">Logins</a>
        <a href="/invites" class="{inv}">Invites</a>
        <a href="/trunks" class="{t}">Trunks</a>
        <a href="/routes" class="{r}">Routes</a>
        <a href="/ring-groups" class="{grp}">Ring groups</a>
        <a href="/cdr" class="{c}">CDR</a>
        <a href="/voicemail" class="{v}">Voicemail</a>
        <a href="/recordings" class="{rec}">Recordings</a>
        <a href="/messages" class="{msgs}">Messages</a>
        <a href="/sms-routes" class="{sms}">SMS</a>
        <a href="/features" class="{feat}">Features</a>
        <a href="/ivr" class="{ivr}">IVR</a>
        <a href="/billing" class="{bill}">Billing</a>
        <a href="/email" class="{email}">Email</a>
        <a href="/network" class="{net}">Network</a>
        <a href="/e911" class="{e911}">E911</a>
        <a href="/reports" class="{rep}">Reports</a>
        <a href="/api-keys" class="{k}">API Keys</a>
        <a href="/branding" class="{brand}">Branding</a>
        <a href="/ucp" class="{me}">My Phone</a>
        </div>""".format(
            me="on" if tab.startswith("ucp") else "",
            ivr="on" if tab == "ivr" else "",
            msgs="on" if tab == "msgs" else "",
            sms="on" if tab == "sms" else "",
            feat="on" if tab == "features" else "",
            bill="on" if tab == "billing" else "",
            email="on" if tab == "email" else "",
            net="on" if tab == "network" else "",
            e911="on" if tab == "e911" else "",
            grp="on" if tab == "groups" else "",
            rep="on" if tab == "reports" else "",
            d="on" if tab=="dash" else "", l="on" if tab=="logins" else "",
            inv="on" if tab=="invites" else "",
            t="on" if tab=="trunks" else "", r="on" if tab=="routes" else "",
            c="on" if tab=="cdr" else "", v="on" if tab=="vm" else "",
            rec="on" if tab=="rec" else "", k="on" if tab=="keys" else "",
            brand="on" if tab=="branding" else "")
    elif role:
        ucp_tabs = [("ucp", "/ucp", "Overview"), ("ucp-calls", "/ucp/calls", "Calls"),
                    ("ucp-vm", "/ucp/voicemail", "Voicemail"),
                    ("ucp-rec", "/ucp/recordings", "Recordings"),
                    ("ucp-msg", "/ucp/messages", "Messages"),
                    ("ucp-ivr", "/ucp/ivr", "IVR"),
                    ("ucp-bill", "/ucp/billing", "Billing"),
                    ("ucp-set", "/ucp/settings", "Settings")]
        tabs = '<div class="tabs">' + "".join(
            f'<a href="{h}" class="{"on" if k == tab else ""}">{label}</a>'
            for k, h, label in ucp_tabs) + "</div>"
    import html as _html
    shown_role = role + (", office-only admin" if role == "admin" and hide_admin else "")
    nav_user = f'<span>{_html.escape(username, quote=True)} ({_html.escape(shown_role, quote=True)})</span> <a href="/logout">Logout</a>' if username else ""
    brand = get_brand()
    # Header brand: text, logo, or both (logo auto-swaps per theme when set)
    _logo_tag = theme_logo_imgs(brand["logo"], brand["logo_dark"], "brand-logo",
                                "height:28px;vertical-align:middle;border-radius:4px")
    _name = _html.escape(brand["site_name"] or "PBX Panel", quote=True)
    if brand["header_mode"] == "logo" and _logo_tag:
        brand_html = _logo_tag
    elif brand["header_mode"] == "both" and _logo_tag:
        brand_html = _logo_tag + f' <strong style="margin-left:8px">{_name}</strong>'
    else:
        brand_html = f"<strong>{_name}</strong>"
    _nav_cls = "nav animated" if brand["header_animated"] == "1" else "nav"
    # Custom colors (validated hex in get_brand). Emitted as CSS variables
    # AFTER the theme blocks so branding wins in both light and dark mode.
    _brand_vars = []
    if brand["header_color"] != BRAND_DEFAULTS["header_color"]:
        _brand_vars.append(f"--nav-bg:{brand['header_color']}")
    if brand["accent_color"] != BRAND_DEFAULTS["accent_color"]:
        _brand_vars.append(f"--accent:{brand['accent_color']}")
    _color_css = (":root{" + ";".join(_brand_vars) + "}") if _brand_vars else ""
    # Favicon
    _fav = brand["favicon"].strip()
    _fav_tag = (f'<link rel="icon" href="{_html.escape(_fav, quote=True)}">') if _fav else ""
    # Footer (opt-in)
    _footer = ""
    if brand["footer_show"] == "1" and brand["footer_text"].strip():
        _footer = (f'<div class="wrap"><p class="muted" style="text-align:center;padding:10px 0">'
                   f'{_html.escape(brand["footer_text"].strip())}</p></div>')
    # Theme: visitor's saved choice wins, else the branded default (dark).
    # Runs synchronously in <head> so the right theme paints first try.
    _theme_js = ("""<script>(function(){var d="%s";try{var t=localStorage.getItem("pbx-theme");"""
                 """t=(t==="light"||t==="dark")?t:d;}catch(e){t=d;}"""
                 """document.documentElement.setAttribute("data-theme",t);})();"""
                 """function toggleTheme(){var h=document.documentElement;"""
                 """var t=h.getAttribute("data-theme")==="dark"?"light":"dark";"""
                 """h.setAttribute("data-theme",t);"""
                 """try{localStorage.setItem("pbx-theme",t);}catch(e){}"""
                 """var b=document.getElementById("theme-toggle");"""
                 """if(b)b.textContent=t==="dark"?"🌙":"☀️";}</script>"""
                ) % brand["theme_default"]
    _theme_btn = ("""<button id="theme-toggle" class="theme-toggle" onclick="toggleTheme()" """
                  """title="Toggle dark / light mode">🌙</button>"""
                  """<script>try{document.getElementById("theme-toggle").textContent="""
                  """document.documentElement.getAttribute("data-theme")==="dark"?"🌙":"☀️";}catch(e){}</script>""")
    return f"""<!DOCTYPE html><html><head><title>{title} - {_name}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
{_theme_js}
{_fav_tag}
<style>{BASE_CSS}{_color_css}</style></head><body>
<div class="{_nav_cls}">{brand_html}<span class="sp"></span>{_theme_btn}{nav_user}</div>
<div class="wrap">{tabs}<div class="card">{body}</div></div>{_footer}</body></html>"""

def login_html():
    """Login form with branding: logo and/or custom title per admin settings."""
    import html as _html
    brand = get_brand()
    _login_src = (brand["login_logo"] or brand["logo"]).strip()
    _login_dark = (brand["login_logo_dark"] or brand["logo_dark"] or _login_src).strip()
    _logo_tag = theme_logo_imgs(_login_src, _login_dark, "login-logo",
                                "max-height:64px;max-width:280px;margin-bottom:8px")
    if _logo_tag:
        _logo_tag += "<br>"
    _title = (brand["login_title"] or "").strip() or brand["site_name"] or "PBX Panel"
    _sub = (brand["login_subtitle"] or "").strip()
    mode = brand["login_mode"]
    if mode == "logo" and _logo_tag:
        head = _logo_tag
    elif mode == "both" and _logo_tag:
        head = _logo_tag + f"<h2>{_html.escape(_title)}</h2>"
    else:
        head = f"<h2>{_html.escape(_title)}</h2>"
    sub = f'<p class="muted">{_html.escape(_sub)}</p>' if _sub else ""
    return f"""{head}{sub}
<form method="post" action="/login">
<input name="username" placeholder="Username" required><br>
<input name="password" type="password" placeholder="Password" required><br>
<button class="btn" type="submit">Sign in</button>
</form>
"""

# Backwards-compat alias (use login_html() for branded output).
LOGIN_HTML = """
<h2>Login</h2>
<form method="post" action="/login">
<input name="username" placeholder="Username" required><br>
<input name="password" type="password" placeholder="Password" required><br>
<button class="btn" type="submit">Sign in</button>
</form>
"""
