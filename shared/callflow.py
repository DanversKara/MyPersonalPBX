# SPDX-License-Identifier: GPL-2.0-or-later
"""Call flow decisions: time conditions and caller ID rules (pure logic).

Time condition (time_conditions row):
  hours     {"mon": [["09:00","17:00"]], ...}  open periods per weekday; an
            end before its start runs past midnight (e.g. 22:00-02:00)
  holidays  [{"date": "2026-12-25", "name": "Christmas"}, {"date": "07-04"}]
            YYYY-MM-DD for one day, MM-DD for every year
  override  auto | open | closed   (admin's manual switch)
  tz        IANA zone ('' = the server's local time)
  -> state open | closed | holiday, and its destination.

Caller rule (caller_rules row), checked before a number's normal route:
  did       '' = any of your numbers, else that number
  match     exact (one number) | prefix (area code / start of number) |
            anonymous (no caller ID / blocked / "Restricted")
  dest      where the call goes (see DEST_TYPES), e.g. a ring group, or
            route_novm = the number's normal route but no voicemail.
First enabled match wins, numbers-specific rules before "any number" ones.

Used by pbx-brain (routing) and pbx-api (pages, 'open now?' display).
"""
import datetime
import json

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABEL = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
             "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
# Every destination a time condition / caller rule / IVR key can use.
DEST_TYPES = {
    "rgroup": "Ring group",
    "ext": "Ring extension",
    "group": "Ring extensions (list)",
    "vm": "Voicemail",
    "ivr": "IVR menu",
    "tc": "Time condition",
    "route_novm": "Normal route, but no voicemail (hang up if unanswered)",
    "disconnected": "Not in service (message, then hang up)",
    "hangup": "Hang up",
}
ANON_WORDS = ("anonymous", "restricted", "private", "unknown", "unavailable", "blocked")


def jload(v, default):
    try:
        x = json.loads(v) if isinstance(v, str) else v
    except (ValueError, TypeError):
        return default
    return x if isinstance(x, type(default)) else default


def digits(v):
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def norm(v):
    d = digits(v)
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def _mins(hhmm):
    try:
        h, m = str(hhmm).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 24 and 0 <= m < 60 and h * 60 + m <= 1440:
            return h * 60 + m
    except (ValueError, AttributeError):
        pass
    return None


def parse_periods(text):
    """'9:00-12:00, 13:00-17:30' -> [["09:00","12:00"], ...] (raises ValueError)."""
    out = []
    for part in str(text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part.lower() in ("closed", "off", "-"):
            continue
        if part.lower() in ("24h", "all day", "open"):
            out.append(["00:00", "24:00"])
            continue
        a, sep, b = part.partition("-")
        ma, mb = _mins(a), _mins(b)
        if not sep or ma is None or mb is None or ma == mb:
            raise ValueError(f'"{part}" isn\'t a time range like 9:00-17:00')
        out.append(["%02d:%02d" % divmod(ma, 60), "%02d:%02d" % divmod(mb, 60)])
    return out[:6]


def periods_text(periods):
    if not periods:
        return ""
    return ", ".join(("24h" if a == "00:00" and b == "24:00" else f"{a}-{b}") for a, b in periods)


def now_in(tz, now=None):
    if now is not None:
        return now
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return datetime.datetime.now(ZoneInfo(tz))
        except Exception:
            pass
    return datetime.datetime.now()


def is_holiday(holidays, d):
    for h in holidays or []:
        v = str((h or {}).get("date", "")).strip()
        if v == d.strftime("%Y-%m-%d") or v == d.strftime("%m-%d"):
            return (h or {}).get("name") or "Holiday"
    return None


def is_open(hours, now):
    """Open by the weekly hours at `now` (handles periods past midnight)."""
    m = now.hour * 60 + now.minute
    today = DAYS[now.weekday()]
    yesterday = DAYS[(now.weekday() - 1) % 7]
    for a, b in hours.get(today, []):
        ma, mb = _mins(a), _mins(b)
        if ma is None or mb is None:
            continue
        if ma < mb and ma <= m < mb:
            return True
        if mb < ma and m >= ma:            # starts today, ends after midnight
            return True
    for a, b in hours.get(yesterday, []):
        ma, mb = _mins(a), _mins(b)
        if ma is not None and mb is not None and mb < ma and m < mb:
            return True                    # yesterday's late period still running
    return False


def tc_state(tc, now=None):
    """('open'|'closed'|'holiday', why) for a time condition row/dict."""
    tc = dict(tc)
    ov = (tc.get("override") or "auto").lower()
    if ov == "open":
        return "open", "set to Open by the admin"
    if ov == "closed":
        return "closed", "set to Closed by the admin"
    now = now_in(tc.get("tz") or "", now)
    hol = is_holiday(jload(tc.get("holidays"), []), now.date())
    if hol:
        return "holiday", hol
    if is_open(jload(tc.get("hours"), {}), now):
        return "open", "within opening hours"
    return "closed", "outside opening hours"


def tc_dest(tc, now=None):
    """(destination dict, state, why) a call to this time condition goes to."""
    tc = dict(tc)
    state, why = tc_state(tc, now)
    key = {"open": "open_dest", "closed": "closed_dest", "holiday": "holiday_dest"}[state]
    d = jload(tc.get(key), {})
    if state == "holiday" and not d.get("type"):
        d = jload(tc.get("closed_dest"), {})          # no holiday choice: as closed
    return (d if d.get("type") else {"type": "hangup"}), state, why


def is_anonymous(caller):
    c = str(caller or "").strip().lower()
    return not digits(c) or any(w in c for w in ANON_WORDS) or len(digits(c)) < 3


def rule_matches(rule, did, caller):
    rule = dict(rule)
    if not rule.get("enabled", 1):
        return False
    rdid = norm(rule.get("did") or "")
    if rdid and rdid != norm(did):
        return False
    kind = rule.get("match") or "exact"
    if kind == "anonymous":
        return is_anonymous(caller)
    if is_anonymous(caller):
        return False
    pat, num = norm(rule.get("pattern") or ""), norm(caller)
    if not pat:
        return False
    if kind == "prefix":
        return num.startswith(pat) or digits(caller).startswith(digits(rule.get("pattern")))
    return num == pat


def match_rule(rules, did, caller):
    """The first matching rule: number-specific before 'any number', then by
    their order (sort, id). None if no rule applies."""
    rules = [dict(r) for r in rules]
    rules.sort(key=lambda r: (0 if norm(r.get("did") or "") else 1, r.get("sort") or 0, r.get("id") or 0))
    for r in rules:
        if rule_matches(r, did, caller):
            return r
    return None


def describe_dest(d, names=None):
    """Short text for a destination; names: {('rgroup', '3'): 'Front desk', ...}."""
    d = d or {}
    t = d.get("type") or "hangup"
    tg = str(d.get("target") or "")
    label = DEST_TYPES.get(t, t)
    if t in ("rgroup", "ivr", "tc"):
        return f"{label}: {(names or {}).get((t, tg), '#' + tg)}"
    if t in ("ext", "vm", "group"):
        return f"{label} {tg}"
    return label
