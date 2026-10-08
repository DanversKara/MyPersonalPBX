# SPDX-License-Identifier: GPL-2.0-or-later
"""Per-user feature quotas: the single hook a billing system plugs into.

Today an admin sets quotas (or the default applies). Later, a billing job
writes the same table when a plan is bought, renewed or lapses, e.g.

    set_quota(db, login_id, "ivr_menus", 5, source="plan:pro")
    set_quota(db, login_id, "ivr_menus", 0, source="plan:lapsed")

Everything that gates a feature calls quota(); nothing else needs to change.
pbx-brain mirrors quota() so a lapsed plan also stops menus answering calls.

Features (quota meaning):
  ivr_menus     how many IVR menus the user may own        (0 = none)
  call_minutes  outside-call minutes per calendar month    (0 = none, -1 = unlimited)
  voicemail     callers can leave messages                 (0 = off, 1 = on)
  messages      texts the user may send per month          (0 = none, -1 = unlimited)
  recording     "record my calls" available                (0 = off, 1 = on)

Internal (extension-to-extension) calls are always free. Admin logins are
never limited. Monthly counters reset on the 1st (server local time).
pbx-brain and agi_server.py mirror quota()/usage so limits apply live.
"""

UNLIMITED = -1

# key: (label, kind)  kind: count | monthly | bool
FEATURES = {
    "call_minutes": ("Outside call minutes", "monthly"),
    "voicemail": ("Voicemail", "bool"),
    "messages": ("Text messages", "monthly"),
    "recording": ("Call recording", "bool"),
    "ivr_menus": ("IVR menus", "count"),
}

# Shown to users before they can turn on "Record my calls". Bump the
# version when the wording changes materially: users then re-acknowledge.
RECORDING_CONSENT_VERSION = 1
RECORDING_NOTICE = (
    "Call-recording laws vary by country, state and city. Some US states "
    "(for example California, Florida, Illinois, Maryland, Massachusetts, "
    "Pennsylvania and Washington) require the consent of everyone on the call "
    "before it is recorded. You are responsible for knowing and following the "
    "laws that apply to you and to the people you call, including telling "
    "them the call is being recorded. This is not legal advice."
)
RECORDING_ACK = ("I have read this and will check and follow my local, state and federal "
                 "call-recording laws, including getting consent where required.")


def usage(c, login_id, feature) -> int:
    """This month's use of a monthly feature."""
    if feature == "call_minutes":
        r = c.execute(
            "SELECT COALESCE(SUM((bill_sec + 59) / 60), 0) FROM cdr"
            " WHERE started_at >= datetime('now', 'localtime', 'start of month')"
            " AND ((login_id=? AND direction IN ('outbound', 'forwarded'))"
            "   OR (answered_login_id=? AND direction='inbound'))",
            (login_id, login_id)).fetchone()
        return int(r[0] or 0)
    if feature == "messages":
        r = c.execute(
            "SELECT COUNT(*) FROM messages m"
            " WHERE m.login_id=? AND m.sent_at >= datetime('now', 'localtime', 'start of month', 'utc')",
            (login_id,)).fetchone()
        return int(r[0] or 0)
    if feature == "ivr_menus":
        return int(c.execute("SELECT COUNT(*) FROM ivr_menus WHERE owner_login_id=?",
                             (login_id,)).fetchone()[0])
    return 0


def is_admin(c, login_id) -> bool:
    r = c.execute("SELECT role FROM logins WHERE id=?", (login_id,)).fetchone()
    return bool(r) and r[0] == "admin"


def describe(feature, value) -> str:
    """Human text for a quota value, e.g. '500 minutes / month'."""
    label, kind = FEATURES[feature]
    v = int(value)
    if kind == "bool":
        return label if v else ""
    if v == UNLIMITED:
        return {"call_minutes": "Unlimited outside calls", "messages": "Unlimited texts"}.get(feature, f"Unlimited {label.lower()}")
    if v <= 0:
        return ""
    if feature == "call_minutes":
        return f"{v:,} outside call minutes / month"
    if feature == "messages":
        return f"{v:,} texts / month"
    if feature == "ivr_menus":
        return f"{v} IVR menu{'s' if v != 1 else ''}"
    return f"{v} {label}"


def quota(c, login_id, feature) -> int:
    """c: sqlite3 connection. Row wins, else default_quota_<feature>, else 0."""
    r = c.execute("SELECT quota FROM user_entitlements WHERE login_id=? AND feature=?",
                  (login_id, feature)).fetchone()
    if r is not None:
        return int(r[0])
    d = c.execute("SELECT value FROM kv_settings WHERE key=?",
                  ("default_quota_" + feature,)).fetchone()
    try:
        return int(d[0]) if d and str(d[0]).strip() else 0
    except ValueError:
        return 0


def has_override(c, login_id, feature) -> bool:
    return c.execute("SELECT 1 FROM user_entitlements WHERE login_id=? AND feature=?",
                     (login_id, feature)).fetchone() is not None


def set_quota(c, login_id, feature, value, source="admin"):
    """Set a user's quota (value=None removes the override -> default)."""
    if value is None:
        c.execute("DELETE FROM user_entitlements WHERE login_id=? AND feature=?",
                  (login_id, feature))
    else:
        c.execute("INSERT INTO user_entitlements (login_id, feature, quota, source, updated_at)"
                  " VALUES (?,?,?,?,datetime('now'))"
                  " ON CONFLICT(login_id, feature) DO UPDATE SET quota=excluded.quota,"
                  " source=excluded.source, updated_at=excluded.updated_at",
                  (login_id, feature, int(value), source))
    c.commit()


def default_quota(c, feature) -> int:
    d = c.execute("SELECT value FROM kv_settings WHERE key=?",
                  ("default_quota_" + feature,)).fetchone()
    try:
        return int(d[0]) if d else 0
    except ValueError:
        return 0


def set_default_quota(c, feature, value):
    c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?,?)",
              ("default_quota_" + feature, str(int(value))))
    c.commit()
