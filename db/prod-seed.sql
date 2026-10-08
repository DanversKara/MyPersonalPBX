-- SPDX-License-Identifier: GPL-2.0-or-later
-- Production seed: trunk, inbound/outbound routes
-- Run after schema.sql on the production server

INSERT OR REPLACE INTO trunks (name, registrar, username, secret, from_user, codecs, enabled) VALUES ('voipms','sip:YOUR_POP.voip.ms','YOUR_SIP_USERNAME','YOUR_SIP_SECRET','','ulaw,alaw,g722',1);
INSERT OR REPLACE INTO inbound_routes (did, ring_extens, timeout_sec, enabled) VALUES ('YOUR_DID_10_DIGIT','["8800", "8801"]',30,1);  -- one route covers 10 or 11 digits
INSERT OR REPLACE INTO outbound_routes (name, patterns, trunk_id, priority, enabled) VALUES ('voipms-out','["1NXXNXXXXXX", "NXXNXXXXXX", "011."]',(SELECT id FROM trunks WHERE name='voipms'),10,1);
