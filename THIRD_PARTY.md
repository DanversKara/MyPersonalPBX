# Third-party software

own-pbx is licensed under the **GNU General Public License, version 2 or
(at your option) any later version** (GPL-2.0-or-later). See `LICENSE`.

own-pbx does not include, modify or link to the programs below. The install
scripts install them from their normal packages (Debian / upstream) and own-pbx
talks to them only through their standard interfaces (Asterisk REST Interface,
SIP, rtpengine's control socket, config files). Each keeps its own license:

| Software | License | How own-pbx uses it | Source |
|---|---|---|---|
| Asterisk | GPL-2.0 (with OpenSSL exception) | Call/media engine, driven over ARI | https://github.com/asterisk/asterisk |
| Kamailio | GPL-2.0-or-later | Internet-facing SIP edge proxy | https://github.com/kamailio/kamailio |
| rtpengine | GPL-3.0 | Media relay / SRTP on the edge | https://github.com/sipwise/rtpengine |
| fail2ban | GPL-2.0-or-later | Bans abusive IPs on the edge | https://github.com/fail2ban/fail2ban |
| acme.sh | GPL-3.0 | Let's Encrypt certificates (optional) | https://github.com/acmesh-official/acme.sh |
| sox | GPL-2.0-or-later / LGPL | Converts uploaded audio | https://sourceforge.net/projects/sox/ |
| FastAPI, Starlette, Uvicorn, Jinja2, python-multipart, requests, websocket-client | MIT / BSD / Apache-2.0 | Python libraries (installed with pip) | PyPI |
| bcrypt | Apache-2.0 | Password hashing | PyPI |

GPL-2.0-or-later was chosen because it is compatible with all of the above:
Asterisk (GPL-2.0) through version 2, Kamailio (GPL-2.0-or-later) directly,
and rtpengine (GPL-3.0) through "or later".

If you distribute a bundle (installer image, appliance, VM) that contains any
of these programs, you must pass on their licenses and offer their source code
as each license requires. Running own-pbx for yourself or as a hosted service
does not require publishing anything.

This file is information, not legal advice.
