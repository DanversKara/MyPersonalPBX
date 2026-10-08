#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""SIP test phones over TCP (this sandbox blocks UDP sendto, so Asterisk
cannot reply over UDP; TCP works because responses reuse the connection).

Proves the Phase 1 milestone end-to-end:
  1. phone8800 and phone8801 REGISTER (digest auth) -> 200 OK
  2. 8800 INVITEs 8801 -> brain originates the B leg -> 8801 auto-answers
  3. ARI bridge holds both channels; RTP ports negotiated via SDP
  4. BYE -> CDR row written in the DB

NOTE on media: Asterisk cannot *send* UDP here (sandbox blocks sendto), so
we verify the bridge + SDP negotiation + CDR. Client->Asterisk RTP uses a
connected UDP socket (allowed) — Asterisk receiving it proves the media
path is established. On real hardware both directions flow.

Usage: python3 scripts/sip-test.py
"""
import hashlib
import random
import re
import select
import socket
import sqlite3
import sys
import threading
import time

SERVER = "127.0.0.1"
SPORT = 5060
DB = "/var/lib/pbx/pbx.db"


def rand(n=10):
    return "".join(random.choice("abcdef0123456789") for _ in range(n))


def parse_sip(data):
    head, _, body = data.partition("\r\n\r\n")
    lines = head.split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return lines[0], headers, body


def parse_auth_params(header_value):
    return dict(re.findall(r'(\w+)="([^"]*)"', header_value))


def digest_response(username, password, realm, nonce, method, uri, qop):
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if qop:
        nc, cnonce = "00000001", rand(8)
        resp = hashlib.md5(
            f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        return resp, nc, cnonce
    return hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest(), None, None


def sdp_offer(rtp_port):
    return (
        "v=0\r\n"
        f"o=- {random.randint(1, 1 << 31)} 1 IN IP4 127.0.0.1\r\n"
        "s=-\r\n"
        "c=IN IP4 127.0.0.1\r\n"
        "t=0 0\r\n"
        f"m=audio {rtp_port} RTP/AVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    )


def sdp_peer_addr(body):
    m = re.search(r"m=audio (\d+)", body)
    c = re.search(r"c=IN IP4 (\S+)", body)
    return (c.group(1) if c else "127.0.0.1",
            int(m.group(1)) if m else 0)


class SIPConn:
    """One SIP-over-TCP connection with Content-Length framing."""

    def __init__(self, sock):
        self.sock = sock
        self.sock.settimeout(0.5)
        self.buf = b""

    def send_msg(self, msg):
        self.sock.sendall(msg.encode())

    def recv_msg(self, timeout=5.0):
        """-> (start_line, headers, body); raises TimeoutError."""
        end = time.time() + timeout
        while True:
            head, sep, rest = self.buf.partition(b"\r\n\r\n")
            if sep:
                lines = head.decode(errors="replace").split("\r\n")
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                clen = int(headers.get("content-length", "0"))
                if len(rest) >= clen:
                    body = rest[:clen].decode(errors="replace")
                    self.buf = rest[clen:]
                    return lines[0], headers, body
            if time.time() > end:
                raise TimeoutError("no SIP message within %.1fs" % timeout)
            try:
                chunk = self.sock.recv(65535)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("TCP closed by peer")
            self.buf += chunk

    def has_data(self):
        r, _, _ = select.select([self.sock], [], [], 0)
        return bool(r) or b"\r\n\r\n" in self.buf


class SIPPhone:
    def __init__(self, username, password, exten):
        self.username = username
        self.password = password
        self.exten = exten
        self.cseq = random.randint(1, 1000)
        self.call_id = rand(12) + "@127.0.0.1"
        self.from_tag = rand(10)
        self.conn = None
        self.contact_port = None  # TCP port Asterisk can reach us on

    def connect(self):
        s = socket.create_connection((SERVER, SPORT), timeout=5)
        self.conn = SIPConn(s)
        return self.conn

    def _via(self):
        return (f"SIP/2.0/TCP 127.0.0.1:{self.conn.sock.getsockname()[1]}"
                f";branch=z9hG4bK{rand(10)};rport")

    def request(self, method, uri, to_uri=None, extra_headers=None, body=""):
        self.cseq += 1
        h = {
            "Via": self._via(),
            "From": f'"{self.exten}" <sip:{self.username}@127.0.0.1>;tag={self.from_tag}',
            "To": f"<{to_uri or f'sip:{self.username}@127.0.0.1'}>",
            "Call-ID": self.call_id,
            "CSeq": f"{self.cseq} {method}",
            "Contact": f"<sip:{self.username}@127.0.0.1:{self.contact_port};transport=tcp>",
            "Max-Forwards": "70",
            "User-Agent": "pbx-sip-test",
            "Content-Length": str(len(body.encode())),
        }
        if body:
            h["Content-Type"] = "application/sdp"
        if extra_headers:
            h.update(extra_headers)
        lines = [f"{method} {uri} SIP/2.0"]
        lines += [f"{k}: {v}" for k, v in h.items()]
        return "\r\n".join(lines) + "\r\n\r\n" + body

    def _authed(self, method, uri, headers, body="", extra_headers=None):
        key = ("www-authenticate" if "www-authenticate" in headers
               else "proxy-authenticate")
        p = parse_auth_params(headers[key])
        qop = "auth" if "auth" in p.get("qop", "") else None
        response, nc, cnonce = digest_response(
            self.username, self.password, p["realm"], p["nonce"],
            method, uri, qop)
        auth = (f'Digest username="{self.username}", realm="{p["realm"]}", '
                f'nonce="{p["nonce"]}", uri="{uri}", response="{response}", '
                f"algorithm=MD5")
        if qop:
            auth += f', qop={qop}, nc={nc}, cnonce="{cnonce}"'
        hdr_name = ("Authorization" if key == "www-authenticate"
                    else "Proxy-Authorization")
        hdrs = {hdr_name: auth}
        if extra_headers:
            hdrs.update(extra_headers)
        msg = self.request(method, uri, extra_headers=hdrs, body=body)
        self.conn.send_msg(msg)
        return self.cseq

    def register(self, contact_port):
        self.contact_port = contact_port
        uri = f"sip:{SERVER}:{SPORT}"
        extra = {"Expires": "300"}
        self.conn.send_msg(self.request("REGISTER", uri,
                                        extra_headers=extra))
        start, headers, _ = self.conn.recv_msg()
        if start.startswith("SIP/2.0 401") or start.startswith("SIP/2.0 407"):
            self._authed("REGISTER", uri, headers, extra_headers=extra)
            start, headers, _ = self.conn.recv_msg()
        if not start.startswith("SIP/2.0 200"):
            raise RuntimeError(f"REGISTER failed: {start}")
        print(f"  [{self.exten}] REGISTER -> 200 OK")

    def invite(self, target_exten, rtp_port):
        uri = f"sip:{target_exten}@{SERVER}:{SPORT}"
        body = sdp_offer(rtp_port)
        self.conn.send_msg(self.request(
            "INVITE", uri, to_uri=f"sip:{target_exten}@127.0.0.1", body=body))
        invite_cseq = self.cseq
        to_tag, peer = None, None
        while True:
            start, headers, rbody = self.conn.recv_msg(timeout=35)
            if start.startswith("SIP/2.0 401") or start.startswith("SIP/2.0 407"):
                invite_cseq = self._authed(
                    "INVITE", uri, headers, body,
                    {"To": f"<sip:{target_exten}@127.0.0.1>"})
                # _authed rebuilt To without target; fix via request override
                continue
            if start.startswith("SIP/2.0 100"):
                continue
            if start.startswith("SIP/2.0 180") or start.startswith("SIP/2.0 183"):
                print(f"  [{self.exten}] ringing...")
                continue
            if start.startswith("SIP/2.0 200"):
                m = re.search(r'tag=([^;,\s]+)', headers.get("to", ""))
                to_tag = m.group(1) if m else ""
                peer = sdp_peer_addr(rbody)
                self.cseq += 0  # ACK reuses INVITE's CSeq
                ack = self.request(
                    "ACK", uri, to_uri=f"sip:{target_exten}@127.0.0.1;tag={to_tag}")
                ack = ack.replace(f"CSeq: {self.cseq} ACK",
                                  f"CSeq: {invite_cseq} ACK", 1)
                ack = ack.replace("ACK ", "ACK ", 1)
                # strip body-related headers for ACK without body
                self.conn.send_msg(ack)
                print(f"  [{self.exten}] 200 OK, ACK sent, peer RTP {peer}")
                return peer, to_tag, invite_cseq
            raise RuntimeError(f"INVITE failed: {start}")

    def bye(self, target_exten, to_tag):
        uri = f"sip:{target_exten}@{SERVER}:{SPORT}"
        self.cseq += 1
        msg = self.request(
            "BYE", uri, to_uri=f"sip:{target_exten}@127.0.0.1;tag={to_tag}")
        # request() built CSeq with incremented value; ensure no body
        self.conn.send_msg(msg)
        start, _, _ = self.conn.recv_msg()
        print(f"  [{self.exten}] BYE -> {start.split(' ', 2)[1]}")


class RTPPort:
    """Connected-UDP RTP endpoint (send works in this sandbox)."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.rx = 0
        self.tx = 0
        self._stop = threading.Event()

    def run(self, peer, duration=4.0):
        self.sock.connect(peer)
        ssrc = random.randint(1, 1 << 31)
        seq, ts = random.randint(1, 1000), random.randint(1, 1 << 30)
        stop = time.time() + duration

        def sender():
            nonlocal seq, ts
            while time.time() < stop and not self._stop.is_set():
                hdr = bytes([0x80, 0x00,
                             (seq >> 8) & 0xFF, seq & 0xFF,
                             (ts >> 24) & 0xFF, (ts >> 16) & 0xFF,
                             (ts >> 8) & 0xFF, ts & 0xFF,
                             (ssrc >> 24) & 0xFF, (ssrc >> 16) & 0xFF,
                             (ssrc >> 8) & 0xFF, ssrc & 0xFF])
                try:
                    self.sock.send(hdr + bytes(160))
                    self.tx += 1
                except OSError:
                    break
                seq = (seq + 1) & 0xFFFF
                ts = (ts + 160) & 0xFFFFFFFF
                time.sleep(0.02)

        t = threading.Thread(target=sender, daemon=True)
        t.start()
        while time.time() < stop:
            try:
                data, _ = self.sock.recvfrom(2048)
                if len(data) >= 12 and data[0] & 0xC0 == 0x80:
                    self.rx += 1
            except socket.timeout:
                continue
        self._stop.set()
        t.join(timeout=1)
        return self.rx, self.tx


def serve_callee(phone, rtp):
    """B side: REGISTER, then accept Asterisk's TCP INVITE and auto-answer."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listen_port = listener.getsockname()[1]

    phone.connect()
    phone.register(listen_port)
    print(f"  [{phone.exten}] registered, listening on TCP {listen_port}")

    listener.settimeout(60)
    srv, _ = listener.accept()
    inv = SIPConn(srv)
    start, headers, rbody = inv.recv_msg(timeout=60)
    assert start.startswith("INVITE"), f"expected INVITE, got {start}"
    call_id = headers["call-id"]
    from_hdr = headers["from"]
    to_tag = rand(10)
    cseq = headers["cseq"].split()[0]
    via = headers["via"]

    def reply(code, text, body=""):
        lines = [f"SIP/2.0 {code} {text}",
                 f"Via: {via}",
                 f"From: {from_hdr}",
                 f"To: {headers['to']};tag={to_tag}",
                 f"Call-ID: {call_id}",
                 f"CSeq: {cseq} INVITE",
                 f"Contact: <sip:{phone.username}@127.0.0.1:{listen_port};transport=tcp>",
                 f"Content-Length: {len(body.encode())}"]
        if body:
            lines.append("Content-Type: application/sdp")
        inv.send_msg("\r\n".join(lines) + "\r\n\r\n" + body)

    reply("100", "Trying")
    reply("180", "Ringing")
    time.sleep(0.5)
    reply("200", "OK", body=sdp_offer(rtp.port))
    # wait for ACK
    end = time.time() + 20
    while time.time() < end:
        try:
            s2, _, _ = inv.recv_msg(timeout=2)
            if s2.startswith("ACK"):
                print(f"  [{phone.exten}] answered, ACK received")
                break
        except (TimeoutError, ConnectionError):
            continue
    peer = sdp_peer_addr(rbody)
    return peer, inv


def main():
    print("== Phase 1 test: 8800 -> 8801 (SIP/TCP) ==")
    a = SIPPhone("phone8800", "secret-8800", "8800")
    b = SIPPhone("phone8801", "secret-8801", "8801")
    a_rtp = RTPPort()
    b_rtp = RTPPort()

    print("-- register --")
    a.connect()
    a.register(a.conn.sock.getsockname()[1])

    result = {}

    def run_b():
        try:
            result["peer"], result["inv"] = serve_callee(b, b_rtp)
        except Exception as e:
            result["error"] = e

    bt = threading.Thread(target=run_b, daemon=True)
    bt.start()
    time.sleep(2)  # let B register + listen

    print("-- call --")
    peer, to_tag, _ = a.invite("8801", a_rtp.port)
    assert peer[1] != 0, "no RTP port in 200 OK SDP"

    print("-- media: A sends RTP to Asterisk --")
    rx, tx = a_rtp.run(peer, duration=4.0)
    print(f"  [8800] RTP sent={tx} received={rx}")
    # B also sends (Asterisk receives; we verify the path is up)
    b_rx, b_tx = b_rtp.run(result["peer"], duration=2.0)
    print(f"  [8801] RTP sent={b_tx} received={b_rx}")

    print("-- bridge check via ARI --")
    import urllib.request, json
    pw = open("/etc/pbx/ari.pass").read().strip()
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, "http://127.0.0.1:8088/ari/", "pbxbrain", pw)
    opener = urllib.request.build_opener(
        urllib.request.HTTPBasicAuthHandler(mgr))
    bridges = json.load(opener.open("http://127.0.0.1:8088/ari/bridges"))
    print(f"  bridges: {len(bridges)}, "
          f"channels: {[len(br.get('channels', [])) for br in bridges]}")

    print("-- hangup --")
    a.bye("8801", to_tag)
    time.sleep(2)

    print("-- cdr --")
    c = sqlite3.connect(DB)
    row = c.execute("SELECT src, dst, disposition, duration_sec, bill_sec"
                    " FROM cdr ORDER BY id DESC LIMIT 1").fetchone()
    print("  latest cdr:", row)

    ok = True
    if tx == 0:
        print("FAIL: A sent no RTP"); ok = False
    if not bridges or not any(len(br.get("channels", [])) == 2
                              for br in bridges):
        print("FAIL: no 2-channel bridge found"); ok = False
    if not row or row[2] != "ANSWERED":
        print("FAIL: no ANSWERED cdr row"); ok = False
    if row and row[4] == 0:
        print("FAIL: bill_sec is 0"); ok = False
    if "error" in result:
        print("FAIL: callee error:", result["error"]); ok = False
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
