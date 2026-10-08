# own-pbx

*A self-hosted home/hobby phone system (Asterisk 22 + Kamailio). Published as-is for anyone curious; not actively supported. Forks welcome. Untested 911 - see below.*

A **home / hobby PBX**: a self-hosted phone system for enthusiasts who want to
run their own phones at home and learn how it all works. Plain **Asterisk 22**
does the calling, a small **Python** service makes every call decision, a clean
**web panel** runs the whole thing, and a separate **Kamailio + rtpengine edge
box** is the only part that faces the internet.

No third-party GUI, no module store, no phoning home. Free software,
GPL-2.0-or-later.

> ## ⚠️ Project status - please read
>
> Why I Made This
I’ve tried a lot of different PBX systems, and while many of them are great, I always found myself running into the same problem: they either had too many features I didn’t need, were missing features I actually wanted, or put certain features behind expensive premium plans.

Sometimes I would find a PBX that had a few of the features I wanted, but other useful features were locked behind a paid plan. Then I’d try another PBX that offered those features for free, only to find that it was missing something else I wanted.

Eventually, I decided to stop looking and build my own.

This project started as a hobby PBX built around the features I personally wanted. The goal isn’t to compete with every major PBX system or include every feature imaginable. Instead, I want to build something that is:

Simple and easy to understand
Focused on useful features instead of unnecessary extras
Free from unnecessary premium feature restrictions
Customizable and easy to expand
Built around the features that actually matter to me
The project is still a work in progress, and I’ll be adding new features and improving existing ones over time.

Is It Perfect?
No. This is a hobby project, and there will probably be bugs, missing features, and things that could be done better.

Is It Secure?
Security is something I take seriously, but I’m also realistic about where this project is right now. Use it at your own risk, especially in production or on systems exposed to the internet.

I’ll continue testing, fixing issues, and improving security as the project develops.

At the end of the day, this project is about building the PBX I wanted to use from the beginning and learning along the way.

It’s a work in progress, and I’m looking forward to seeing where it goes.
>
> - **Hobby project, not a business product.** own-pbx is built and tested as
>   a home/hobby PBX. It is **not** intended or certified for business,
>   commercial, multi-tenant, carrier or life-safety use. It comes with
>   **no warranty**; you use it entirely at your own risk (see `LICENSE`).
> - **Only tested with VoIP.ms.** VoIP.ms is the only SIP trunk provider it
>   has been tested with. Other providers may need changes.
> - **911 / E911 has NOT been tested.** The 911 features exist but have not
>   been tested end to end. **Do not rely on this system to call 911.** Always
>   keep another way to reach emergency services (a mobile phone or landline).
> - **Stripe billing has not been tested with real payments.**
> - **You are responsible** for your provider's terms, call-recording consent
>   and any laws that apply where you live.
>
> **Published as-is.** I'm sharing this for anyone curious. It is not actively
> supported: I won't be answering questions, fixing issues or taking feature
> requests. You're welcome to fork it and adapt it however you like, under the
> GPL-2.0-or-later license.

![Dashboard](docs/screenshots/02-dashboard.png)

> All screenshots use made-up demo data (names like "Front Desk" are just examples).

---

## Contents

1. [What you get](#1-what-you-get)
2. [How it fits together](#2-how-it-fits-together)
3. [What you need](#3-what-you-need)
4. [Install the PBX server](#4-install-the-pbx-server)
5. [Install the edge server (for phones away from home)](#5-install-the-edge-server)
6. [Router, DNS and certificate](#6-router-dns-and-certificate)
7. [First-time setup in the panel](#7-first-time-setup-in-the-panel)
8. [Setting up phones](#8-setting-up-phones)
9. [The admin panel, page by page](#9-the-admin-panel-page-by-page)
10. [My Phone: the user panel](#10-my-phone-the-user-panel)
11. [Phone feature codes](#11-phone-feature-codes)
12. [911 / E911](#12-911--e911)
13. [Call recording and the law](#13-call-recording-and-the-law)
14. [Plans and billing (Stripe)](#14-plans-and-billing-stripe)
15. [Security](#15-security)
16. [Opening My Phone to the internet (optional)](#16-opening-my-phone-to-the-internet-optional)
17. [Updating](#17-updating)
18. [Maintenance and housekeeping](#18-maintenance-and-housekeeping)
19. [Troubleshooting](#19-troubleshooting)
20. [Files and folders](#20-files-and-folders)
21. [SMS/MMS with voip.ms](#21-smsmms-with-voipms)
22. [License](#22-license)

---

## 1. What you get

**Calling**
- Extensions (phones) with their own voicemail, call forwarding, Do Not
  Disturb and ring time.
- Calls in and out through a SIP trunk provider (**only tested with VoIP.ms**).
- **Ring groups**: ring several phones at once; choose what happens when
  nobody answers (one person's voicemail, a copy for everyone, another phone,
  another group, a menu, an outside number, or hang up).
- **IVR menus** ("press 1 for ...") for your main number and for each user.
- Optional **"dial 9 for an outside line"** (off, optional or required).
- Conference room, voicemail by phone, greeting recording by phone.

**Phones anywhere**
- Desk phones and softphones (e.g. Zoiper) at home talk to the PBX directly.
- Phones away from home connect through the edge box with **encrypted
  signalling (TLS)** and **encrypted audio (SRTP)**. The PBX itself is never
  reachable from the internet.
- Works with a changing home/office internet address (Cloudflare dynamic DNS).

**Panels**
- **Admin panel**: users, trunks, routes, ring groups, IVR, call records,
  recordings, voicemail, , billing, email, network, E911, reports, API keys.
- **My Phone** for every user: calls, voicemail, recordings, texts, IVR,
  billing and settings, also on a phone screen.

**Messaging**
- Extension-to-extension texts from phones and from My Phone, emailed to the
  recipient if they want; block list, reports and on/off switches.
- **Two-way SMS/MMS with the outside world through voip.ms** (see section 21):
  each DID routes to an extension — inbound via URL callback plus a polling
  safety net, outbound via the voip.ms API from Zoiper, My Phone or ESP/HA devices.

**Safety and security**
- Kill switch (stop every call at once) and safety lock (freeze all changes).
- Sign-in lockouts, fail2ban on the edge, flood limit, security alerts by email,
  a live Activity feed.
- **911 always connects** (by design), with per-user E911 addresses behind a separate unlock code - **untested, see section 12**.
- Call recording off by default, with legal acknowledgements and an optional
  "this call may be recorded" announcement.

**Plans / billing (optional, untested with real payments)**
- Monthly plans through **Stripe**: outside minutes, voicemail, texts,
  recording and IVR menus per plan. Users without a plan can still call other
  extensions.

---

## 2. How it fits together

```
                     Internet
                        │
          TCP 5061 (TLS) + UDP 20000-20099 (SRTP audio)
                        │
                 ┌──────▼───────┐      Router forwards ONLY these to the edge.
                 │  Edge server │      Kamailio (SIP proxy) + rtpengine (audio relay)
                 │ 192.168.1.11 │      fail2ban, flood limit, TLS certificate
                 └──────┬───────┘
                        │  plain SIP/RTP on your LAN
                 ┌──────▼───────┐
  Office phones ─►  PBX server  │      Asterisk 22 (calls, audio, voicemail)
  (UDP 5060)     │ 192.168.1.10 │      pbx-brain  (Python: every call decision)
                 │              │      pbx-agi    (Python: text )
                 │              │      pbx-api    (Python: web panel, port 8001)
                 └──────────────┘      SQLite database /var/lib/pbx/pbx.db
                        │
                 SIP trunk provider (outbound registration)
```

- **Asterisk** handles the low-level work: SIP, audio, recording and playing
  sounds. It has almost no dialplan; every call is handed to pbx-brain.
- **pbx-brain** decides what happens to every call: who rings, forwarding,
  voicemail, ring groups, IVR, 911, plans and blocking.
- **pbx-api** is the web panel and REST API. It owns the database and rebuilds
  Asterisk's phone list (`pjsip.conf`) whenever you change users or trunks.
- **The edge server** is optional. Without it, the system works fully inside
  your network; you only need it for phones away from home.

Ports at a glance:

| Where | Port | What | Who can reach it |
|---|---|---|---|
| PBX | 5060 UDP/TCP | SIP for office phones and the edge | your LAN only |
| PBX | 8001 TCP | web panel (admin + My Phone) | your LAN (optionally My Phone via a tunnel, see section 16) |
| PBX | 8088, 8099, 4573 | Asterisk API, brain status, text handler | the PBX itself only (127.0.0.1) |
| Edge | 5061 TCP | SIP over TLS for outside phones | internet |
| Edge | 20000-20099 UDP | call audio (SRTP) | internet |
| Edge | 5060 UDP | SIP to and from the PBX | your LAN only |
| Edge | 22 TCP | SSH | your LAN only |

---

## 3. What you need

- **Two small Linux machines, VMs or containers running Debian 12 or 13**: one
  for the PBX, one for the edge. Proxmox LXC containers work well (1-2 CPU
  cores, 1-2 GB RAM, 8-16 GB disk each). The edge is only needed for phones
  outside the office.
  - In Proxmox, LXC containers need `nesting=1`: `pct set <ID> --features nesting=1`.
- **A SIP trunk** for calls to and from the phone network. **VoIP.ms is the
  only provider this has been tested with.** You need:
  server, username, password and at least one phone number (DID).
- **For phones away from home:** a domain name (e.g. `sip.example.com`),
  ideally on **Cloudflare** for automatic DNS updates and certificates, and a
  router where you can forward ports.
- **Optional:** an SMTP account for emails (voicemail, texts, security alerts),
  a Stripe account for paid plans, and Cloudflare Tunnel (or similar) to
  publish My Phone.

---

## 4. Install the PBX server

On the PBX machine, as root:

```bash
apt-get update && apt-get install -y unzip git
# copy the project here (git clone, or upload the zip and unzip it), then:
cd /root/own-pbx

# 1. One-time install: builds Asterisk 22 from source (10-20 minutes),
#    creates the pbx service user, folders and Python environments.
./scripts/install.sh

# 2. Deploy: database, Asterisk configs, services. Run this again after every update.
./scripts/deploy.sh
```

At the end of the first deploy you are asked to **create the admin login**
(at least 12 characters). Then open the panel from a computer on your network:

```
http://<PBX-IP>:8001
```

![Login](docs/screenshots/01-login.png)

Check that everything runs:

```bash
systemctl status asterisk pbx-brain pbx-api pbx-agi --no-pager
asterisk -rx "pjsip show endpoints"
./scripts/security-check.sh
```

Lost the admin password? On the PBX run
`/opt/pbx/api-venv/bin/python /root/own-pbx/scripts/create-admin.py`. It
resets the password of an existing login, or creates a new admin.

---

## 5. Install the edge server

Skip this section if all your phones are at home (on your own network).

**5.1 Create the machine.** For example, a Proxmox LXC on the host:

```bash
pct create 200 local:vztmpl/<debian-13-standard template>.tar.zst \
  --hostname kamailio-edge --cores 1 --memory 1024 --rootfs local-lvm:8 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.1.11/24,gw=192.168.1.1 \
  --features nesting=1 --unprivileged 1
pct start 200
pct enter 200      # then: passwd   (set a root password)
```

**5.2 Copy the `kamailio/` folder** to the edge box, e.g. `/root/kamailio`,
and install:

```bash
cd /root/kamailio
chmod +x *.sh pbx-edge-notify
PUBLIC_HOST=sip.example.com ASTERISK_IP=192.168.1.10 ./install.sh
```

(If you leave the two values out, the installer asks for them.) This installs:
- **Kamailio**: TLS-only on port 5061, with a flood limit.
- **rtpengine**: the audio relay that does the SRTP encryption.
- **fail2ban**: bans scanners and password guessers. Your own LAN is never banned.
- **A firewall**: nftables, default deny.
- **An IP-follow timer**: re-advertises your public IP every minute if it changes.

**5.3 Get a real certificate** (with your domain on Cloudflare):

```bash
CF_Token=<Cloudflare API token with Zone → DNS → Edit> \
  ./get-cert-cloudflare.sh sip.example.com you@example.com
```

The certificate renews itself.

**5.4 Connect security alerts to the PBX.** In the admin panel go to **API Keys**,
create a key called `edge`, then on the edge box:

```bash
pbx-edge-notify --setup http://192.168.1.10:8001 <the API key>
```

**5.5 Check everything:**

```bash
./verify-edge.sh
./security-check.sh
```

All checks should pass.

Later updates of the edge: copy the new `kamailio/` folder over and run
`./update-edge.sh`. It keeps your IPs and domain, and rolls back by itself if
the new config doesn't start.

---

## 6. Router, DNS and certificate

**Router port forwards** go to the **edge** server, never to the PBX:

| Protocol | External port | Internal IP | Internal port |
|---|---|---|---|
| TCP | 5061 | 192.168.1.11 (edge) | 5061 |
| UDP | 20000-20099 | 192.168.1.11 (edge) | 20000-20099 |

Don't forward 5060, 8001 or 22.

**DNS:** `sip.example.com` → your public IP, as a plain A record. On Cloudflare
it must be **DNS only (grey cloud)**: Cloudflare's proxy can't carry phone calls.

**Changing public IP?** Admin → **Network**: add the domain and a Cloudflare
API token and turn on automatic updates. The PBX updates the DNS record, and
the edge's timer updates the edge itself.

![Network](docs/screenshots/16-network.png)

---

## 7. First-time setup in the panel

Do these in order:

1. **Trunks** → add your provider (server, username, password, codecs
   `ulaw,alaw,g722`). Check with `asterisk -rx "pjsip show registrations"`,
   which should say *Registered*.
2. **Routes → Outbound**: an outbound route for your trunk with the patterns
   `1NXXNXXXXXX`, `NXXNXXXXXX` and `011.`.
3. **Logins** → create a login for each person or phone: panel username and
   password, extension number (e.g. 8800), SIP username (usually the
   extension) and a **SIP secret** (the password the phone uses - make it long).
   Users can also generate a new SIP password themselves in My Phone.
4. **Billing → User access**: give users outside minutes, texts, voicemail
   and so on (or set defaults for everyone). **Without this, users can only
   call other extensions**, and incoming calls from the trunk skip them.
5. **Ring groups** (optional) → create a group, e.g. "Front office".
6. **Routes → Inbound** → your phone number → *Send callers to* a ring
   group, an IVR menu or a list of extensions. Numbers work with or without
   the leading 1.
7. **Email** → SMTP server, so voicemails, texts and security alerts can be
   emailed. Set **Panel address for links in emails**.
8. **Email → Security alerts** → your address.
9. **E911** → set the E911 code, then the office fallback or per-user
   addresses (see section 12). Test each phone by dialing **933**.
10. **Network** → domain and Cloudflare dynamic DNS, if you use the edge.

---

## 8. Setting up phones

Every user can see their own phone settings in **My Phone → Settings → Phone setup**.

| Setting | In the office | Away from the office |
|---|---|---|
| Server / domain | PBX IP, e.g. `192.168.1.10` | `sip.example.com` |
| Port | 5060 | 5061 |
| Transport | UDP | **TLS** |
| Outbound proxy | (none) | `sip.example.com:5061` |
| Media encryption | off | **SRTP (SDES)** |
| Username / auth name | extension number, e.g. `8800` | same |
| Password | the extension's SIP password | same |
| Re-register | default | every 60-120 seconds |

**Zoiper on a mobile phone (away):** account → domain `sip.example.com`,
transport TLS, outbound proxy `sip.example.com:5061`. Under Advanced →
Encryption choose **SRTP (SDES)**, not ZRTP. Android and iOS can close a
background app's connection, so allow Zoiper to run in the background (or use
push), or incoming calls go to voicemail.

**Desk phones:** set Proxy, Registrar and Outbound proxy to the PBX IP, port
5060, transport UDP, and turn SRTP off on the LAN. If you use "dial 9 for an
outside line", update the phone's dial plan (e.g. `9xxxxxxxxxx`).

**Check it:** the phone appears under **Dashboard → Signed in now** as
*Online*, showing whether it's internal or external.

---

## 9. The admin panel, page by page

Admin pages only open **on your home network** (the panel calls it the "office network"): someone coming in through
the internet gets My Phone only, and the admin menu is hidden. That can be
changed under Network, but it isn't recommended.

### Dashboard
![Dashboard](docs/screenshots/02-dashboard.png)
- **Status bar**:
  - **Kill switch** hangs up every call and disconnects every phone. Use it
    to stop abuse such as toll fraud. Release it to go back to normal.
  - **Lock** turns on the safety lock: calls keep working, but nobody can
    change anything (handy overnight). Both always work and both send an alert.
- **Tiles**: live calls, devices online, sign-ins and failures in the last
  24 hours, IPs blocked in the last 24 hours.
- **Live calls**: who is calling whom, by name, with type
  (internal/incoming/outgoing/911), state and duration.
- **Signed in now**: every registered phone, its IP, internal or external,
  connection (direct, or through the edge with TLS + SRTP), status and
  response time.
- **Recent sign-ins** and **Activity**: security events and changes from
  the last 24 hours. Updates every 10 seconds.

### Logins
![Logins](docs/screenshots/03-logins.png) ![Edit login](docs/screenshots/04-login-edit.png)
Users are extensions: username, panel password, extension number, SIP
username and password, display name, notification email, admin recording
(see section 13) and enabled. Disabling a login signs it out everywhere at once.

### Trunks
![Trunks](docs/screenshots/05-trunks.png)
Your SIP provider(s): registrar, username, secret and codecs.

### Routes
![Routes](docs/screenshots/06-routes.png)
- **Inbound**: phone number → ring group, IVR menu or list of extensions.
  One route covers the number with or without the leading 1.
- **Outbound**: number patterns → trunk.
- **Outside line prefix**: *Off* (default), *Optional* (9 + number and the
  number alone both work) or *Required*, with the digit 9 or 8. The prefix is
  removed before dialing out; 911 never needs it.

### Ring groups
![Ring groups](docs/screenshots/07-ring-groups.png) ![Edit ring group](docs/screenshots/08-ring-group-edit.png)
- **Name** and an optional **internal number** that phones can dial.
- **Members**, all ringing at once. Their order sets the "first member".
- **Ring time**.
- **If nobody answers**:
  - one member's voicemail;
  - a copy for every member;
  - another extension, ring group, IVR menu, or an outside number (this uses
    the first member's minutes);
  - hang up.

Calls to the group's internal number aren't billed.

### IVR
![IVR](docs/screenshots/09-ivr.png)
Company menus with a greeting (upload WAV/MP3) and key options: ring an
extension, ring several extensions, voicemail, another menu, repeat, or hang
up. Also controls how many personal menus users may build.

### CDR, Voicemail, Recordings, 
![CDR](docs/screenshots/10-cdr.png)
Call records (kept 90 days), every voicemail, every recording and every
text, with select / delete all.
![Voicemail (admin)](docs/screenshots/11-voicemail-admin.png)
![ (admin)](docs/screenshots/13-messages-admin.png)
![Recordings](docs/screenshots/12-recordings.png)
The **Recordings** page also holds the **recording settings** (see section 13).

### Billing
![Billing](docs/screenshots/14-billing.png)
Stripe keys, plans and **User access** (defaults for users without a plan,
plus per-user exceptions). See section 14.

### Email
![Email](docs/screenshots/15-email.png)
SMTP settings, a test email, the panel address used in email links, and
**Security alerts**: who gets them, and for what (blocked IPs, sign-in
lockouts, kill switch / lock / E911 / recording changes, user reports). Every
911 call is always emailed.

### Network
Remote-access domain and TLS port, office server address, Cloudflare dynamic
DNS (status, IP history), and panel access from the internet.

### E911
![E911](docs/screenshots/17-e911.png)
See section 12.

### Reports
![Reports](docs/screenshots/18-reports.png)
Calls and texts that users reported from My Phone. Mark them resolved,
reopen or delete them. To stop someone, disable their login.

### API Keys
![API keys](docs/screenshots/19-api-keys.png)
Bearer tokens for the REST API (`docs/API.md`) and for the edge's security
alerts. A key is shown only once; revoke it any time.

### Branding
White-label the panel: site name, header logo (upload or URL, shown as text,
logo, or both) with an optional separate dark-mode logo that auto-swaps with
the theme, header and button colors with one-click presets (Ocean, Sunset,
Royal…), an animated gradient header option, default dark/light theme
(visitors can flip it with the 🌙/☀️ icon; their choice sticks in the
browser), login page logo/title/subtitle (also with a dark-mode variant),
favicon, and an optional footer line. Logos are stored in the database, so
they ride along with backups.

**Regional:** time zone selector (defaults to Los Angeles); all call,
message, voicemail and recording times display in 12-hour format in that
zone.

---

## 10. My Phone: the user panel

Every user signs in at `http://<PBX-IP>:8001` (or your published address,
see section 16) with their panel username and password.

| Page | What it does |
|---|---|
| **Overview** | phone status, Do Not Disturb, new voicemail, missed calls, where 911 sends help, plan usage, recent calls |
| **Calls** | 90-day history (can't be deleted), recordings, **Block** and **Report** for each call |
| **Voicemail** | listen, download, move, delete; greeting upload or record by phone (`*98`) |
| **Recordings** | the user's own recordings |
| **Messages** | conversations, send a text to any extension even Zoiper/supported apps, hey maybe even an ESP? i have calls working on ESP see my [ESP32-S3-Box-3 project](https://github.com/DanversKara/ESP32-S3-Box-3)
, so im sure you can program on screen messages., **Block**, **Report**; updates by itself |
| **IVR** | personal menus (if their plan allows) |
| **Billing** | choose, switch or cancel a plan (Stripe) |
| **Settings** | DND, forwarding, ring time, answer with IVR, "Record my calls" (with legal acknowledgement), notification email, text emails on/off, receive/send texts on/off, block list, 911 notice, phone setup, SIP password, panel password |

![Overview](docs/screenshots/20-myphone-overview.png)
![Calls](docs/screenshots/21-myphone-calls.png)
![Voicemail](docs/screenshots/22-myphone-voicemail.png)
![Recordings](docs/screenshots/23-myphone-recordings.png)
![Messages](docs/screenshots/24-myphone-messages.png)
![IVR](docs/screenshots/25-myphone-ivr.png)
![Billing](docs/screenshots/26-myphone-billing.png)
![Settings](docs/screenshots/27-myphone-settings.png)
<img src="docs/screenshots/28-myphone-mobile.png" width="300" alt="My Phone on a phone">

---

## 11. Phone feature codes

| Dial | What it does |
|---|---|
| `911` (also `9911`/prefix + 911) | Emergency call. Always connects (see section 12). |
| `933` | Address test line at many providers: reads back your E911 address |
| `*97` | Check your voicemail |
| `*98` | Record your voicemail greeting |
| `*60` | Join the conference room |
| `*555<ext>` | Monitor an extension's calls: stays on the line, auto-follows new calls (admins only by default) |
| `*555` | Scan mode: listen to any active call on the system |
| `*70` | DISA: outside dial tone after a PIN (only if a PIN has been configured) |
| ring group or IVR number | Ring that group or menu |

While monitoring (`*555`), press `*` / `1` for the previous call and `#` / `2`
for the next call. A beep confirms each hop. Hang up to stop monitoring.

The **Features** page in the admin panel controls every star code: turn each
one on/off for the whole system, set the default access (**All users**,
**Admins only**, or **No one**), and override per user with Allow/Deny. For
example, you can give `*555` (ChanSpy) to a supervisor without making them a
full admin, or disable `*70` (DISA) entirely.

---

## 12. 911 / E911

> ⚠️ **NOT TESTED.** The 911/E911 features below have **not** been tested end
> to end. Do not rely on own-pbx for emergency calls. Keep a mobile phone or
> landline available, and if you use these features, test them yourself
> (e.g. with your provider's 933 test line) and confirm the address with your provider.


**911 always connects.** US 911 rules (Kari's Law and RAY BAUM's Act) don't
allow a phone system to block 911. Every phone can dial 911, including users
without E911, users with no plan, phones on DND, phones outside the office,
and while the safety lock is on.

**The address 911 gets comes from your provider, not this panel.** It's the
address registered there for the caller-ID number the call is sent with:
- **A user with E911 on** sends their own 911 number (whose address you
  registered at the provider).
- **Everyone else** uses the **office fallback**. Either use your provider's
  own E911 (the trunk's default caller ID) or set an office 911 number.

**The E911 page is locked** with a separate 6-12 digit code. Unlock it for 10
minutes to make changes. Wrong codes are limited and alerted, and every change
is logged and emailed.

**Every 911 call** shows on the dashboard and is emailed straight away, with
who called, which address was used, and whether the phone was outside the
office.

**Users are warned in My Phone** which address 911 will send help to, and
that it doesn't follow the phone.

**Test every phone by dialing 933** if your provider offers it.

Lost E911 code: `sqlite3 /var/lib/pbx/pbx.db "DELETE FROM kv_settings WHERE key='e911_code_hash'"`,
then set a new one.

*This system helps you route 911 correctly but you are responsible for
registering accurate addresses with your provider and for meeting the 911
rules that apply to you. This is not legal advice.*

---

## 13. Call recording and the law

Recording calls without everyone's consent is **illegal in some places**
("two-party" or all-party consent states such as California, and other
countries), and can break workplace or union rules.

- **Admin call recording** is **off for the whole system by default**.
  Turning it on (Recordings → Recording settings) needs the admin to tick an
  acknowledgement, which is logged. When on, only extensions with *Admin call
  recording* ticked on the Logins page are recorded. These recordings are
  silent to the callers unless the announcement is on.
- **"Record my calls"** (My Phone → Settings) needs recording in the user's
  plan, plus the user's own legal acknowledgement.
- **"This call may be recorded" announcement**: plays to both people when a
  recorded call starts. Upload your own message, or a beep plays instead.

![Recording settings](docs/screenshots/12-recordings.png)

---

## 14. Plans and billing (Stripe)

> Optional, and **not tested with real payments** - only use Stripe's test mode
> unless you have tested it yourself. Most home users don't need billing at
> all: just give people access under Billing → User access.


**What a plan sets:** monthly outside minutes, voicemail, texts per month,
recording, and the number of IVR menus.

**No plan:** users can only call other extensions. Admins are never limited.

**Setting it up:**
1. In Stripe (start in **test mode**), copy the secret key and create a
   webhook to `https://<your-public-panel>/stripe/webhook`. Copy the signing
   secret.
2. Admin → **Billing**: paste the keys, set the **public URL**, and create
   plans (name, price, limits). Then **Publish** each plan to Stripe.
3. Users subscribe, switch and cancel in **My Phone → Billing**. Stripe's own
   page handles payment details.

**Overriding plans:** Billing → **User access** sets defaults for users
without a plan, and per-user exceptions such as `unlimited` minutes for a
lobby phone.

**Without Stripe:** just use User access to give people what they need.

---

## 15. Security

**Network design:** only the edge faces the internet, and only on TLS port
5061 plus the audio ports. The PBX has no ports forwarded to it at all.

**Edge server:**
- TLS only.
- Known scanners dropped on sight.
- Flood limit: more than about 30 requests in 2 s gets an IP banned for 24 hours.
- fail2ban: 8 failed sign-ins or junk requests in 10 minutes gets an IP banned
  for 1 hour. The office LAN is never banned.
- Every ban is shown on the dashboard and emailed.

**Panel:**
- **Passwords and sessions:** bcrypt passwords. Sessions expire after 3 hours
  idle or 12 hours in total, and end immediately when a user is disabled,
  demoted or changes password.
- **Sign-in lockouts:** 10 failures per address, or 20 per username, in 15
  minutes locks that address or username for 15 minutes.
- **Admin pages:** home network only.
- **Page protection:** CSRF tokens on every form, security headers that stop
  framing and clickjacking, and a 25 MB upload limit.

**Server hardening:**
- Code, Python environments and helper scripts are root-owned, and the
  service account can't change them.
- The database is readable by the PBX services only (mode 600).

**Emergency tools:** the kill switch, the safety lock, and per-user block
lists and reports.

**Check regularly:**
- `scripts/security-check.sh` on both servers. It reports pending security
  updates, open ports, SSH settings, permissions and vulnerable Python
  packages.
- Keep Debian, Asterisk (latest 22.x) and Kamailio updated:
  `apt-get update && apt-get upgrade`.
- Scan your public IP from outside (e.g. an online port scanner on mobile
  data). Only TCP 5061 should be open.
- Turn off SSH root password logins once setup is done.

---

## 16. Opening My Phone to the internet (optional)

To let users reach My Phone from anywhere, publish the panel through
**Cloudflare Tunnel** (optionally behind Nginx Proxy Manager), e.g.
`https://myphone.example.com` → `http://<PBX-IP>:8001`. Then:

- Anything coming through the tunnel or proxy is treated as **internet**:
  users get My Phone, and admin pages answer "office network only" with the
  admin menu hidden. Admins keep using `http://<PBX-IP>:8001` at the office.
- Sign-in lockouts and logs use each visitor's real IP.
- Turn on Cloudflare's **Always Use HTTPS**.
- Never forward port 8001 (or NPM's 80/443) directly from the router.
- Use a different name from the SIP domain: `sip.example.com` stays a
  grey-cloud A record.

---

## 17. Updating

**PBX:** replace `/root/own-pbx` with the new version, then run
`./scripts/deploy.sh`. Database changes are applied automatically, and your
data and settings stay.

**Edge:** copy the new `kamailio/` folder to `/root/kamailio`, then run
`./update-edge.sh`.

---

## 18. Maintenance and housekeeping

| Data | Kept for |
|---|---|
| Sign-in log | 24 hours |
| Security events in the Activity feed | 24 hours (the change log itself is kept, including recording-consent records) |
| Call history | 90 days (users can't delete it) |
| Voicemail, recordings, texts | until deleted |

**Backups:** back up `/var/lib/pbx/` (database, greetings), `/var/spool/pbx/`
(voicemail, recordings) and `/etc/pbx/`.

**Logs:**
- `journalctl -u pbx-brain -u pbx-api -u pbx-agi`
- `asterisk -rvvv`
- On the edge: `journalctl -u kamailio -u rtpengine -u fail2ban`

---

## 19. Troubleshooting

| Problem | Check |
|---|---|
| Phone won't register | **Dashboard → Recent sign-ins** and **Activity**; `asterisk -rx "pjsip show endpoints"`; on the edge `fail2ban-client status kamailio-edge` (unban with `fail2ban-client unban <ip>`) |
| Outside calls fail | User has outside minutes (Billing → User access)? Outbound route matches? `asterisk -rx "pjsip show registrations"` says *Registered*? `journalctl -u pbx-brain` shows `outbound ... via trunk`? |
| Incoming calls skip someone | They need outside minutes; check `journalctl -u pbx-brain \| grep skipped` |
| Remote phone: no audio | Router forwards UDP 20000-20099 to the **edge**; the phone uses SRTP (SDES); `./verify-edge.sh` |
| Remote phone: "SDP mismatch" when answering | Set the phone's media encryption to **SRTP (SDES)** |
| Remote phone never rings | Zoiper killed in the background (phone settings / push); the edge config is current (`./update-edge.sh`) |
| Hang-up doesn't end the call on the other side | The edge config is current; the certificate matches `sip.example.com` |
| Calls drop after an IP change | `systemctl status pbx-edge-ip.timer` on the edge; Network page shows "In sync" |
| No emails | Email page → **Send test**; port 587 or 465 (many ISPs block 25) |
| Can't open admin pages | Use `http://<PBX-IP>:8001` from the office network, not the published address |

---

## 20. Files and folders

```
pbx-api/        web panel + REST API (FastAPI)
  app.py          admin pages, auth, sessions, dashboard, v1 API,
                  voip.ms SMS webhook + inbound poller, /sms-routes admin UI
  voipms_sms.py   voip.ms helper: sendSMS/sendMMS, getSMS/getMMS polling,
                  DID normalization, route lookups
  ucp.py          My Phone
  ring_groups_ui.py, ivr_ui.py, billing.py, e911_ui.py, email_ui.py,
  network_ui.py, safety_ui.py, security.py, ddns.py, entitlements.py, mailer.py
pbx-brain/      call engine (ARI) - app.py, e911.py, agi_server.py (texts,
                incl. external SMS/MMS via voip.ms API), voipms_sms.py (shared helper)
config-gen/     renders Asterisk pjsip.conf from the database; static Asterisk configs
db/             schema.sql (applied on every deploy), prod-seed.sql (example)
scripts/        install.sh, deploy.sh, create-admin.py, security-check.sh,
                systemd units, privileged helper scripts
kamailio/       edge server: install.sh, update-edge.sh, kamailio.cfg.template,
                rtpengine.conf.template, fail2ban/, certificate + IP scripts,
                verify-edge.sh, pbx-edge-notify
dev/            developer and test tools (not needed to run own-pbx)
docs/           API.md (REST API), SCREENSHOTS.md (all screenshots), screenshots/,
                VOIPMS_SMS_SETUP.md (voip.ms SMS/MMS setup and troubleshooting)
```

**On the PBX:**
- Code: `/opt/pbx`
- Data: `/var/lib/pbx` (database `pbx.db`, greetings)
- Voicemail and recordings: `/var/spool/pbx`
- ARI password: `/etc/pbx/ari.pass`

**On the edge:**
- `/etc/kamailio`
- `/etc/rtpengine`
- `/etc/fail2ban/jail.d`
- `/etc/pbx-edge/notify.env`

---

## 21. SMS/MMS with voip.ms

Your voip.ms DIDs can send and receive real SMS/MMS, routed to your
extensions. Each DID maps to one extension: inbound texts go to that
extension's Zoiper (SIP MESSAGE), My Phone conversation, and any ESP/HA
device polling the inbox. Replies and new outbound texts go out through the
voip.ms API from the mapped DID.

### How it works

**Inbound — two paths, no lost messages:**
- **URL callback (instant):** voip.ms fires a GET/POST to
  `https://<your-panel>/hooks/voipms-sms` with `to, from, message, id, date`.
  The PBX checks the token, dedupes by message ID, looks up the DID route,
  stores the message, and pushes it to Zoiper via SIP MESSAGE.
- **Poller (safety net, every 60s):** callbacks can misfire (tunnel hiccup,
  timeout). A background thread polls voip.ms `getSMS`/`getMMS` per routed
  DID and ingests anything the webhook missed. Same dedupe table, so nothing
  is ever delivered twice. Webhook = instant, poll = source of truth.

**Outbound:**
- **Zoiper:** text `+15551234567` (or `15551234567`) as a SIP MESSAGE
  destination. The `[pbx-msg]` dialplan hands it to the AGI, which detects
  the external number and sends it via voip.ms `sendSMS` from the DID mapped
  to your extension. The API call runs in the background, so Zoiper gets its
  reply immediately even if voip.ms is slow.
- **My Phone → Messages:** type an external number, same path.
- **ESP/HA:** `POST http://<pbx>:8099/messages/send` with
  `{"to": "+1555...", "from": "8801", "body": "..."}` — external numbers
  route out through voip.ms automatically; `GET /messages/inbox?ext=8801`
  reads the thread.

**MMS:** voip.ms callbacks carry no media, so inbound MMS is enriched via the
`getMMS` API (webhook) or picked up by the poller. Pictures arrive as
`[MMS: <link>]` entries — tappable in Zoiper, My Phone and ESP.

**Sender DID selection** (outbound): replies go out from the DID the
conversation is already on (each message records its DID), so a thread stays
on one number even when several DIDs route to the same extension. For a
brand-new conversation: the DID whose route points at your extension, else
the configured default DID, else the first route.

### Setup

1. **voip.ms portal → Main Menu → SOAP and REST/JSON API:** enable the API
   and set an **API password** (separate from your portal login password).
   The API username is your **portal login email** — not the account number.
2. **DID Numbers → Manage DIDs → edit each DID → Message Service (SMS/MMS):**
   enable it, paste the webhook URL from the PBX's **SMS** page
   (`https://<panel>/hooks/voipms-sms?token=...&to={TO}&from={FROM}&message={MESSAGE}&id={ID}&date={TIMESTAMP}`),
   choose E.164 number format, turn on callback retry, Apply.
3. **PBX → SMS page:** add one route per DID (DID → extension), fill in the
   API username/password. The page shows the exact webhook URL to paste.
4. Test: text the DID from your mobile → Zoiper should pop within seconds.
   Reply from Zoiper → your mobile gets it from the DID.

Details, troubleshooting and the ESP API shapes: `docs/VOIPMS_SMS_SETUP.md`.

## 22. License

own-pbx is free software under the **GNU General Public License, version 2 or
(at your option) any later version** (`LICENSE`). Asterisk, Kamailio,
rtpengine and the other programs it uses are installed separately and keep
their own licenses. See `THIRD_PARTY.md`.

own-pbx is a **home / hobby project**, tested only with the VoIP.ms trunk, and
its **911/E911 features have not been tested**. It comes with **no warranty**
of any kind (see `LICENSE`, sections 11-12). It is not intended for business,
commercial or life-safety use. You are responsible for how you use it,
including 911, your provider's terms, call-recording consent and the laws
that apply where you live.
