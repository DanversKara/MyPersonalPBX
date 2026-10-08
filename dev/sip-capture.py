#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
sip-capture.py — Capture SIP traffic on port 5060 during phone registration.
Bypasses Asterisk's pjsip history (which isn't working). Uses raw UDP socket.

Usage (on PBX as root):
    python3 sip-capture.py [duration_seconds] [phone_ip]

Example:
    python3 sip-capture.py 60 192.168.1.50

Reboot the phone while this runs. It prints all SIP messages to/from the phone.
"""
import socket
import sys
import time
from datetime import datetime

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 60
PHONE_IP = sys.argv[2] if len(sys.argv) > 2 else None

# Raw UDP socket on 5060 — SO_REUSEADDR lets us bind alongside Asterisk
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(('0.0.0.0', 5060))
except OSError as e:
    print(f"Cannot bind to 5060: {e}")
    print("Asterisk is using the port. Use iptables LOG or mirror instead.")
    sys.exit(1)

sock.settimeout(1.0)
print(f"Capturing SIP on UDP 5060 for {DURATION}s" +
      (f" (filter: {PHONE_IP})" if PHONE_IP else " (all hosts)") + "...")
print("Reboot the phone now.\n")

start = time.time()
count = 0
while time.time() - start < DURATION:
    try:
        data, addr = sock.recvfrom(65535)
    except socket.timeout:
        continue
    ip, port = addr
    if PHONE_IP and ip != PHONE_IP:
        continue
    try:
        msg = data.decode('utf-8', errors='replace')
    except:
        continue
    # Only show SIP (starts with method or SIP/2.0)
    first = msg.split('\n')[0].strip()
    if not (first.startswith('SIP/2.0') or
            any(first.startswith(m) for m in
                ('REGISTER', 'INVITE', 'ACK', 'BYE', 'CANCEL', 'OPTIONS',
                 'SUBSCRIBE', 'NOTIFY', 'PUBLISH', 'REFER', 'MESSAGE'))):
        continue
    count += 1
    ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
    direction = "PHONE -> PBX" if ip == PHONE_IP else f"{ip} -> PBX"
    print(f"\n===== [{ts}] {direction}:{port} ({len(data)} bytes) =====")
    # Print headers only (not SDP body) for readability
    parts = msg.split('\r\n\r\n', 1)
    print(parts[0][:2000])
    if 'Authorization:' in msg:
        print("  [HAS Authorization header]")
    if 'WWW-Authenticate:' in msg:
        print("  [HAS WWW-Authenticate challenge]")
    # Quick analysis
    if 'REGISTER' in first:
        print("  >>> REGISTER attempt")
    elif '401 Unauthorized' in first:
        print("  >>> 401 challenge sent")
    elif '200 OK' in first and 'CSeq:' in msg and 'REGISTER' in msg:
        print("  >>> 200 OK - REGISTRATION SUCCESS!")

print(f"\n\nDone. Captured {count} SIP messages.")
if count == 0:
    print("NO SIP TRAFFIC AT ALL — the phone is not sending anything to 5060.")
    print("Check: phone IP, SIP port, network, phone SIP stack enabled.")
