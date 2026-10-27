# SPDX-License-Identifier: GPL-2.0-or-later
"""Sending faxes: who may send from which number, turning an upload (PDF or
picture) plus an optional cover page into fax pages, and queueing it.

The actual call is made by pbx-brain (faxout.py): it dials the number over
the outbound routes with the chosen DID as caller ID and SendFAX plays the
pages ([pbx-fax-send] in extensions.conf), retrying a busy / unanswered line.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import entitlements as ent

M = None
FAX_DIR = os.environ.get("PBX_FAX_DIR", "/var/spool/pbx/fax")
MAX_UPLOAD = 20 * 1024 * 1024
MAX_PAGES = 50
DAILY_LIMIT = 30
MAX_ATTEMPTS_HINT = 3      # pbx-brain faxout.MAX_ATTEMPTS
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")
ACCEPT = (".pdf",) + IMAGE_EXT


def out_dir():
    return os.path.join(FAX_DIR, "out")


def digits(v):
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def norm(v):
    d = digits(v)
    return d[1:] if len(d) == 11 and d.startswith("1") else d


# ------------------------------------------------------------------ who may send

def allowed_numbers(me):
    """DIDs this login may send from: numbers that ring them (all, for admins)."""
    ext = me["exten"] or ""
    out = []
    with M.db() as c:
        groups = {r["id"]: r["members"] or "[]" for r in c.execute("SELECT id, members FROM ring_groups")}
        for r in c.execute("SELECT * FROM inbound_routes WHERE enabled=1 ORDER BY did"):
            r = dict(r)
            r.setdefault("group_id", None)
            mine = me["role"] == "admin"
            if not mine and ext:
                try:
                    mine = ext in [str(x) for x in json.loads(r["ring_extens"] or "[]")]
                except ValueError:
                    mine = False
                if not mine and r["group_id"] in groups:
                    try:
                        mine = ext in [str(x) for x in json.loads(groups[r["group_id"]])]
                    except ValueError:
                        pass
            if mine:
                n = norm(r["did"])
                if n and n not in out:
                    out.append(n)
    return out


def minutes_ok(me):
    if me["role"] == "admin":
        return True
    with M.db() as c:
        q = ent.quota(c, me["id"], "call_minutes")
        if q == ent.UNLIMITED:
            return True
        return q > 0 and ent.usage(c, me["id"], "call_minutes") < q


def send_status(me):
    """(can_send, reason if not)."""
    if not allowed_numbers(me):
        return False, "No phone number is set up for you to send from. Ask the admin."
    if not fax_ready():
        return False, "This PBX can't send faxes yet (the admin needs to run pbx-update --with-fax)."
    if not minutes_ok(me):
        return False, "Sending a fax uses outside-call minutes, and your plan has none left."
    return True, ""


def fax_ready():
    try:
        return bool(M._asterisk_status().get("fax_ready"))
    except Exception:
        return False


# ------------------------------------------------------------------ building the pages

def _ps_text(s):
    s = str(s or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in s)


def _wrap(text, width=78, max_lines=28):
    lines = []
    for para in str(text or "").splitlines() or [""]:
        words, cur = para.split(), ""
        if not words:
            lines.append("")
        for w in words:
            if len(cur) + len(w) + 1 > width:
                lines.append(cur)
                cur = w[:width]
            else:
                cur = (cur + " " + w).strip()
        if words:
            lines.append(cur)
    return lines[:max_lines]


def cover_ps(path, c):
    """A plain cover page (PostScript) from the details in dict c."""
    y, out = 700, ["%!PS", "/H {/Helvetica findfont exch scalefont setfont} def",
                   "/HB {/Helvetica-Bold findfont exch scalefont setfont} def",
                   "48 HB 72 700 moveto (FAX) show"]
    y = 640
    rows = [("To", c.get("to_name")), ("Fax", c.get("to_number")), ("From", c.get("from_name")),
            ("Phone", c.get("from_number")), ("Date", time.strftime("%B %d, %Y  %I:%M %p")),
            ("Pages", f"{c.get('pages', '?')} including this cover"), ("Subject", c.get("subject"))]
    for k, v in rows:
        if not v:
            continue
        out.append(f"14 HB 72 {y} moveto ({_ps_text(k)}:) show 14 H 160 {y} moveto ({_ps_text(v)[:70]}) show")
        y -= 26
    if c.get("note"):
        y -= 14
        out.append(f"1 setlinewidth 72 {y + 16} moveto 540 {y + 16} lineto stroke")
        for line in _wrap(c["note"]):
            out.append(f"12 H 72 {y} moveto ({_ps_text(line)}) show")
            y -= 17
    out.append("showpage")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def _image_to_pdf(src, dst):
    from PIL import Image, ImageOps
    img = Image.open(src)
    frames = []
    try:
        n = getattr(img, "n_frames", 1)
        for i in range(min(n, MAX_PAGES)):
            img.seek(i)
            fr = ImageOps.exif_transpose(img.copy()).convert("L")
            frames.append(fr)
    finally:
        img.close()
    if not frames:
        raise ValueError("That picture couldn't be read.")
    frames[0].save(dst, "PDF", save_all=True, append_images=frames[1:], resolution=200)


def _gs(args, timeout=180):
    r = subprocess.run(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE"] + args,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise ValueError("The document couldn't be turned into fax pages "
                         "(is it a normal, unprotected PDF?).")


def _tiff_pages(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return getattr(im, "n_frames", 1)
    except Exception:
        r = subprocess.run(["tiffinfo", path], capture_output=True, text=True)
        return max(1, r.stdout.count("TIFF Directory"))


def build(job, data, filename, cover):
    """Write job/fax.tif (what is sent) and job/fax.pdf (to look at).
    Returns the page count. Raises ValueError with a readable reason."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ACCEPT:
        raise ValueError("Send a PDF or a picture (JPG, PNG, TIFF). Save other documents as PDF first.")
    src = os.path.join(job, "upload" + ext)
    with open(src, "wb") as f:
        f.write(data)
    doc = src
    if ext in IMAGE_EXT:
        doc = os.path.join(job, "upload.pdf")
        try:
            _image_to_pdf(src, doc)
        except ImportError:
            raise ValueError("Pictures need the Pillow package on the PBX (run pbx-update). PDFs work.")
        except Exception:
            raise ValueError("That picture couldn't be read.")
    tif = os.path.join(job, "fax.tif")
    body_tif = os.path.join(job, "body.tif")
    base = ["-sDEVICE=tiffg3", "-r204x196", "-sPAPERSIZE=letter", "-dFIXEDMEDIA", "-dPDFFitPage"]
    _gs(base + [f"-sOutputFile={body_tif}", doc])
    pages = _tiff_pages(body_tif)
    if pages > MAX_PAGES:
        raise ValueError(f"That's {pages} pages; the limit is {MAX_PAGES}.")
    if cover:
        cover = dict(cover, pages=pages + 1)
        ps = os.path.join(job, "cover.ps")
        cover_ps(ps, cover)
        _gs(base + [f"-sOutputFile={tif}", ps, doc])
        pages += 1
        os.remove(body_tif)
    else:
        os.replace(body_tif, tif)
    pdf = os.path.join(job, "fax.pdf")
    try:
        subprocess.run(["tiff2pdf", "-o", pdf, tif], check=True, timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass
    for p in (src, doc):
        try:
            os.remove(p)
        except OSError:
            pass
    return pages


def queue(me, data, filename, to_number, from_did, to_name="", subject="", note="", cover=True):
    """(fax_outbox id, error). Checks everything, builds the pages, queues it."""
    ok, why = send_status(me)
    if not ok:
        return None, why
    to = digits(to_number)
    if not 10 <= len(to) <= 15:
        return None, "Enter the full fax number, e.g. 3105550100."
    if len(to) == 10:
        to = "1" + to
    did = norm(from_did)
    if did not in allowed_numbers(me):
        return None, "Pick one of your numbers to send from."
    if not data:
        return None, "Choose a file to fax."
    if len(data) > MAX_UPLOAD:
        return None, "That file is over 20 MB."
    with M.db() as c:
        today = c.execute("SELECT COUNT(*) FROM fax_outbox WHERE login_id=? AND created_at > datetime('now','-1 day')",
                          (me["id"],)).fetchone()[0]
    if today >= DAILY_LIMIT and me["role"] != "admin":
        return None, f"That's {DAILY_LIMIT} faxes in a day; try again tomorrow."
    os.makedirs(out_dir(), exist_ok=True)
    job = tempfile.mkdtemp(prefix="job-", dir=out_dir())
    os.chmod(job, 0o755)      # Asterisk (root) reads it; nothing secret beyond the fax itself
    try:
        pages = build(job, data, filename, {
            "to_name": to_name.strip()[:60], "to_number": to, "subject": subject.strip()[:80],
            "note": note.strip()[:2000], "from_name": (me["display_name"] or me["username"] or "")[:60],
            "from_number": did} if cover else None)
    except ValueError as e:
        shutil.rmtree(job, ignore_errors=True)
        return None, str(e)
    except subprocess.TimeoutExpired:
        shutil.rmtree(job, ignore_errors=True)
        return None, "That document took too long to convert. Try a smaller one."
    pdf = os.path.join(job, "fax.pdf")
    with M.db() as c:
        fid = c.execute(
            "INSERT INTO fax_outbox (login_id, from_did, to_number, to_name, subject, note, cover, pages,"
            " status, tif_path, pdf_path, filename) VALUES (?,?,?,?,?,?,?,?,'queued',?,?,?)",
            (me["id"], did, to, to_name.strip()[:60], subject.strip()[:80], note.strip()[:2000],
             1 if cover else 0, pages, os.path.join(job, "fax.tif"), pdf if os.path.isfile(pdf) else "",
             os.path.basename(filename or "")[:120])).lastrowid
        c.commit()
    return fid, ""


def sent_for(me, limit=200):
    with M.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, from_did, to_number, to_name, subject, pages, status, attempts, last_error, created_at, sent_at,"
            " pdf_path != '' AS has_pdf FROM fax_outbox WHERE login_id=? AND status != 'deleted' ORDER BY id DESC LIMIT ?",
            (me["id"], limit))]


def cancel_or_delete(me, ids, admin=False):
    """Queued/retrying ones are cancelled; finished ones deleted (with their files)."""
    n = 0
    with M.db() as c:
        for i in ids:
            r = c.execute("SELECT * FROM fax_outbox WHERE id=?" + ("" if admin else " AND login_id=?"),
                          (i,) if admin else (i, me["id"])).fetchone()
            if not r or r["status"] == "sending":
                continue
            job = os.path.dirname(r["tif_path"] or "")
            if job and os.path.realpath(job).startswith(os.path.realpath(out_dir()) + os.sep):
                shutil.rmtree(job, ignore_errors=True)
            c.execute("UPDATE fax_outbox SET status='deleted', tif_path='', pdf_path='' WHERE id=?", (i,))
            n += 1
        c.commit()
    return n


def sent_pdf(me, fid, admin=False):
    with M.db() as c:
        r = c.execute("SELECT pdf_path FROM fax_outbox WHERE id=?" + ("" if admin else " AND login_id=?"),
                      (fid,) if admin else (fid, me["id"])).fetchone()
    p = os.path.realpath((r["pdf_path"] if r else "") or "")
    return p if r and p.startswith(os.path.realpath(out_dir()) + os.sep) and os.path.isfile(p) else ""


def install(app_module):
    global M
    M = app_module
