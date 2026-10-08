# SPDX-License-Identifier: GPL-2.0-or-later
"""HTML templates for the pbx panel (admin dashboard + /me)."""
# Simple string templates; no Jinja dependency beyond what FastAPI has.
# NOTE: every interpolated value MUST be html-escaped by the caller
# (see esc() in app.py) — caller ID, message bodies etc. are attacker input.

BASE_CSS = """
body{font-family:system-ui,sans-serif;margin:0;background:#f5f5f5;color:#222}
.nav{background:#1a1a2e;color:#fff;padding:12px 20px;display:flex;gap:20px;align-items:center}
.nav a{color:#aaccff;text-decoration:none}
.nav a:hover{color:#fff}
.nav .sp{flex:1}
.wrap{max-width:1100px;margin:20px auto;padding:0 20px}
.card{background:#fff;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px;border-bottom:1px solid #eee}
th{background:#f8f8f8}
.btn{background:#1a1a2e;color:#fff;border:0;padding:8px 16px;border-radius:4px;cursor:pointer}
.btn:hover{background:#2a2a4e}
input,select{padding:8px;border:1px solid #ddd;border-radius:4px;margin:4px 0}
.tabs{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}
.tabs a{padding:8px 16px;background:#fff;border-radius:4px;text-decoration:none;color:#333}
.tabs a.on{background:#1a1a2e;color:#fff}
audio{width:100%;max-width:400px}
td audio{min-width:240px;height:36px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.stat{background:#f8f8f8;border-radius:8px;padding:14px;text-align:center}
.stat b{font-size:26px;display:block}
.stat span{color:#666;font-size:12px}
.muted{color:#888;font-size:12px}
.ok{color:#0a7d2c}.bad{color:#c22}.warn{color:#8a5a00}
/* ---- user control panel ---- */
.ucp-head{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:16px}
.ucp-head h2{margin:0 0 2px}
.subtabs{display:flex;gap:4px;flex-wrap:wrap;margin:-4px 0 18px;border-bottom:1px solid #eee}
.subtabs a{padding:8px 12px;text-decoration:none;color:#555;border-bottom:2px solid transparent;margin-bottom:-1px}
.subtabs a.on{color:#1a1a2e;border-color:#1a1a2e;font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:22px}
.tile{background:#f8f8f8;border-radius:8px;padding:14px;color:inherit;text-decoration:none;display:block}
.tile .lbl{display:block;color:#666;font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px}
.tile b{font-size:28px}
.tile.link:hover{background:#eef}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:#eee;color:#444}
.pill.ok{background:#e3f5e8;color:#0a7d2c}.pill.bad{background:#fde8e8;color:#b31d1d}.pill.warn{background:#fff3d6;color:#8a5a00}
.flash{padding:10px 14px;border-radius:6px;margin-bottom:16px;background:#f3f3f3}
.flash.ok{background:#e3f5e8;color:#0a5a22}.flash.bad{background:#fde8e8;color:#8d1616}
.flash.secret code{font-size:15px;background:#fff;padding:3px 8px;border-radius:4px;margin:0 6px;word-break:break-all}
.filters{display:flex;gap:6px;margin-bottom:12px}
.filters a{padding:5px 12px;border-radius:999px;background:#f1f1f1;color:#333;text-decoration:none;font-size:14px}
.filters a.on{background:#1a1a2e;color:#fff}
form.inline{display:inline;margin:0}
.link-btn{background:none;border:0;color:#2856c7;cursor:pointer;padding:0 6px;font:inherit}
.link-btn.bad{color:#c22}
.actions a{margin-right:6px}
.btn.ghost{background:#fff;color:#1a1a2e;border:1px solid #1a1a2e}
tr.unread td{font-weight:600}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#2856c7;margin-right:6px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.panel{border:1px solid #eee;border-radius:8px;padding:16px}
.panel h3{margin-top:0}
.panel label{display:block;margin:10px 0;font-size:14px}
.panel input[type=text],.panel input:not([type]),.panel input[type=email],.panel input[type=password]{width:100%;box-sizing:border-box}
.switch{display:flex!important;align-items:center;gap:8px;font-weight:600}
.switch input{width:18px;height:18px}
table.kv td:first-child{color:#666;width:110px}
.msgs{display:grid;grid-template-columns:220px 1fr;gap:16px;min-height:300px}
.convs{border-right:1px solid #eee;padding-right:8px}
.conv{display:block;padding:8px;border-radius:6px;text-decoration:none;color:inherit}
.conv span{display:block;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.conv.on{background:#eef}
.thread{display:flex;flex-direction:column;gap:8px}
.bubble{max-width:70%;background:#f1f1f1;padding:8px 12px;border-radius:12px;align-self:flex-start}
.bubble.mine{background:#1a1a2e;color:#fff;align-self:flex-end}
.bubble .muted{display:block;font-size:11px;margin-top:2px}
.bubble.mine .muted{color:#bbc}
table.keys td{padding:5px 6px}
table.keys td.key{font-weight:700;font-size:18px;width:34px;text-align:center}
.dest{display:flex;gap:6px;flex-wrap:wrap}
.dest select,.dest input{min-width:0;max-width:100%}
.greet{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:6px 0}
.inline-chk{display:inline-flex!important;gap:6px;align-items:center;margin:0!important}
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin-bottom:12px}
.plan{border:1px solid #e3e3e3;border-radius:10px;padding:16px;display:flex;flex-direction:column}
.plan.on{border:2px solid #1a1a2e}
.plan h3{margin:0 0 6px}
.plan .price{font-size:28px;font-weight:700;margin-bottom:6px}
.plan .price span{font-size:14px;font-weight:400;color:#666}
.plan form,.plan>button{margin-top:auto}
.current-plan{margin-bottom:20px}
.current-plan h3{margin:4px 0 6px;display:flex;gap:10px;align-items:center}
.row-actions{display:flex;gap:8px;flex-wrap:wrap}
.btn[disabled]{opacity:.55;cursor:default}
.vm-greeting{margin-bottom:18px}
.greet-up{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
.bulkbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0 10px}
.btn.danger{color:#b31d1d;border-color:#b31d1d}
.scrollbox{max-height:420px;overflow-y:auto;border:1px solid #eee;border-radius:6px}
.scrollbox th{position:sticky;top:0;z-index:1}
th input[type=checkbox],td input.sel,td input.si{width:16px;height:16px;margin:0}
.conv-row{display:flex;align-items:center;gap:4px}
.conv-row .conv{flex:1;min-width:0}
.bubble input.sel{margin:0 6px 0 0;vertical-align:middle}
td.msgbody{max-width:420px;overflow-wrap:anywhere}
.flash.warn-note{background:#fff6dd;color:#6b4a00}
.legal{background:#f7f7fb;border-left:3px solid #8a5a00;padding:10px 12px;font-size:13px;margin:8px 0;line-height:1.45}
label.ack{display:flex!important;gap:8px;align-items:flex-start;font-size:13px;font-weight:600}
label.ack input{margin-top:2px;width:16px;height:16px;flex:none}
ul.feat{padding-left:18px;margin:4px 0 10px}ul.feat li{margin:2px 0}
table.usage caption{text-align:left;padding:4px 0}
.meter{display:inline-block;width:90px;height:6px;background:#eee;border-radius:3px;margin-left:8px;vertical-align:middle;overflow:hidden}
.meter span{display:block;height:100%;background:#1a1a2e}
.warn{color:#8a5a00}
.access-defaults{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:8px 0 12px}
.access-defaults label{margin:0!important}
table.access input{width:90px}
ul.checklist{padding-left:18px;font-size:13px;line-height:1.5}
ul.checklist code{display:block;margin:4px 0 6px;word-break:break-all}
.setup-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.setup-grid h4{margin:0 0 6px}
@media (max-width:640px){.msgs{grid-template-columns:1fr}.convs{border:0}
  table{display:block;overflow-x:auto}.wrap{padding:0 10px}.card{padding:14px}}
"""

import contextvars

# Set per request by app._remote_guard: True when admin pages are blocked for
# this visitor (internet, "office network only"). Admins then get the same
# menu as users - the admin tabs would only lead to "Office network only".
HIDE_ADMIN = contextvars.ContextVar("hide_admin", default=False)


def page(title, body, username="", role="", tab=""):
    tabs = ""
    hide_admin = HIDE_ADMIN.get()
    if role == "admin" and not hide_admin:
        tabs = """<div class="tabs">
        <a href="/" class="{d}">Dashboard</a>
        <a href="/logins" class="{l}">Logins</a>
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
            t="on" if tab=="trunks" else "", r="on" if tab=="routes" else "",
            c="on" if tab=="cdr" else "", v="on" if tab=="vm" else "",
            rec="on" if tab=="rec" else "", k="on" if tab=="keys" else "")
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
    return f"""<!DOCTYPE html><html><head><title>{title} - PBX</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{BASE_CSS}</style></head><body>
<div class="nav"><strong>PBX Panel</strong><span class="sp"></span>{nav_user}</div>
<div class="wrap">{tabs}<div class="card">{body}</div></div></body></html>"""

LOGIN_HTML = """
<h2>Login</h2>
<form method="post" action="/login">
<input name="username" placeholder="Username" required><br>
<input name="password" type="password" placeholder="Password" required><br>
<button class="btn" type="submit">Sign in</button>
</form>
"""
