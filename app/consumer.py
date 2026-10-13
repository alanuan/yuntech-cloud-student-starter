#!/usr/bin/env python3
"""W6 consumer: IoT Core topic -> device list check -> the local event API. It never touches the database."""
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "http://127.0.0.1:8080/events"
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def post(body, token):
    request = urllib.request.Request(API, data=body, method="POST")
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return None


def handle(payload, allowed, send):
    """One message -> (event_id for the log, result). device_id is only a claim, so it is checked first."""
    try:
        event = json.loads(payload.decode("utf-8"))
    except ValueError:
        return "?", "rejected"
    if not isinstance(event, dict):
        return "?", "rejected"
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not SAFE_ID.fullmatch(event_id):
        event_id = "?"  # never copy unchecked message text into the log
    if event.get("device_id") not in allowed:
        return event_id, "rejected"
    status = send(json.dumps(event).encode("utf-8"))
    if status == 201:
        return event_id, "created"
    if status == 200:
        return event_id, "duplicate"
    if status is not None and 400 <= status < 500:
        return event_id, "rejected"
    return event_id, "error"


def main():
    import paho.mqtt.client as mqtt  # vendored on the host by the packer (paho-mqtt 2.1.0)
    env = os.environ
    names = ("IOT_ENDPOINT", "IOT_TOPIC", "IOT_CLIENT_ID", "IOT_DEVICE_IDS", "IOT_CA", "IOT_CERT", "IOT_KEY",
             "REPORTER_TOKEN")
    missing = [name for name in names if not env.get(name)]
    if missing:
        sys.exit("missing settings: " + ", ".join(missing))
    allowed = frozenset(item for item in env["IOT_DEVICE_IDS"].split(",") if item)
    token = env["REPORTER_TOKEN"]

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            print("connack rc=%d" % reason_code.value, flush=True)
        else:
            client.subscribe(env["IOT_TOPIC"], qos=1)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        print("subscribed rc=%d" % reason_codes[0].value, flush=True)

    def on_message(client, userdata, message):
        print("event_id=%s result=%s" % handle(message.payload, allowed, lambda body: post(body, token)), flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=env["IOT_CLIENT_ID"], protocol=mqtt.MQTTv5)
    client.tls_set(ca_certs=env["IOT_CA"], certfile=env["IOT_CERT"], keyfile=env["IOT_KEY"])
    client.on_connect, client.on_subscribe, client.on_message = on_connect, on_subscribe, on_message
    client.connect(env["IOT_ENDPOINT"], 8883, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    main()
