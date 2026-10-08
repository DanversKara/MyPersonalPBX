# Developer tools

<!-- SPDX-License-Identifier: GPL-2.0-or-later -->

Not needed to install or run own-pbx. These are helpers used while developing
and testing:

| File | What it does |
|---|---|
| `sandbox-up.sh`, `sandbox-recover.sh` | start / repair a throwaway test environment |
| `seed-test-db.py` | fill a test database with example logins |
| `sip-test.py` | send test SIP requests (register / call) |
| `sip-capture.py`, `sip-capture2.py` | print SIP traffic on a port for a while (`python3 sip-capture.py 60 <phone-ip>`) |
| `test-ext-api.py` | exercise the REST API end to end against a running panel |
| `test-vm97.py` | test the `*97` voicemail flow |

Run them on a test machine, not on a PBX you rely on.
