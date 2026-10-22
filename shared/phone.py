# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared phone-number helpers for the own-PBX services.

The canonical form is digits-only, with US/Canada numbers stored as the
10 national digits (a leading "1" on an 11-digit NANP number is dropped),
so `+1 310-555-0100`, `13105550100` and `3105550100` all become `3105550100`.
This is the same form blocked_numbers is keyed on, so contacts, block
lists and caller-ID matching all agree with each other.

One file, deployed into both service dirs (see scripts/deploy.sh): pbx-brain
uses it for call-time name lookup, pbx-api for contacts / notes / the panel.
"""

import re

_DIGITS_RE = re.compile(r"\D")


def norm_number(v) -> str:
    """Canonical digits for a phone number, or "" when there are none.

    Extensions (short digit strings) pass through untouched; only an
    11-digit number starting with 1 is shortened to its 10 national digits.
    Non-NANP international numbers keep all their digits.
    """
    d = _DIGITS_RE.sub("", str(v or ""))
    if len(d) == 11 and d.startswith("1"):
        return d[1:]
    return d


def looks_like_outside(v) -> bool:
    """True for dialable outside numbers (7+ digits), not extensions."""
    return len(norm_number(v)) >= 7
