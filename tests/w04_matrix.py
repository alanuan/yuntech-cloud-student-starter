#!/usr/bin/env python3
"""W4 rejection matrix: 7 rows against the deployed host. Prints no tokens and no request headers.

Usage: python tests/w04_matrix.py [--base-url http://HOST]
Without --base-url the host's CURRENT public address is looked up from .local/resources.json.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and not name.startswith("#"):
            values[name] = value.strip().strip('"')
    return values


def current_base_url():
    config = read_env(ROOT / ".local" / "config.env")
    instance = json.loads((ROOT / ".local" / "resources.json").read_text(encoding="utf-8"))["instance_id"]
    address = subprocess.run(
        ["aws", "--profile", config.get("LAB_PROFILE", "learnerlab"), "--region", config.get("LAB_REGION", "us-east-1"),
         "--no-cli-pager", "ec2", "describe-instances", "--instance-ids", instance,
         "--query", "Reservations[0].Instances[0].PublicIpAddress", "--output", "text"],
        capture_output=True, text=True, timeout=60, check=True).stdout.strip()
    if not address or address == "None":
        sys.exit("STOP: the host has no public address; is it running?")
    return "http://" + address


def call(base, method, path, token=None, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    base = (parser.parse_args().base_url or current_base_url()).rstrip("/")
    tokens = read_env(ROOT / ".local" / "app.env")
    reporter, operator = tokens["REPORTER_TOKEN"], tokens["OPERATOR_TOKEN"]

    valid = json.loads((FIXTURES / "event_valid.json").read_text(encoding="utf-8"))
    no_zone = json.loads((FIXTURES / "event_reject_no_timezone.json").read_text(encoding="utf-8"))
    # A fresh serial per run: the fixture's "<group>-<member>-" prefix plus the UTC time.
    prefix = valid["event_id"].rsplit("-", 1)[0]
    serial = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
    valid["event_id"] = prefix + "-" + serial
    no_zone["event_id"] = prefix + "-" + serial + "-tz"

    health = json.loads(call(base, "GET", "/health")[1])
    print("version:", health.get("version"))
    print("auth_configured:", health.get("auth_configured"))

    rows = [
        (1, "reporter 送一筆合法事件", 201, ("POST", "/events", reporter, valid)),
        (2, "同上，不帶權杖", 401, ("POST", "/events", None, valid)),
        (3, "operator 權杖送事件", 403, ("POST", "/events", operator, valid)),
        (4, "reporter，observed_at 沒有時區", 400, ("POST", "/events", reporter, no_zone)),
        (5, "reporter，再送一次 #1", 409, ("POST", "/events", reporter, valid)),
        (6, "reporter 權杖讀清單", 403, ("GET", "/events", reporter, None)),
        (7, "operator 權杖讀清單", 200, ("GET", "/events", operator, None)),
    ]
    failed = 0
    for number, what, expected, request in rows:
        status, text = call(base, *request)
        ok = status == expected
        if number == 7 and ok:
            ok = any(event.get("event_id") == valid["event_id"] for event in json.loads(text)["events"])
        failed += not ok
        print("#%d %s | 預期 %d | 實際 %d | %s\n   %s" % (number, what, expected, status, "符合" if ok else "不符", text))
    print("結果: %d/7 符合" % (7 - failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
