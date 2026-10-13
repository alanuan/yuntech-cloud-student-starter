"""Shared by the W6 tools: build an MQTT 5 client for one identity from the files in .local/iot/."""
import json
from pathlib import Path
import sys
import threading

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[1]
IOT = ROOT / ".local" / "iot"
TIMEOUT = 15


def stop(message):
    sys.exit("STOP: " + message)


def code():
    """The student's code, taken from the gateway policy name recorded by iot-up.sh (cxe-<code>-gw)."""
    name = json.loads((ROOT / ".local" / "resources.json").read_text(encoding="utf-8")).get("iot_gw_policy", "")
    if not (name.startswith("cxe-") and name.endswith("-gw")):
        stop("iot_gw_policy is not in .local/resources.json; run deploy/iot-up.sh first")
    return name[len("cxe-"):-len("-gw")]


class Session:
    """One connected client. publish() and subscribe() wait for the broker's own answer and return its reason code."""

    def __init__(self, role, on_message=None):
        self.answers = {}  # message id -> reason code
        self.arrived = threading.Condition()
        self.connack = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="%s-%s" % (role, code()),
                                  protocol=mqtt.MQTTv5)
        # Server certificate and host name are verified against Amazon Root CA 1 (paho's default; never disabled).
        self.client.tls_set(ca_certs=str(IOT / "AmazonRootCA1.pem"), certfile=str(IOT / (role + ".cert.pem")),
                            keyfile=str(IOT / (role + ".private.key")))
        self.client.on_connect = self._on_connect
        self.client.on_publish = lambda client, userdata, mid, reason_code, properties: self._answer(mid, reason_code.value)
        self.client.on_subscribe = lambda client, userdata, mid, reason_codes, properties: self._answer(mid, reason_codes[0].value)
        if on_message:
            self.client.on_message = on_message
        self.client.connect((IOT / "endpoint").read_text(encoding="utf-8").strip(), 8883, keepalive=30)
        self.client.loop_start()
        with self.arrived:
            if not self.arrived.wait_for(lambda: self.connack is not None, TIMEOUT):
                stop("no CONNACK within %d s (client ID not in the policy, policy not attached, wrong CA or endpoint?)" % TIMEOUT)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        with self.arrived:
            self.connack = reason_code.value
            self.arrived.notify_all()

    def _answer(self, mid, value):
        with self.arrived:
            self.answers[mid] = value
            self.arrived.notify_all()

    def _wait(self, mid):
        with self.arrived:
            if not self.arrived.wait_for(lambda: mid in self.answers, TIMEOUT):
                return None
            return self.answers.pop(mid)

    def publish(self, topic, payload):
        """Returns the PUBACK reason code for exactly this message, or None when it did not arrive."""
        return self._wait(self.client.publish(topic, payload, qos=1).mid)

    def subscribe(self, topic_filter):
        return self._wait(self.client.subscribe(topic_filter, qos=1)[1])

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()
