#!/opt/pbx/api-venv/bin/python
# SPDX-License-Identifier: GPL-2.0-or-later
"""Parse `pjsip show contacts` into JSON. Reads Asterisk CLI output on stdin,
writes {"contacts": [...]} on stdout. Called by pbx-status.sh (root)."""
import json
import math
import re
import sys


def main():
    contacts = []
    for line in sys.stdin:
        m = re.match(r'^\s*Contact:\s+(\S+)\s+([0-9a-f]+)\s+(\S+)\s+(\S+)\s*$',
                     line.rstrip())
        if not m:
            continue
        aor_uri, _hash, status, rtt = m.groups()
        aor, _, uri = aor_uri.partition('/')
        ip, port, transport = '', '', ''
        mu = re.search(r'@([^;:\s]+)(?::(\d+))?', uri)
        if mu:
            ip, port = mu.group(1), mu.group(2) or ''
        mt = re.search(r';transport=([a-z]+)', uri, re.I)  # phones write TLS or tls
        if mt:
            transport = mt.group(1).lower()
        # Asterisk prints "nan" for contacts not yet qualified; float("nan")
        # parses but is not valid JSON, so treat any non-finite value as unknown.
        try:
            rtt_ms = float(rtt)
            if not math.isfinite(rtt_ms):
                rtt_ms = None
        except ValueError:
            rtt_ms = None
        contacts.append({"aor": aor, "ip": ip, "port": port,
                         "transport": transport, "status": status,
                         "rtt_ms": rtt_ms})
    print(json.dumps({"contacts": contacts}))


if __name__ == "__main__":
    main()
