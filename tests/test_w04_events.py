"""Offline W4 contract checks: auth order, validation, duplicates, listing and the display page."""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
spec = importlib.util.spec_from_file_location("w04_service", ROOT / "app/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

REPORTER, OPERATOR = "reporter-test-token", "operator-test-token"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class EventContract(unittest.TestCase):
    tokens = {"reporter": REPORTER, "operator": OPERATOR}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        version = Path(self.tmp.name) / "version"
        version.write_text("b" * 40)
        self.server = service.make_server(version, port=0, tokens=self.tokens)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.tmp.cleanup()

    def call(self, method, path, token=None, body=None, content_type="application/json", raw=None):
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            request.add_header("Authorization", "Bearer " + token)
        if data is not None and content_type:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def call_json(self, *args, **kwargs):
        status, payload = self.call(*args, **kwargs)
        return status, json.loads(payload)

    def test_health_reports_auth_configured(self):
        status, body = self.call_json("GET", "/health")
        self.assertEqual(status, 200)
        self.assertIs(body["auth_configured"], True)

    def test_valid_fixture_is_stored_listed_and_fetched(self):
        event = fixture("event_valid.json")
        status, created = self.call_json("POST", "/events", REPORTER, event)
        self.assertEqual(status, 201)
        self.assertEqual(created["event_id"], event["event_id"])
        self.assertTrue(created["received_at"].endswith("Z"))
        status, listing = self.call_json("GET", "/events", OPERATOR)
        self.assertEqual(status, 200)
        self.assertEqual(listing["events"][0], created)
        status, single = self.call_json("GET", "/events/" + event["event_id"], OPERATOR)
        self.assertEqual((status, single), (200, created))
        self.assertEqual(self.call_json("GET", "/events/unknown-id", OPERATOR)[0], 404)

    def test_rejected_fixtures_name_the_field(self):
        for name, field in (("event_reject_no_timezone.json", "observed_at"),
                            ("event_reject_unknown_type.json", "type")):
            status, body = self.call_json("POST", "/events", REPORTER, fixture(name))
            self.assertEqual((status, body["field"]), (400, field), name)
            self.assertEqual(set(body), {"error", "field"})

    def test_identity_is_checked_before_content(self):
        bad = fixture("event_reject_no_timezone.json")
        self.assertEqual(self.call_json("POST", "/events", None, bad)[0], 401)
        self.assertEqual(self.call_json("POST", "/events", "wrong-token", bad)[0], 401)
        self.assertEqual(self.call_json("POST", "/events", OPERATOR, bad)[0], 403)
        self.assertEqual(self.call_json("GET", "/events", None)[0], 401)
        self.assertEqual(self.call_json("GET", "/events", REPORTER)[0], 403)
        self.assertEqual(self.call_json("GET", "/events/x", REPORTER)[0], 403)

    def test_resend_is_idempotent_and_changed_content_conflicts(self):
        """W5 rules: same id and content -> 200 with nothing added; same id, other content -> 409."""
        event = fixture("event_valid.json")
        status, created = self.call_json("POST", "/events", REPORTER, event)
        self.assertEqual(status, 201)
        self.assertEqual(self.call_json("POST", "/events", REPORTER, event), (200, created))
        status, body = self.call_json("POST", "/events", REPORTER, dict(event, note="changed"))
        self.assertEqual((status, body["field"]), (409, "event_id"))
        without_note = {k: v for k, v in event.items() if k != "note"}
        self.assertEqual(self.call_json("POST", "/events", REPORTER, without_note)[0], 409)
        self.assertEqual(self.call_json("GET", "/events", OPERATOR)[1]["events"], [created])

    def test_health_reports_no_database_offline(self):
        self.assertIs(self.call_json("GET", "/health")[1]["db_configured"], False)

    def test_field_rules(self):
        good = fixture("event_valid.json")
        cases = [
            (dict(good, extra="x"), "extra"),
            ({k: v for k, v in good.items() if k != "device_id"}, "device_id"),
            (dict(good, event_id=""), "event_id"),
            (dict(good, event_id="a" * 65), "event_id"),
            (dict(good, event_id="has space"), "event_id"),
            (dict(good, device_id="d" * 33), "device_id"),
            (dict(good, observed_at="2026-13-40T10:00:00+08:00"), "observed_at"),
            (dict(good, observed_at=20261006), "observed_at"),
            (dict(good, note=None), "note"),
            (dict(good, note="n" * 201), "note"),
        ]
        for body, field in cases:
            status, answer = self.call_json("POST", "/events", REPORTER, body)
            self.assertEqual((status, answer["field"]), (400, field), body)
        for stamp in ("2026-10-06T02:00:00Z", "2026-10-06T10:00:00.5+08:00", "2026-10-06T10:00-05:00"):
            body = dict(good, event_id="tz-" + str(abs(hash(stamp))), observed_at=stamp)
            self.assertEqual(self.call_json("POST", "/events", REPORTER, body)[0], 201, stamp)
        without_note = {k: v for k, v in good.items() if k != "note"}
        self.assertEqual(self.call_json("POST", "/events", REPORTER, dict(without_note, event_id="no-note"))[0], 201)

    def test_envelope_rules(self):
        good = fixture("event_valid.json")
        self.assertEqual(self.call_json("POST", "/events", REPORTER, good, content_type="text/plain")[0], 400)
        self.assertEqual(self.call_json("POST", "/events", REPORTER, raw=b"{not json")[0], 400)
        self.assertEqual(self.call_json("POST", "/events", REPORTER, raw=b"[1]")[0], 400)
        big = json.dumps(dict(good, note="n" * 200)).encode() + b" " * 4096
        status, body = self.call_json("POST", "/events", REPORTER, raw=big)
        self.assertEqual((status, body["error"]), (400, "body_too_large"))

    def test_list_returns_newest_fifty(self):
        good = fixture("event_valid.json")
        for number in range(55):
            self.assertEqual(self.call_json("POST", "/events", REPORTER, dict(good, event_id="n-%03d" % number))[0], 201)
        events = self.call_json("GET", "/events", OPERATOR)[1]["events"]
        self.assertEqual(len(events), 50)
        self.assertEqual((events[0]["event_id"], events[-1]["event_id"]), ("n-054", "n-005"))

    def test_responses_never_echo_tokens_or_bodies(self):
        marker = "SECRET-NOTE-MARKER"
        bad = dict(fixture("event_reject_no_timezone.json"), note=marker)
        for token in (None, "wrong-token", OPERATOR, REPORTER):
            payload = self.call("POST", "/events", token, bad)[1].decode("utf-8")
            for leaked in (marker, REPORTER, OPERATOR, "wrong-token"):
                self.assertNotIn(leaked, payload)

    def test_display_page_is_safe(self):
        status, payload = self.call("GET", "/")
        page = payload.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("textContent", page)
        for banned in ("innerHTML", "localStorage", "sessionStorage", "?token=", "document.cookie"):
            self.assertNotIn(banned, page)


class WithoutTokens(EventContract):
    """No secret file on the host yet: the service still starts and nobody can authenticate."""
    tokens = {"reporter": "", "operator": ""}

    def test_health_reports_auth_configured(self):
        self.assertIs(self.call_json("GET", "/health")[1]["auth_configured"], False)

    def test_everything_is_unauthorized(self):
        self.assertEqual(self.call_json("POST", "/events", REPORTER, fixture("event_valid.json"))[0], 401)
        self.assertEqual(self.call_json("GET", "/events", OPERATOR)[0], 401)
        self.assertEqual(self.call_json("GET", "/events", "")[0], 401)


# Only the two tests above apply without tokens; drop the inherited ones.
for _name in [n for n in dir(EventContract) if n.startswith("test_")]:
    if _name not in WithoutTokens.__dict__:
        setattr(WithoutTokens, _name, None)
