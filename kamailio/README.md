# Kamailio edge proxy for own-pbx

> The complete install guide is in the main `README.md` (section 5). This file is the
> edge server's detailed reference.

Remote phones (Zoiper) without exposing the PBX and without a VPN —
with encrypted signaling (TLS) **and** encrypted audio (SRTP).

## Architecture

```
Remote phone --TLS 5061--> Kamailio --UDP 5060--> Asterisk (192.168.1.10, private)
RTP: phone <-SRTP, UDP 20000-20099-> rtpengine (same box) <-plain RTP, LAN-> Asterisk
```

- **Kamailio** terminates TLS, proxies SIP to Asterisk, pins a `Path` header on
  REGISTER so inbound calls find the phone.
- **Asterisk stays the registrar and the authenticator.** Extensions are still
  100% API-managed ("APIs are the ext") — no credential sync, no second user DB.
- **rtpengine** relays audio so NAT "just works", and terminates **SRTP (SDES)**
  on the internet leg. The LAN leg to Asterisk stays plain RTP (trusted network,
  zero Asterisk changes needed).
- Public surface is **one TLS port**. SIP scanners are UDP-based, so TLS-only
  kills ~99% of drive-by scanning before fail2ban even gets involved.

## Deploy

### 1. Create the LXC (Proxmox host)

```bash
pct create 200 local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst \
  --hostname kamailio-edge --cores 2 --memory 2048 --swap 512 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.1.11/24,gw=192.168.1.1 \
  --storage local-lvm --rootfs 8
pct start 200
```

(Any free CT ID and IP on your LAN works — adjust the commands below.)

### 2. Install

Copy this `kamailio/` directory to the LXC (FTP it like you do), then:

```bash
cd /root/kamailio
chmod +x install.sh update-public-ip.sh
PUBLIC_HOST=sip.example.com ./install.sh
```

Takes ~10–15 min (rtpengine builds from source). `PUBLIC_HOST` is the public
hostname in the TLS certificate — use whatever DNS name you'll point at it.

### 3. Router port forwards

| Public port      | → LXC                    |
|------------------|--------------------------|
| TCP 5061         | 192.168.1.11:5061 (SIP/TLS) |
| UDP 20000-20099  | 192.168.1.11 (rtpengine RTP) |

Nothing goes to the PBX (192.168.1.10). It stays fully private.

### 4. DNS

`sip.example.com` → your public IP (or just use the raw public IP in Zoiper).

### 5. PBX side — re-render pjsip

The template now sets `support_path=yes` on phone AORs (needed for the
Path header). On the PBX LXC:

```bash
cd /root/own-pbx && ./scripts/deploy.sh
```

This re-renders `pjsip.conf` and reloads. Phones re-register on their own.

### 6. Zoiper (remote)

- **Domain:** `sip.example.com` (or your public IP)
- **Username:** `phone8800`
- **Password:** the SIP secret
- **Outbound proxy:** enabled → `sip.example.com`, port `5061`, transport **TLS**
- **SRTP:** enabled (Zoiper account → Advanced/Encryption → SRTP). Without it
  the call still connects, but that audio leg is unencrypted.

First connect shows a certificate warning (self-signed) — accept it, or see
below for a real cert. LAN phones keep working exactly as before; nothing
changes for them.

## Real certificate (optional, recommended)

Replace the self-signed pair anytime — e.g. via NPMplus or acme.sh DNS-01 —
then:

```bash
cp fullchain.pem /etc/kamailio/tls/cert.pem
cp privkey.pem   /etc/kamailio/tls/key.pem
chmod 600 /etc/kamailio/tls/key.pem
systemctl restart kamailio
```

## If your public IP changes (dynamic IP)

Two things follow the IP automatically:

- **DNS** — PBX panel → **Network**: SIP domain + Cloudflare API token
  (Zone → DNS → Edit). The PBX checks every 1–15 min and updates the A record
  (always *DNS only*, TTL 60 s; Cloudflare's proxy can't carry SIP/RTP).
- **This edge box** — `pbx-edge-ip.timer` (installed by install.sh, or
  `./update-public-ip.sh --install-timer`) checks every minute and updates
  Kamailio's advertised address and rtpengine's SDP address, then restarts
  both. Without it, calls connect after an IP change but have no audio.

Manual run: `/root/kamailio/update-public-ip.sh` (or pass the IP).
Logs: `journalctl -u pbx-edge-ip`.

Expected downtime after a change: up to ~1 min (detection) + DNS TTL (60 s) +
the phone's re-register interval, so set phones to re-register every 60–120 s.

## Real certificate via Cloudflare (recommended for a domain)

```bash
cd /root/kamailio
CF_Token=<token> ./get-cert-cloudflare.sh sip.example.com you@example.com
```

Uses Let's Encrypt with the Cloudflare DNS challenge (no port 80 needed),
installs to /etc/kamailio/tls/, adds the domain as a Kamailio alias, and
renews automatically. Many softphones refuse self-signed or mismatched
certificates, so do this when you switch to a domain.

## Verify

One command covers it on the edge LXC:

```bash
cd /root/kamailio && PUBLIC_HOST=sip.example.com ./verify-edge.sh
```

Checks services, kamailio.cfg syntax, TLS listener + handshake on 5061, cert
hostname/expiry, rtpengine media ports, nftables rules, fail2ban jail, the UDP
path to Asterisk, and whether your public IP still matches the advertised one.
Green means go. Manual checks still fine too:

```bash
# on the edge LXC
systemctl is-active kamailio rtpengine fail2ban
kamailio -c -f /etc/kamailio/kamailio.cfg   # config ok
tail -f /var/log/syslog | grep kamailio     # watch registrations

# on the PBX
asterisk -rx 'pjsip show contacts'          # phone contact with Path
```

Register Zoiper remotely, call 8800→8801 both ways, confirm two-way audio.

## Troubleshooting

- **Phone won't register:** `tail -f /var/log/syslog | grep kamailio` on the
  edge box — look for TLS handshake errors (cert) or forwards to Asterisk.
  Check the router forward for TCP 5061.
- **Registers but no audio:** UDP 20000-20099 not forwarded, or the public IP
  changed (run `update-public-ip.sh`).
- **Inbound calls don't reach the phone:** the REGISTER must carry a Path
  header — check `pjsip show contacts` on the PBX shows the contact, and that
  `support_path=yes` is on the AOR (re-run deploy.sh).
- **fail2ban banning you:** `fail2ban-client status kamailio-edge` and
  `fail2ban-client unban <ip>`.

## Files

| File | Purpose |
|---|---|
| `kamailio.cfg.template` | Main proxy config (syntax-checked against Kamailio 5.7; targets 6.x) |
| `tls.cfg` | TLS server profile |
| `rtpengine.conf.template` | Media relay config |
| `install.sh` | Full LXC installer |
| `update-public-ip.sh` | Re-point at a new public IP |
| `fail2ban/` | Filter + jail for scanners/brute-force |

## Security: flood limit, fail2ban and alerts

- **Flood limit (pike):** more than ~30 requests in 2 seconds from one IP is
  dropped and logged once as `edge: flood from <ip>`.
- **Failed sign-ins:** the edge logs `edge: auth failed for <method> user
  '<name>' from <ip>` only when a phone sent a password and was refused (the
  normal first 401 challenge is not counted; stale nonces are ignored).
- **fail2ban jails:** `kamailio-edge` (scanners, failed sign-ins, junk: 8 in
  10 min -> 1 hour ban) and `kamailio-edge-flood` (1 flood -> 24 hour ban).
  `fail2ban-client status kamailio-edge` lists banned IPs;
  `fail2ban-client unban <ip>` lifts a ban.
- **Alerts:** each ban/unban is sent to the PBX, which shows it in the
  dashboard Activity feed and emails the address set under Email -> Security
  alerts. One-time setup with an admin API key from the panel's API keys page:
  `pbx-edge-notify --setup http://<pbx-ip>:8001 <API key>`

Existing edge boxes: `cd /root/kamailio && ./update-edge.sh` re-renders
kamailio.cfg from the new template (keeps IPs and aliases, rolls back if it
won't start) and installs the fail2ban files and alert hook.
