#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Test *97 voicemail check."""
import socket, re, hashlib, random, time

def rand(n=8):
    return "".join(random.choice("abcdef0123456789") for _ in range(n))

class QuickPhone:
    def __init__(self, user, pw, exten):
        self.user, self.pw, self.exten = user, pw, exten
        self.cseq = 100
        self.call_id = rand(10) + "@127.0.0.1"
        self.tag = rand(8)
        s = socket.create_connection(("127.0.0.1", 5060), timeout=5)
        self.sock = s
        self.buf = b""
        s.settimeout(0.5)

    def send(self, msg):
        self.sock.sendall(msg.encode())

    def recv(self, timeout=10):
        end = time.time() + timeout
        while True:
            if b"\r\n\r\n" in self.buf:
                head, _, rest = self.buf.partition(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                h = {}
                for l in lines[1:]:
                    if ":" in l:
                        k, v = l.split(":", 1)
                        h[k.strip().lower()] = v.strip()
                clen = int(h.get("content-length", 0))
                if len(rest) >= clen:
                    self.buf = rest[clen:]
                    return lines[0], h
            if time.time() > end:
                raise TimeoutError()
            try:
                d = self.sock.recv(65535)
                if not d: raise ConnectionError()
                self.buf += d
            except socket.timeout:
                continue

    def register(self):
        port = self.sock.getsockname()[1]
        for attempt in range(2):
            self.cseq += 1
            msg = (f"REGISTER sip:127.0.0.1:5060 SIP/2.0\r\n"
                   f"Via: SIP/2.0/TCP 127.0.0.1;branch=z9hG4bK{rand(6)};rport\r\n"
                   f"From: <sip:{self.user}@127.0.0.1>;tag={self.tag}\r\n"
                   f"To: <sip:{self.user}@127.0.0.1>\r\n"
                   f"Call-ID: {self.call_id}@127.0.0.1\r\n"
                   f"CSeq: {self.cseq} REGISTER\r\n"
                   f"Contact: <sip:{self.user}@127.0.0.1:{port};transport=tcp>\r\n"
                   "Expires: 300\r\nContent-Length: 0\r\n\r\n")
            self.send(msg)
            start, h = self.recv()
            if "401" in start:
                p = dict(re.findall(r'(\w+)="([^"]*)"', h["www-authenticate"]))
                ha1 = hashlib.md5(f"{self.user}:{p['realm']}:{self.pw}".encode()).hexdigest()
                ha2 = hashlib.md5(b"REGISTER:sip:127.0.0.1:5060").hexdigest()
                resp = hashlib.md5(f"{ha1}:{p['nonce']}:{ha2}".encode()).hexdigest()
                self.cseq += 1
                msg2 = (f"REGISTER sip:127.0.0.1:5060 SIP/2.0\r\n"
                        f"Via: SIP/2.0/TCP 127.0.0.1;branch=z9hG4bK{rand(6)};rport\r\n"
                        f"From: <sip:{self.user}@127.0.0.1>;tag={self.tag}\r\n"
                        f"To: <sip:{self.user}@127.0.0.1>\r\n"
                        f"Call-ID: {self.call_id}@127.0.0.1\r\n"
                        f"CSeq: {self.cseq} REGISTER\r\n"
                        f"Contact: <sip:{self.user}@127.0.0.1:{port};transport=tcp>\r\n"
                        "Expires: 300\r\n"
                        f'Authorization: Digest username="{self.user}", realm="{p["realm"]}", nonce="{p["nonce"]}", uri="sip:127.0.0.1:5060", response="{resp}", algorithm=MD5\r\n'
                        "Content-Length: 0\r\n\r\n")
                self.send(msg2)
                start, h = self.recv()
            if "200" in start:
                print(f"[{self.exten}] REGISTER OK")
                return
        raise RuntimeError("register failed")

    def call(self, dest):
        self.cseq += 1
        cseq = self.cseq
        msg = (f"INVITE sip:{dest}@127.0.0.1:5060 SIP/2.0\r\n"
               f"Via: SIP/2.0/TCP 127.0.0.1;branch=z9hG4bK{rand(6)};rport\r\n"
               f"From: \"{self.exten}\" <sip:{self.user}@127.0.0.1>;tag={self.tag}\r\n"
               f"To: <sip:{dest}@127.0.0.1>\r\n"
               f"Call-ID: {rand(10)}@127.0.0.1\r\n"
               f"CSeq: {cseq} INVITE\r\n"
               f"Contact: <sip:{self.user}@127.0.0.1:{self.sock.getsockname()[1]};transport=tcp>\r\n"
               "Max-Forwards: 70\r\nContent-Length: 0\r\n\r\n")
        self.send(msg)
        # Handle 401, wait for 200
        to_tag = None
        while True:
            start, h = self.recv(timeout=15)
            if "401" in start or "407" in start:
                p = dict(re.findall(r'(\w+)="([^"]*)"', h.get("www-authenticate", h.get("proxy-authenticate", ""))))
                ha1 = hashlib.md5(f"{self.user}:{p['realm']}:{self.pw}".encode()).hexdigest()
                ha2 = hashlib.md5(f"INVITE:sip:{dest}@127.0.0.1:5060".encode()).hexdigest()
                resp = hashlib.md5(f"{ha1}:{p['nonce']}:{ha2}".encode()).hexdigest()
                self.cseq += 1
                msg2 = (f"INVITE sip:{dest}@127.0.0.1:5060 SIP/2.0\r\n"
                        f"Via: SIP/2.0/TCP 127.0.0.1;branch=z9hG4bK{rand(6)};rport\r\n"
                        f"From: \"{self.exten}\" <sip:{self.user}@127.0.0.1>;tag={self.tag}\r\n"
                        f"To: <sip:{dest}@127.0.0.1>\r\n"
                        f"Call-ID: {self.call_id}@127.0.0.1\r\n"
                        f"CSeq: {self.cseq} INVITE\r\n"
                        f"Contact: <sip:{self.user}@127.0.0.1:{self.sock.getsockname()[1]};transport=tcp>\r\n"
                        "Max-Forwards: 70\r\n"
                        f'Proxy-Authorization: Digest username="{self.user}", realm="{p["realm"]}", nonce="{p["nonce"]}", uri="sip:{dest}@127.0.0.1:5060", response="{resp}", algorithm=MD5\r\n'
                        "Content-Length: 0\r\n\r\n")
                self.send(msg2)
                cseq = self.cseq
                continue
            if "180" in start or "183" in start:
                print(f"[{self.exten}] ringing...")
                continue
            if "200" in start:
                m = re.search(r'tag=([^;,\s]+)', h.get("to", ""))
                to_tag = m.group(1) if m else ""
                print(f"[{self.exten}] {dest} answered")
                # ACK
                ack = (f"ACK sip:{dest}@127.0.0.1:5060 SIP/2.0\r\n"
                       f"Via: SIP/2.0/TCP 127.0.0.1;branch=z9hG4bK{rand(6)};rport\r\n"
                       f"From: \"{self.exten}\" <sip:{self.user}@127.0.0.1>;tag={self.tag}\r\n"
                       f"To: <sip:{dest}@127.0.0.1>;tag={to_tag}\r\n"
                       f"Call-ID: {self.call_id}@127.0.0.1\r\n"
                       f"CSeq: {cseq} ACK\r\nContent-Length: 0\r\n\r\n")
                self.send(ack)
                return to_tag
            raise RuntimeError(f"call failed: {start}")

    def bye(self, dest, to_tag):
        self.cseq += 1
        msg = (f"BYE sip:{dest}@127.0.0.1:5060 SIP/2.0\r\n"
               f"Via: SIP/2.0/TCP 127.0.0.1;branch=z9hG4bK{rand(6)};rport\r\n"
               f"From: <sip:{self.user}@127.0.0.1>;tag={self.tag}\r\n"
               f"To: <sip:{dest}@127.0.0.1>;tag={to_tag}\r\n"
               f"Call-ID: {self.call_id}@127.0.0.1\r\n"
               f"CSeq: {self.cseq} BYE\r\nContent-Length: 0\r\n\r\n")
        self.send(msg)
        start, _ = self.recv(timeout=5)
        print(f"[{self.exten}] BYE -> {start.split()[1]}")

print("== *97 voicemail test ==")
a = QuickPhone("phone8800", "secret-8800", "8800")
a.register()
tag = a.call("*97")
time.sleep(3)
a.bye("*97", tag)
print("PASS: *97 completed")
