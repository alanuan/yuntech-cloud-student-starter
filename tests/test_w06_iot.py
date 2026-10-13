"""Offline W6 checks: the device filter, the consumer's classification and the gateway's 32 messages."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
from test_w04_events import OPERATOR, REPORTER, EventContract, fixture  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


consumer = load("w06_consumer", "app/consumer.py")


class DeviceFilter(EventContract):
    def test_filter_returns_only_that_device_newest_first(self):
        for serial, device in enumerate(("g01-m1-d01", "g01-m1-d02", "g01-m1-d01")):
            event = dict(fixture("event_valid.json"), event_id="g01-m1-f-%d" % serial, device_id=device)
            self.assertEqual(self.call("POST", "/events", REPORTER, event)[0], 201)
        status, body = self.call_json("GET", "/events?device_id=g01-m1-d01", OPERATOR)
        self.assertEqual(status, 200)
        self.assertEqual([event["event_id"] for event in body["events"]], ["g01-m1-f-2", "g01-m1-f-0"])
        self.assertEqual(len(self.call_json("GET", "/events", OPERATOR)[1]["events"]), 3)

    def test_filter_needs_the_operator_token_before_anything_else(self):
        self.assertEqual(self.call("GET", "/events?device_id=g01-m1-d01")[0], 401)
        self.assertEqual(self.call("GET", "/events?device_id=g01-m1-d01", REPORTER)[0], 403)
        self.assertEqual(self.call("GET", "/events?device_id=bad%20id", REPORTER)[0], 403)

    def test_filter_rejects_a_malformed_device_id(self):
        for bad in ("", "bad%20id", "a%27%20OR%201=1", "x" * 33):
            status, body = self.call_json("GET", "/events?device_id=" + bad, OPERATOR)
            self.assertEqual((status, body), (400, {"error": "invalid_field", "field": "device_id"}), bad)

    def test_page_has_the_device_box_and_still_uses_text_content(self):
        page = self.call("GET", "/")[1].decode("utf-8")
        self.assertIn('id="device"', page)
        self.assertIn("encodeURIComponent(device)", page)
        self.assertIn("textContent", page)
        self.assertNotIn("innerHTML", page)


class ConsumerClassification(unittest.TestCase):
    allowed = frozenset(["g01-m1-d01"])
    event = {"event_id": "g01-m1-r1-d01-01", "device_id": "g01-m1-d01", "observed_at": "2026-10-13T10:00:00+08:00",
             "type": "status"}

    def run_one(self, event, status):
        sent = []
        payload = event if isinstance(event, bytes) else json.dumps(event).encode("utf-8")
        result = consumer.handle(payload, self.allowed, lambda body: sent.append(body) or status)
        return result, len(sent)

    def test_api_answers_are_classified(self):
        for status, expected in ((201, "created"), (200, "duplicate"), (400, "rejected"), (409, "rejected"),
                                 (401, "rejected"), (503, "error"), (None, "error")):
            self.assertEqual(self.run_one(self.event, status), ((self.event["event_id"], expected), 1), status)

    def test_unlisted_device_is_rejected_without_calling_the_api(self):
        outsider = dict(self.event, event_id="g01-m1-r1-d99-01", device_id="g01-m1-d99")
        self.assertEqual(self.run_one(outsider, 201), (("g01-m1-r1-d99-01", "rejected"), 0))
        self.assertEqual(self.run_one({"event_id": "x"}, 201), (("x", "rejected"), 0))

    def test_unusable_messages_never_reach_the_api_or_the_log(self):
        for payload in (b"not json", b"[1]", b"\xff\xfe"):
            self.assertEqual(self.run_one(payload, 201), (("?", "rejected"), 0))
        injected = dict(self.event, event_id="x result=created\nevent_id=y", device_id="g01-m1-d99")
        self.assertEqual(self.run_one(injected, 201), (("?", "rejected"), 0))


class GatewayMessages(unittest.TestCase):
    def test_thirty_two_messages_as_specified(self):
        try:
            gateway = load("w06_gateway", "tools/gateway_sim.py")
        except ImportError:
            self.skipTest("paho-mqtt is not installed")
        events = gateway.messages("g01-m1", "g01-m1-r1")
        self.assertEqual(len(events), 32)
        self.assertEqual(len({event["event_id"] for event in events}), 31)
        self.assertEqual(events[30], events[0])
        self.assertEqual((events[31]["event_id"], events[31]["device_id"]), ("g01-m1-r1-d99-01", "g01-m1-d99"))
        self.assertEqual(sorted({event["device_id"] for event in events[:30]}),
                         ["g01-m1-d%02d" % number for number in range(1, 11)])
        service = load("w06_service", "app/service.py")
        for event in events:
            service.validate_event(json.dumps(event).encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
