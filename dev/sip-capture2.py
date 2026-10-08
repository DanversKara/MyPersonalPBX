#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
sip-capture2.py — Capture SIP via raw socket (no port binding needed).
Works alongside Asterisk.

Usage (on PBX as root):
    python3 sip-capture2.py 60 192.168.1.50

Reboot the phone while this runs.
"""
import socket
import struct
import sys
import time
from datetime import datetime

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 60
PHONE_IP = sys.argv[2] if len(sys.argv) > 2 else None

def ip_to_int(ip):
    return struct.unpack("!I", socket.inet_aton(ip))[0]

PHONE_INT = ip_to_int(PHONE_IP) if PHONE_IP else None

# Raw socket for UDP
try:
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0800))
except PermissionError:
    print("Need root for raw socket")
    sys.exit(1)

sock.settimeout(1.0)
print(f"Capturing SIP for {DURATION}s" +
      (f" (filter: {PHONE_IP})" if PHONE_IP else "") + "...")
print("Reboot the phone now.\n")

start = time.time()
count = 0
while time.time() - start < DURATION:
    try:
        data, _ = sock.recvfrom(65535)
    except socket.timeout:
        continue
    # Ethernet (14) + IP (20 min) + UDP (8)
    if len(data) < 42:
        continue
    # IP header
    ip_hdr = data[14:34]
    iph = struct.unpack('!BBHHHBBH4s4s', ip_hdr)
    proto = iph[6]
    if proto != 17:  # UDP only
        continue
    src_ip = socket.inet_ntoa(iph[8])
    dst_ip = socket.inet_ntoa(iph[9])
    # UDP header
    udp_hdr = data[34:42]
    udph = struct.unpack('!HHHH', udp_hdr)
    src_port, dst_port = udph[0], udph[1]
    if src_port != 5060 and dst_port != 5060:
        continue
    if PHONE_IP and src_ip != PHONE_IP and dst_ip != PHONE_IP:
        continue
    # SIP payload
    payload = data[42:]
    try:
        msg = payload.decode('utf-8', errors='replace')
    except:
        continue
    first = msg.split('\n')[0].strip() if '\n' in msg else msg[:60]
    if not (first.startswith('SIP/2.0') or
            any(first.startswith(m) for m in
                ('REGISTER', 'INVITE', 'ACK', 'BYE', 'CANCEL', 'OPTIONS'))):
        continue
    count += 1
    ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
    direction = "-> PBX" if dst_port == 5060 else "<- PBX"
    print(f"\n===== [{ts}] {src_ip}:{src_port} {direction} ({len(payload)}b) =====")
    headers = msg.split('\r\n\r\n', 1)[0][:1500]
    print(headers)
    if 'Authorization:' in msg:
        # Show auth username without revealing full hash
        for line in msg.split('\n'):
            if 'Authorization:' in line:
                u = line.split('username=')[1].split(',')[0] if 'username=' in line else '?'
                print(f"  [Auth username: {u}]")
                break
    if 'WWW-Authenticate:' in msg:
        print("  [401 challenge]")
    if first.startswith('SIP/2.0 200') and 'REGISTER' in msg:
        print("  *** REGISTRATION SUCCESS ***")
    elif first.startswith('SIP/2.0 401'):
        print("  >>> 401 Unauthorized")
    elif first.startswith('SIP/2.0 403'):
        print("  >>> 403 Forbidden - AUTH FAILED")
    elif first.startswith('REGISTER'):
        print("  >>> REGISTER attempt")

print(f"\n\nDone. Captured {count} SIP messages.")
if count == 0:
    print("NO SIP TRAFFIC — phone is not sending to UDP 5060.")
