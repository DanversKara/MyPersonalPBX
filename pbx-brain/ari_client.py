# SPDX-License-Identifier: GPL-2.0-or-later
"""Minimal ARI client: REST via requests, events via websocket-client.

Covers exactly what pbx-brain needs (originate, hangup, answer, bridges,
record, snoop, play, channel events). One less dead dependency to babysit:
the PyPI/GitHub ari-py packages are Python-2-only and unmaintained.
"""
import json
import logging

import requests
import websocket

log = logging.getLogger("ari-client")


class Channel:
    def __init__(self, client, channel_id):
        self._client = client
        self.id = channel_id
        self.json = {}

    def hangup(self):
        self._client.delete("channels/%s" % self.id)

    def answer(self):
        self._client.post("channels/%s/answer" % self.id)

    def ring(self):
        self._client.post("channels/%s/ring" % self.id)

    def stop_ring(self):
        self._client.delete("channels/%s/ring" % self.id)

    def start_moh(self):
        self._client.post("channels/%s/moh" % self.id)

    def stop_moh(self):
        self._client.delete("channels/%s/moh" % self.id)

    def play(self, media):
        return self._client.post("channels/%s/play" % self.id,
                                 params={"media": media})

    def record(self, name, format="wav", max_duration=0, beep=False,
               terminate_on="none", if_exists="fail"):
        return self._client.post(
            "channels/%s/record" % self.id,
            params={"name": name, "format": format,
                    "maxDurationSeconds": max_duration,
                    "beep": "true" if beep else "false",
                    "terminateOn": terminate_on, "ifExists": if_exists})

    def get_var(self, name):
        """Channel variable / dialplan function value ('' if unset)."""
        try:
            r = self._client.get("channels/%s/variable" % self.id, params={"variable": name})
            return (r or {}).get("value", "") or ""
        except Exception:
            return ""

    def snoop(self, app, spy="both", whisper="none", app_args=""):
        return self._client.post(
            "channels/%s/snoop" % self.id,
            params={"app": app, "spy": spy, "whisper": whisper,
                    "appArgs": app_args})


class Bridge:
    def __init__(self, client, bridge_id):
        self._client = client
        self.id = bridge_id
        self.json = {}

    def add_channel(self, channel_ids):
        if isinstance(channel_ids, str):
            channel_ids = [channel_ids]
        self._client.post("bridges/%s/addChannel" % self.id,
                           params={"channel": channel_ids})

    def remove_channel(self, channel_ids):
        if isinstance(channel_ids, str):
            channel_ids = [channel_ids]
        self._client.post("bridges/%s/removeChannel" % self.id,
                           params={"channel": channel_ids})

    def destroy(self):
        self._client.delete("bridges/%s" % self.id)

    def play(self, media):
        return self._client.post("bridges/%s/play" % self.id, params={"media": media})

    def record(self, name, format="wav", max_duration=0):
        return self._client.post(
            "bridges/%s/record" % self.id,
            params={"name": name, "format": format,
                    "maxDurationSeconds": max_duration})


class _ChannelNS:
    def __init__(self, client):
        self._c = client

    def originate(self, endpoint, app, app_args="", caller_id="",
                  timeout=30):
        params = {"endpoint": endpoint, "app": app, "timeout": timeout}
        if app_args:
            params["appArgs"] = app_args
        if caller_id:
            params["callerId"] = caller_id
        data = self._c.post("channels", params=params)
        ch = Channel(self._c, data["id"])
        ch.json = data
        return ch

    def get(self, channel_id):
        data = self._c.get("channels/%s" % channel_id)
        ch = Channel(self._c, channel_id)
        ch.json = data
        return ch


class _BridgeNS:
    def __init__(self, client):
        self._c = client

    def create(self, type="mixing"):
        data = self._c.post("bridges", params={"type": type})
        b = Bridge(self._c, data["id"])
        b.json = data
        return b

    def get(self, bridge_id):
        data = self._c.get("bridges/%s" % bridge_id)
        b = Bridge(self._c, bridge_id)
        b.json = data
        return b


class ARIClient:
    """Thin wrapper over the ARI REST API + event websocket."""

    def __init__(self, base_url, username, password):
        self.base = base_url.rstrip("/") + "/"
        self.auth = (username, password)
        self._handlers = {}
        self.channels = _ChannelNS(self)
        self.bridges = _BridgeNS(self)

    # -- REST -----------------------------------------------------------
    def _req(self, method, path, params=None):
        url = self.base + path.lstrip("/")
        r = requests.request(method, url, params=params, auth=self.auth,
                             timeout=15)
        if r.status_code >= 400:
            raise RuntimeError("ARI %s %s -> %s: %s"
                               % (method, path, r.status_code, r.text[:200]))
        return r.json() if r.text.strip() else None

    def get(self, path, params=None):
        return self._req("GET", path, params)

    def post(self, path, params=None):
        return self._req("POST", path, params)

    def put(self, path, params=None):
        return self._req("PUT", path, params)

    def delete(self, path, params=None):
        return self._req("DELETE", path, params)

    # -- events ----------------------------------------------------------
    def on_event(self, event_type, callback):
        """callback(channel, event) for channel events, else callback(event)."""
        self._handlers.setdefault(event_type, []).append(callback)

    def _dispatch(self, raw):
        try:
            event = json.loads(raw)
        except Exception:
            return
        etype = event.get("type")
        for cb in self._handlers.get(etype, []):
            try:
                if "channel" in event and isinstance(event["channel"], dict):
                    ch = Channel(self, event["channel"]["id"])
                    ch.json = event["channel"]
                    cb(ch, event)
                else:
                    cb(event)
            except Exception:
                log.exception("event handler failed for %s", etype)

    def run(self, app):
        ws_url = (self.base.replace("https://", "wss://")
                  .replace("http://", "ws://")
                  + "events?app=%s&api_key=%s:%s"
                  % (app, self.auth[0], self.auth[1]))
        log.info("connecting to %s",
                 ws_url.split("api_key")[0] + "api_key=***")
        ws = websocket.WebSocketApp(
            ws_url,
            on_message=lambda ws, msg: self._dispatch(msg),
            on_error=lambda ws, err: log.error("ws error: %s", err),
            on_close=lambda ws, *a: log.warning("ws closed"),
            on_open=lambda ws: log.info("event stream open"))
        ws.run_forever()
