#!/usr/bin/env python3
"""Inspection service: W3 health check plus the W4 in-memory event API and display page."""
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

MAX_BODY = 4 * 1024
LIST_LIMIT = 50
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
TYPES = ("status", "anomaly", "test")
FIELDS = ("event_id", "device_id", "observed_at", "type", "note")
REQUIRED = ("event_id", "device_id", "observed_at", "type")
# ISO 8601 date-time whose time zone is mandatory (Z or +hh:mm / -hh:mm).
OBSERVED_AT = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(:\d{2})?(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})")

PAGE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>巡檢事件</title>
<style>
body { font-family: system-ui, sans-serif; margin: 1.5rem; }
table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
th, td { border: 1px solid #999; padding: .3rem .5rem; text-align: left; }
#state { margin-left: .5rem; }
</style>
</head>
<body>
<h1>巡檢事件</h1>
<label>operator 權杖 <input id="token" type="password" autocomplete="off" size="48"></label>
<button id="load" type="button">讀取</button>
<button id="clear" type="button">清空權杖</button>
<span id="state"></span>
<table>
<thead><tr><th>event_id</th><th>device_id</th><th>type</th><th>observed_at</th><th>received_at</th><th>note</th></tr></thead>
<tbody id="rows"></tbody>
</table>
<script>
"use strict";
const columns = ["event_id", "device_id", "type", "observed_at", "received_at", "note"];
const tokenBox = document.getElementById("token");
const state = document.getElementById("state");
const rows = document.getElementById("rows");
document.getElementById("clear").addEventListener("click", () => { tokenBox.value = ""; });
document.getElementById("load").addEventListener("click", async () => {
  const token = tokenBox.value;  // kept in this variable only: never in the URL or browser storage
  rows.replaceChildren();
  state.textContent = "讀取中";
  try {
    const response = await fetch("/events", { headers: { Authorization: "Bearer " + token }, cache: "no-store" });
    const body = await response.json();
    if (!response.ok) { state.textContent = "HTTP " + response.status + " " + body.error; return; }
    for (const event of body.events) {
      const tr = document.createElement("tr");
      for (const name of columns) {
        const td = document.createElement("td");
        td.textContent = event[name] === undefined ? "" : event[name];
        tr.appendChild(td);
      }
      rows.appendChild(tr);
    }
    state.textContent = "共 " + body.events.length + " 筆";
  } catch (error) {
    state.textContent = "讀取失敗";
  }
});
</script>
</body>
</html>
""".encode("utf-8")


class Rejected(Exception):
    def __init__(self, status, error, field=None):
        super().__init__(error)
        self.status, self.error, self.field = status, error, field


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def has_timezone(value):
    match = OBSERVED_AT.fullmatch(value)
    if not match:
        return False
    date, minutes, seconds, fraction, zone = match.groups()
    # Normalised so the Python 3.9 on AL2023 parses it; this also rejects impossible dates and offsets.
    text = (date + "T" + minutes + (seconds or ":00") + (fraction or ".0").ljust(7, "0")
            + ("+00:00" if zone == "Z" else zone))
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def validate_event(raw):
    try:
        event = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise Rejected(400, "invalid_json") from None
    if not isinstance(event, dict):
        raise Rejected(400, "invalid_json")
    for name in event:
        if name not in FIELDS:
            raise Rejected(400, "unknown_field", name)
    for name in REQUIRED:
        if name not in event:
            raise Rejected(400, "missing_field", name)
    for name, limit in (("event_id", 64), ("device_id", 32)):
        value = event[name]
        if not isinstance(value, str) or not 1 <= len(value) <= limit or not ID_PATTERN.fullmatch(value):
            raise Rejected(400, "invalid_field", name)
    if not isinstance(event["observed_at"], str) or not has_timezone(event["observed_at"]):
        raise Rejected(400, "invalid_field", "observed_at")
    if event["type"] not in TYPES:
        raise Rejected(400, "invalid_field", "type")
    if "note" in event and (not isinstance(event["note"], str) or len(event["note"]) > 200):
        raise Rejected(400, "invalid_field", "note")
    return event


def make_server(version_file, port=8080, tokens=None):
    version = Path(version_file).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")
    started = utc_now()
    if tokens is None:
        tokens = {"reporter": os.environ.get("REPORTER_TOKEN", ""),
                  "operator": os.environ.get("OPERATOR_TOKEN", "")}
    # Both roles need their own token; otherwise nobody can authenticate.
    configured = bool(tokens.get("reporter")) and bool(tokens.get("operator")) \
        and tokens["reporter"] != tokens["operator"]
    secrets_by_role = {role: tokens[role].encode("utf-8") for role in ("reporter", "operator")} if configured else {}
    events = {}  # event_id -> stored event; insertion order is arrival order. Memory only until W5.
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def send_json(self, status, body, headers=()):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def reject(self, status, error, field=None):
            # The request body may be unread, so this connection cannot be reused.
            self.close_connection = True
            headers = (("WWW-Authenticate", "Bearer"),) if status == 401 else ()
            self.send_json(status, {"error": error, "field": field}, headers)

        def require(self, role):
            """Authenticate (401), then authorise (403). Runs before the body is looked at."""
            scheme, _, supplied = self.headers.get("Authorization", "").partition(" ")
            supplied = supplied.strip().encode("utf-8")
            found = None
            if scheme.lower() == "bearer" and supplied:
                for name, secret in secrets_by_role.items():
                    if hmac.compare_digest(supplied, secret):
                        found = name
            if found is None:
                raise Rejected(401, "unauthorized")
            if found != role:
                raise Rejected(403, "forbidden")

        def read_body(self):
            if self.headers.get_content_type() != "application/json":
                raise Rejected(400, "invalid_content_type")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise Rejected(400, "invalid_length") from None
            if length < 0:
                raise Rejected(400, "invalid_length")
            if length > MAX_BODY:
                raise Rejected(400, "body_too_large")
            return self.rfile.read(length)

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == "/health":
                    self.send_json(200, {"status": "ok", "service": "inspection", "version": version,
                                         "started_at": started, "auth_configured": configured})
                elif path == "/":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(PAGE)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                    self.wfile.write(PAGE)
                elif path == "/events":
                    self.require("operator")
                    with lock:
                        latest = list(events.values())[-LIST_LIMIT:]
                    self.send_json(200, {"events": latest[::-1]})
                elif path.startswith("/events/"):
                    self.require("operator")
                    with lock:
                        event = events.get(path[len("/events/"):])
                    if event is None:
                        raise Rejected(404, "not_found")
                    self.send_json(200, event)
                else:
                    raise Rejected(404, "not_found")
            except Rejected as refusal:
                self.reject(refusal.status, refusal.error, refusal.field)

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                if path != "/events":
                    raise Rejected(404, "not_found")
                self.require("reporter")
                event = validate_event(self.read_body())
                with lock:
                    if event["event_id"] in events:
                        raise Rejected(409, "duplicate_event", "event_id")
                    stored = dict(event, received_at=utc_now())
                    events[event["event_id"]] = stored
                self.send_json(201, stored)
            except Rejected as refusal:
                self.reject(refusal.status, refusal.error, refusal.field)

        def log_message(self, fmt, *args):
            pass  # Never log request paths, bodies, headers or query strings.

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
