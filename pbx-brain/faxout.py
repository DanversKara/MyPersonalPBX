# SPDX-License-Identifier: GPL-2.0-or-later
"""Sending faxes (fax_outbox, queued by pbx-api/fax_send.py).

One fax at a time: dial the number over the outbound routes with the
sender's DID as caller ID (ARI originate into pbx-brain, kind "faxout").
When the far end answers the channel continues into the dialplan's
[pbx-fax-send], where SendFAX plays the pages; its hangup handler tells
pbx-agi the result (fax-sent). When the channel is gone this module decides:
sent -> done; busy / no answer / fax not accepted -> retry a few minutes
later, up to MAX_ATTEMPTS; then failed. Answered time is billed like an
outside call (a cdr row), and the sender is emailed the outcome.
"""
import logging
import threading
import time

M = None                 # pbx-brain's app module
MAX_ATTEMPTS = 3
RETRY_SECS = 180
RING_SECS = 60
TICK = 5
log = logging.getLogger("pbx-faxout")
_lock = threading.Lock()
_inflight = {}           # channel id -> fax id


def install(app_module):
    global M
    M = app_module
    with M.db() as c:
        try:
            # A restart mid-call: that attempt is lost; try it again.
            c.execute("UPDATE fax_outbox SET status='retry', next_try=0, channel_id=''"
                      " WHERE status='sending'")
        except Exception:
            return          # table not there yet (PBX not updated)
    threading.Thread(target=_loop, daemon=True, name="faxout").start()


def _loop():
    while True:
        try:
            _tick()
        except Exception:
            log.exception("faxout tick failed")
        time.sleep(TICK)


def _tick():
    with _lock:
        if _inflight:
            return
    with M.db() as c:
        row = c.execute("SELECT f.*, l.exten, l.display_name, l.username FROM fax_outbox f"
                        " JOIN logins l ON l.id=f.login_id"
                        " WHERE f.status IN ('queued','retry') AND f.next_try <= ?"
                        " ORDER BY f.id LIMIT 1", (int(time.time()),)).fetchone()
    if row:
        _send(dict(row))


def _set(fid, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    with M.db() as c:
        c.execute(f"UPDATE fax_outbox SET {cols} WHERE id=?", tuple(kw.values()) + (fid,))


def endpoint_for(number):
    return M.trunk_endpoint_for(number)


def _send(row):
    fid = row["id"]
    if M._kill_switch_on():
        return
    if not M.can_call_outside(row["login_id"]):
        return _finish(row, False, "No outside-call minutes left in the plan.")
    ep = endpoint_for(row["to_number"])
    if not ep:
        return _finish(row, False, "No outbound route matches that number.")
    name = (row["display_name"] or row["username"] or "Fax").replace('"', "")[:40]
    header = f"{name} {row['from_did']}"[:50]
    try:
        with _lock:
            ch = M.CLIENT.channels.originate(
                endpoint=ep, app=M.APP, app_args=f"faxout,{fid}",
                caller_id=f'"{name}" <{row["from_did"]}>', timeout=RING_SECS,
                variables={"FAXOUT_ID": str(fid), "FAXOUT_FILE": row["tif_path"],
                           "FAXOUT_STATION": row["from_did"], "FAXOUT_HEADER": header})
            _inflight[ch.id] = fid
        _set(fid, status="sending", attempts=row["attempts"] + 1, channel_id=ch.id, answered_at=0,
             last_error="")
        log.info("fax %s: dialing %s from %s (attempt %d)", fid, row["to_number"], row["from_did"],
                 row["attempts"] + 1)
    except Exception as e:
        log.warning("fax %s: originate failed: %s", fid, e)
        _retry_or_fail(row, row["attempts"] + 1, "Couldn't place the call.")


def on_answer(channel, fid):
    """The fax line answered: hand the call to the dialplan's SendFAX."""
    try:
        fid = int(fid)
    except ValueError:
        channel.hangup()
        return
    _set(fid, answered_at=int(time.time()))
    log.info("fax %s: answered, sending pages", fid)
    try:
        channel.continue_in_dialplan("pbx-fax-send", "s", 1)
    except Exception:
        log.exception("fax %s: couldn't start SendFAX", fid)
        channel.hangup()


def on_destroyed(channel_id, cause_txt=""):
    """The fax call ended (any way): sent, retry or failed."""
    with _lock:
        fid = _inflight.pop(channel_id, None)
    if fid is None:
        return
    with M.db() as c:
        row = c.execute("SELECT f.*, l.exten, l.vm_email, l.display_name, l.username FROM fax_outbox f"
                        " JOIN logins l ON l.id=f.login_id WHERE f.id=?", (fid,)).fetchone()
    if not row:
        return
    row = dict(row)
    _bill(row)
    if row["status"] == "sent":
        log.info("fax %s: sent (%s pages)", fid, row["pages_sent"])
        _email(row, True)
        return
    reason = row["last_error"] or (
        "The line didn't answer or was busy." if not row["answered_at"] else
        f"The other side didn't accept the fax ({cause_txt or 'hung up'}).")
    _retry_or_fail(row, row["attempts"], reason)


def _retry_or_fail(row, attempts, reason):
    if attempts < MAX_ATTEMPTS:
        _set(row["id"], status="retry", next_try=int(time.time()) + RETRY_SECS, last_error=reason, channel_id="")
        log.info("fax %s: %s; retrying in %ds", row["id"], reason, RETRY_SECS)
    else:
        _finish(row, False, reason)


def _finish(row, ok, reason=""):
    _set(row["id"], status="sent" if ok else "failed", last_error=reason, channel_id="")
    log.info("fax %s: %s", row["id"], "sent" if ok else "failed: " + reason)
    if not ok:
        _email(dict(row, last_error=reason), False)


def _bill(row):
    """Answered time counts as outside minutes, like any outside call."""
    if not row.get("answered_at"):
        return
    secs = max(1, int(time.time()) - int(row["answered_at"]))
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(row["answered_at"])))
    try:
        with M.db() as c:
            c.execute("INSERT INTO cdr (call_id, started_at, src, dst, direction, login_id, duration_sec,"
                      " bill_sec, disposition) VALUES (?,?,?,?,?,?,?,?,?)",
                      (f"fax-{row['id']}-{row['attempts']}", ts, row.get("exten") or "", row["to_number"],
                       "outbound", row["login_id"], secs, secs, "FAX"))
    except Exception:
        log.exception("fax %s: cdr failed", row["id"])


def _email(row, ok):
    to = (row.get("vm_email") or "").strip()
    if not to:
        return
    def send():
        import mailer
        try:
            with M.db() as c:
                cfg = mailer.settings(c)
            if not mailer.configured(cfg):
                return
            who = (row.get("to_name") + " " if row.get("to_name") else "") + row["to_number"]
            if ok:
                mailer.send(cfg, to, f"Fax to {who} sent",
                            f"Your fax to {who} was sent ({row.get('pages_sent') or row['pages']} page(s)) "
                            f"from {row['from_did']}.\n")
            else:
                mailer.send(cfg, to, f"Fax to {who} failed",
                            f"Your fax to {who} couldn't be sent after {row['attempts']} attempt(s).\n\n"
                            f"Reason: {row.get('last_error') or 'unknown'}\n\nYou can send it again from My Phone or the app.\n")
        except Exception as e:  # noqa: BLE001
            log.warning("fax %s: email failed: %s", row["id"], e)
    threading.Thread(target=send, daemon=True).start()
