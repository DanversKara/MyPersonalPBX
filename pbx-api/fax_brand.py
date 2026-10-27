# SPDX-License-Identifier: GPL-2.0-or-later
"""Fax branding: what the cover page and the header line on each page say.

Two layers:
  * the PBX's branding (admin, Faxes page): used for everyone by default;
  * a user's own branding (My Phone -> Faxes), used for their faxes when the
    admin allows it ("Let users use their own branding") and they turn it on.

Branding = logo image and/or logo text (company name), company details
(address, phone, email, website), a footer notice (e.g. confidentiality) and
the header text printed at the top of every page. Logos are stored already
turned into fax black & white, so what the preview shows is what is sent.
Stored in fax_branding (login_id 0 = the PBX's); logos in BRAND_DIR.
"""
import io
import os
import time
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

M = None
BRAND_DIR = os.environ.get("PBX_FAX_BRAND_DIR", "/var/lib/pbx/fax-brand")
PBX = 0                       # fax_branding.login_id of the PBX-wide branding
MAX_LOGO = 5 * 1024 * 1024
LOGO_DPI = 200                # fax resolution, near enough (204 x 196)
LOGO_BOX = (700, 220)         # px at LOGO_DPI: 3.5 x 1.1 inches
FIELDS = {                    # name: (label, max length)
    "company": ("Logo text / company name", 60),
    "address": ("Address", 200),
    "phone": ("Phone", 30),
    "email": ("Email", 80),
    "website": ("Website", 80),
    "footer": ("Footer notice", 600),
    "header": ("Header line on every page", 40),
}
EMPTY = dict({k: "" for k in FIELDS}, use_own=0, logo="")


def esc(x):
    import html
    return html.escape(str(x if x is not None else ""))


def _clean(name, v):
    v = str(v or "").replace("\r", "")
    if name not in ("address", "footer"):
        v = " ".join(v.split())
    else:
        v = "\n".join(" ".join(line.split()) for line in v.split("\n")).strip()
        if name == "address":
            v = "\n".join(v.split("\n")[:4])
    return v[:FIELDS[name][1]]


# ------------------------------------------------------------------ storage

def load(c, login_id):
    try:
        r = c.execute("SELECT * FROM fax_branding WHERE login_id=?", (login_id,)).fetchone()
    except Exception:
        return dict(EMPTY)
    if not r:
        return dict(EMPTY)
    b = dict(EMPTY)
    b.update({k: r[k] or "" for k in FIELDS})
    b["use_own"] = int(r["use_own"] or 0)
    b["logo"] = r["logo"] or ""
    return b


def save(c, login_id, vals, use_own=None):
    cur = load(c, login_id)
    b = {k: _clean(k, vals.get(k, cur[k])) for k in FIELDS}
    own = cur["use_own"] if use_own is None else (1 if use_own else 0)
    c.execute("INSERT INTO fax_branding (login_id, use_own, company, address, phone, email, website, footer,"
              " header, logo, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))"
              " ON CONFLICT(login_id) DO UPDATE SET use_own=excluded.use_own, company=excluded.company,"
              " address=excluded.address, phone=excluded.phone, email=excluded.email, website=excluded.website,"
              " footer=excluded.footer, header=excluded.header, updated_at=excluded.updated_at",
              (login_id, own, b["company"], b["address"], b["phone"], b["email"], b["website"], b["footer"],
               b["header"], cur["logo"]))
    c.commit()


def users_allowed(c):
    r = c.execute("SELECT value FROM kv_settings WHERE key='fax_brand_users'").fetchone()
    return (r[0] if r else "1") == "1"


def for_login(c, me):
    """The branding a user's faxes use, plus where it came from ('own' | 'pbx')."""
    pbx = load(c, PBX)
    if me is not None and users_allowed(c):
        own = load(c, me["id"])
        if own["use_own"]:
            return dict(own, source="own")
    return dict(pbx, source="pbx")


def header_text(brand, me):
    """Top line of every page (Asterisk prints the date, this, the sending
    number and the page number)."""
    name = ""
    if me is not None:
        name = (me["display_name"] or me["username"] or "") if hasattr(me, "keys") else ""
    return (brand.get("header") or brand.get("company") or name or "Fax")[:40]


# ------------------------------------------------------------------ logo

def logo_path(b):
    p = b.get("logo") or ""
    if not p:
        return ""
    full = os.path.realpath(os.path.join(BRAND_DIR, os.path.basename(p)))
    return full if full.startswith(os.path.realpath(BRAND_DIR) + os.sep) and os.path.isfile(full) else ""


def store_logo(c, login_id, data):
    """Fax-ready black & white PNG from an uploaded picture. Raises ValueError."""
    if not data:
        raise ValueError("Choose a picture for the logo.")
    if len(data) > MAX_LOGO:
        raise ValueError("That logo is over 5 MB.")
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise ValueError("Logos need the Pillow package on the PBX (run pbx-update).")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ValueError("That logo couldn't be read. Use a PNG or JPG.")
    if img.width * img.height > 40_000_000:
        raise ValueError("That logo is too large. Use a smaller picture.")
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img)
        img = bg
    g = img.convert("L")
    box = ImageOps.invert(g).point(lambda v: 255 if v > 24 else 0).getbbox()   # trim blank margins
    if box:
        g = g.crop(box)
    w, h = g.size
    scale = min(LOGO_BOX[0] / w, LOGO_BOX[1] / h)
    g = g.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    # Fax is black or white: stretch the contrast so coloured logos come out
    # mostly solid (light tints stay as fine dots, near-white drops out).
    g = g.point(lambda v: 0 if v < 70 else 255 if v > 215 else int((v - 70) * 255 / 145))
    bw = g.convert("1")          # Floyd-Steinberg dithering for what's left in between
    os.makedirs(BRAND_DIR, exist_ok=True)
    name = f"logo-{login_id}-{int(time.time())}.png"
    bw.save(os.path.join(BRAND_DIR, name), "PNG")
    old = load(c, login_id)
    if not c.execute("SELECT 1 FROM fax_branding WHERE login_id=?", (login_id,)).fetchone():
        save(c, login_id, {})
    c.execute("UPDATE fax_branding SET logo=?, updated_at=datetime('now') WHERE login_id=?", (name, login_id))
    c.commit()
    _rm(old)


def remove_logo(c, login_id):
    old = load(c, login_id)
    c.execute("UPDATE fax_branding SET logo='' WHERE login_id=?", (login_id,))
    c.commit()
    _rm(old)


def _rm(b):
    p = logo_path(b)
    if p:
        try:
            os.remove(p)
        except OSError:
            pass


# ------------------------------------------------------------------ cover page

def _ps(s):
    s = str(s or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in s)


def pretty(n):
    d = "".join(ch for ch in str(n or "") if ch.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else str(n or "")


def _wrap(text, width):
    out = []
    for para in str(text or "").split("\n"):
        line = ""
        for word in para.split():
            while len(word) > width:
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:width])
                word = word[width:]
            if len(line) + len(word) + (1 if line else 0) > width:
                out.append(line)
                line = word
            else:
                line = (line + " " + word) if line else word
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return out


def _logo_ps(path, x, top):
    """PostScript drawing the 1-bit logo with its top-left at (x, top).
    Returns (code, height in points)."""
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("1")
        w, h = im.size
        raw = im.tobytes()            # rows padded to whole bytes, 1 = white: same as PostScript
    wpt, hpt = w * 72 / LOGO_DPI, h * 72 / LOGO_DPI
    hexdata = raw.hex()
    lines = "\n".join(hexdata[i:i + 120] for i in range(0, len(hexdata), 120))
    code = (f"gsave {x:.2f} {top - hpt:.2f} translate {wpt:.2f} {hpt:.2f} scale\n"
            f"{w} {h} 1 [{w} 0 0 -{h} 0 {h}] currentfile /ASCIIHexDecode filter image\n{lines}>\ngrestore")
    return code, hpt


def cover_ps(path, c, brand=None):
    """The cover page (PostScript, US letter) from the details in dict c and
    the branding (dict from for_login, or None for a plain one)."""
    b = brand or EMPTY
    L, R, top = 54, 558, 738
    out = ["%!PS", "/H {/Helvetica findfont exch scalefont setfont} def",
           "/HB {/Helvetica-Bold findfont exch scalefont setfont} def",
           "/HI {/Helvetica-Oblique findfont exch scalefont setfont} def",
           "/RS {dup stringwidth pop 3 -1 roll exch sub 2 index moveto show pop} def"]   # y x (s) RS: right-aligned
    y = top
    logo = logo_path(b)
    if logo:
        try:
            code, hpt = _logo_ps(logo, L, top)
            out.append(code)
            y = top - hpt - 6
        except Exception:
            logo = ""
    if b.get("company"):
        size = 13 if logo else 24
        y -= size
        out.append(f"{size} HB {L} {y:.1f} moveto ({_ps(b['company'])}) show")
        y -= 5
    info = [ln for ln in (b.get("address") or "").split("\n") if ln.strip()]
    contact = "   ".join(x for x in (
        ("Tel " + b["phone"]) if b.get("phone") else "", b.get("email") or "", b.get("website") or "") if x)
    if contact:
        info.append(contact)
    for ln in info[:6]:
        y -= 12
        out.append(f"9.5 H {L} {y:.1f} moveto ({_ps(ln)[:95]}) show")
    out.append(f"{top - 34} {R} (FAX) 40 HB RS")
    y = min(y, top - 50) - 14
    out.append(f"2 setlinewidth {L} {y:.1f} moveto {R} {y:.1f} lineto stroke")
    y -= 30
    rows = [("To", c.get("to_name")), ("Fax", pretty(c.get("to_number"))), ("From", c.get("from_name")),
            ("Company", b.get("company") if b.get("company") and b.get("company") != c.get("from_name") else ""),
            ("Phone", pretty(c.get("from_number"))), ("Date", time.strftime("%B %d, %Y  %I:%M %p")),
            ("Pages", f"{c.get('pages', '?')} including this cover"), ("Subject", c.get("subject"))]
    for k, v in rows:
        if not v:
            continue
        out.append(f"13 HB {L} {y:.1f} moveto ({_ps(k)}:) show 13 H 140 {y:.1f} moveto ({_ps(v)[:62]}) show")
        y -= 24
    foot = _wrap(b.get("footer"), 108)[:8]
    foot_top = 50 + 11 * len(foot) + (14 if foot else 0)
    if c.get("note"):
        y -= 8
        out.append(f"0.75 setlinewidth {L} {y + 14:.1f} moveto {R} {y + 14:.1f} lineto stroke")
        y -= 8
        for line in _wrap(c["note"], 82):
            if y < foot_top + 10:
                break
            out.append(f"12 H {L} {y:.1f} moveto ({_ps(line)}) show")
            y -= 17
    if foot:
        fy = 50 + 11 * (len(foot) - 1)
        out.append(f"0.5 setlinewidth {L} {fy + 16:.1f} moveto {R} {fy + 16:.1f} lineto stroke")
        for line in foot:
            out.append(f"8.5 HI {L} {fy:.1f} moveto ({_ps(line)}) show")
            fy -= 11
    out.append("showpage")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def preview_pdf(brand, c):
    """A PDF of the cover page, rendered the way it's faxed (black & white)."""
    import shutil
    import subprocess
    import tempfile
    d = tempfile.mkdtemp(prefix="cover-")
    try:
        ps = os.path.join(d, "cover.ps")
        cover_ps(ps, c, brand)
        tif, pdf = os.path.join(d, "cover.tif"), os.path.join(d, "cover.pdf")
        M.fax_send._gs(["-sDEVICE=tiffg3", "-r204x196", "-sPAPERSIZE=letter", "-dFIXEDMEDIA",
                        f"-sOutputFile={tif}", ps], timeout=60)
        subprocess.run(["tiff2pdf", "-o", pdf, tif], check=True, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(pdf, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _sample(request, me, brand):
    q = request.query_params
    return {"to_name": (q.get("to_name") or "Sample Recipient")[:60],
            "to_number": (q.get("to_number") or "13105550100")[:20],
            "from_name": ((me["display_name"] or me["username"]) if me is not None else (brand.get("company") or "Your name"))[:60],
            "from_number": (q.get("from_did") or (M.fax_send.allowed_numbers(me)[:1] or ["5555550123"])[0] if me is not None else "5555550123"),
            "subject": (q.get("subject") or "Sample subject")[:80],
            "note": (q.get("note") if q.get("note") is not None else "This is how your cover page looks. Your message appears here.")[:2000],
            "pages": 3}


def _pdf_response(data):
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="cover-preview.pdf"', "Cache-Control": "no-store"})


# ------------------------------------------------------------------ forms

def form_html(b, action, csrf, logo_url, preview_url, user=False, allowed=True, pbx_name=""):
    """The branding form (admin or user)."""
    def inp(k, ph=""):
        return (f'<label>{FIELDS[k][0]}<br><input name="{k}" value="{esc(b[k])}" maxlength="{FIELDS[k][1]}" '
                f'placeholder="{esc(ph)}"></label>')
    logo = (f'<div class="brand-logo"><img src="{logo_url}?v={esc(b["logo"])}" alt="Current logo" '
            f'style="max-width:280px;max-height:90px;background:#fff;padding:6px;border-radius:6px;display:block">'
            f'<label class="inline-chk"><input type="checkbox" name="remove_logo" value="1"> Remove logo</label></div>'
            if b.get("logo") else '<p class="muted">No logo yet.</p>')
    own = ""
    if user:
        whose = esc(pbx_name) or "the PBX's"
        own = (f'<label class="switch"><input type="checkbox" name="use_own" value="1" {"checked" if b["use_own"] else ""}> '
               f'<span>Use my own branding on my faxes</span></label>'
               f'<p class="muted">Off: your faxes use {whose} branding.</p>')
    return f"""<form method="post" action="{action}" enctype="multipart/form-data" class="panel fax-brand" style="max-width:640px">{csrf}
{own}
<h4 style="margin:6px 0">Logo</h4>
{logo}
<label>Upload a logo (PNG or JPG; turned into fax black &amp; white)<br><input type="file" name="logo" accept="image/png,image/jpeg,image/gif,image/webp"></label>
{inp("company", "e.g. Acme Dental")}
<p class="muted">Shown big at the top when there's no logo, or under the logo.</p>
<label>Address<br><textarea name="address" rows="3" maxlength="200" placeholder="123 Main St&#10;Fresno, CA 93721">{esc(b["address"])}</textarea></label>
{inp("phone", "(559) 555-0100")}
{inp("email", "office@example.com")}
{inp("website", "example.com")}
<label>Footer notice (bottom of the cover page)<br><textarea name="footer" rows="3" maxlength="600" placeholder="CONFIDENTIAL: This fax is intended only for the named recipient. If you received it in error, please notify us and destroy it.">{esc(b["footer"])}</textarea></label>
{inp("header", "Acme Dental")}
<p class="muted">Printed at the top of every page next to the date, your fax number and the page number. Empty: the company name (or your name).</p>
<button class="btn">Save branding</button>
<a class="btn ghost" href="{preview_url}" target="_blank" rel="noopener">Preview cover page</a>
</form>"""


async def _save_from_form(request, login_id, user):
    f = await request.form()
    vals = {k: f.get(k) or "" for k in FIELDS}
    err = ""
    with M.db() as c:
        save(c, login_id, vals, use_own=bool(f.get("use_own")) if user else None)
        if login_id == PBX and not user:
            c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('fax_brand_users', ?)",
                      ("1" if f.get("users_allowed") else "0",))
            c.commit()
        up = f.get("logo")
        data = await up.read(MAX_LOGO + 1) if hasattr(up, "read") else b""
        if data:
            try:
                store_logo(c, login_id, data)
            except ValueError as e:
                err = str(e)
        elif f.get("remove_logo"):
            remove_logo(c, login_id)
    return err


# admin (Faxes page; admin pages are office-only like the rest of the panel)

def admin_section(s):
    with M.db() as c:
        b = load(c, PBX)
        allowed = users_allowed(c)
    csrf = M._csrf_field(s)
    form = form_html(b, "/faxes/branding", csrf, "/faxes/branding/logo", "/faxes/branding/preview")
    tick = (f'<label class="switch"><input type="checkbox" name="users_allowed" value="1" form="fbrand" '
            f'{"checked" if allowed else ""}> <span>Let users use their own branding (My Phone &rarr; Faxes)</span></label>')
    form = form.replace('<form method="post" action="/faxes/branding"', '<form id="fbrand" method="post" action="/faxes/branding"', 1)
    form = form.replace("<h4 ", tick + '<p class="muted">Faxing itself is a plan feature (Billing &rarr; plans: <em>Fax</em>).</p><h4 ', 1)
    return f"""<h3 id="branding" style="margin-top:22px">Fax branding</h3>
<p class="muted">The cover page and the header line on every page of faxes sent from this PBX. Users can
use their own instead when you allow it below.</p>{form}"""


def _admin(request):
    s = M._sess(request)
    if not s or s["role"] != "admin":
        raise HTTPException(403)
    return s


async def admin_save(request: Request):
    s = _admin(request)
    await M._check_csrf(request, s)
    if M._admin_locked():
        return M._panel_locked(s, "faxes")
    err = await _save_from_form(request, PBX, user=False)
    M._audit_log(s["username"], "faxes.branding", "saved" + (f" ({err})" if err else ""))
    q = ("brand_err=" + urllib.parse.quote(err)) if err else "brand_saved=1"
    return RedirectResponse(f"/faxes?{q}#branding", status_code=303)


def admin_logo(request: Request):
    _admin(request)
    with M.db() as c:
        p = logo_path(load(c, PBX))
    if not p:
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})


def admin_preview(request: Request):
    s = _admin(request)
    with M.db() as c:
        b = load(c, PBX)
    try:
        return _pdf_response(preview_pdf(b, _sample(request, None, b)))
    except Exception:
        raise HTTPException(500, "Couldn't draw the preview (is ghostscript installed? run pbx-update)")


# user (My Phone -> Faxes, and the app)

def user_section(s, me):
    with M.db() as c:
        if not users_allowed(c):
            return ""
        b = load(c, me["id"])
        pbx = load(c, PBX)
    csrf = M._csrf_field(s)
    form = form_html(b, "/ucp/faxes/branding", csrf, "/ucp/faxes/branding/logo", "/ucp/faxes/branding/preview",
                     user=True, pbx_name=pbx.get("company") or "")
    return f"""<details class="panel" id="branding" style="margin-top:16px"><summary><b>My fax branding</b>
<span class="muted">{'Your own' if b['use_own'] else 'Using ' + (esc(pbx.get('company')) or "the PBX's")} cover page</span></summary>
<p class="muted">Put your own logo, company details and a footer notice on your cover page, and your name on the top line of every page.</p>
{form}</details>"""


async def user_save(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if M.ucp._locked():
        return RedirectResponse("/ucp/faxes?err=" + urllib.parse.quote("Changes are disabled right now."), status_code=303)
    if not M.fax_send.entitled(me):
        return RedirectResponse("/ucp/faxes?err=" + urllib.parse.quote("Faxing isn't included in your plan."), status_code=303)
    with M.db() as c:
        if not users_allowed(c):
            return RedirectResponse("/ucp/faxes?err=" + urllib.parse.quote("Own branding is turned off by the admin."), status_code=303)
    err = await _save_from_form(request, me["id"], user=True)
    M.ucp._audit(me, "fax.branding", "saved" + (f" ({err})" if err else ""))
    if err:
        return RedirectResponse("/ucp/faxes?err=" + urllib.parse.quote(err) + "#branding", status_code=303)
    return RedirectResponse("/ucp/faxes?brand_saved=1#branding", status_code=303)


def user_logo(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    with M.db() as c:
        p = logo_path(load(c, me["id"]))
    if not p:
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})


def user_preview(request: Request):
    """The cover page this user's faxes get (web, or the app's token).
    ?own=1 previews their own branding even while it's off."""
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    with M.db() as c:
        b = for_login(c, me)
        if request.query_params.get("own") == "1" and users_allowed(c):
            b = load(c, me["id"])
    try:
        return _pdf_response(preview_pdf(b, _sample(request, me, b)))
    except Exception:
        raise HTTPException(500, "Couldn't draw the preview.")


def summary(me):
    """For the app: which branding the user's faxes use."""
    try:
        with M.db() as c:
            b = for_login(c, me)
            return {"name": b.get("company") or "", "own": b["source"] == "own",
                    "logo": bool(b.get("logo")), "editable": users_allowed(c)}
    except Exception:
        return {"name": "", "own": False, "logo": False, "editable": False}


def install(app_module):
    global M
    M = app_module
    app = M.app
    app.post("/faxes/branding")(admin_save)
    app.get("/faxes/branding/logo")(admin_logo)
    app.get("/faxes/branding/preview")(admin_preview)
    app.post("/ucp/faxes/branding")(user_save)
    app.get("/ucp/faxes/branding/logo")(user_logo)
    app.get("/ucp/faxes/branding/preview")(user_preview)
    app.get("/api/v1/me/faxes/cover-preview")(user_preview)
