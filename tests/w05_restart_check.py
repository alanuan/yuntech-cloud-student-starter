#!/usr/bin/env python3
"""W5 T1: send one event, list, restart the service on the host, list again. Prints no tokens.

Usage: python tests/w05_restart_check.py
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from w04_matrix import FIXTURES, ROOT, call, current_base_url, read_env  # noqa: E402


def ssh(base, command):
    config = read_env(ROOT / ".local" / "config.env")
    host = base.split("//", 1)[1]
    return subprocess.run(
        [config.get("SSH_BIN", "ssh"), "-i", config["SSH_KEY"], "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=15", "-o", "LogLevel=ERROR", "ec2-user@" + host, command],
        capture_output=True, text=True, timeout=120, check=True).stdout


def listing(base, operator):
    status, text = call(base, "GET", "/events", operator)
    events = json.loads(text)["events"] if status == 200 else []
    return "HTTP %d，%d 筆 %s" % (status, len(events), [event["event_id"] for event in events])


def main():
    base = current_base_url()
    tokens = read_env(ROOT / ".local" / "app.env")
    event = json.loads((FIXTURES / "event_valid.json").read_text(encoding="utf-8"))
    event["event_id"] = event["event_id"].rsplit("-", 1)[0] + "-t1-" + datetime.now(timezone.utc).strftime("%H%M%S")
    status, _ = call(base, "POST", "/events", tokens["REPORTER_TOKEN"], event)
    print("送出 %s：HTTP %d" % (event["event_id"], status))
    print("重啟前：" + listing(base, tokens["OPERATOR_TOKEN"]))
    ssh(base, "sudo systemctl restart inspection")
    time.sleep(3)
    print("重啟後：" + listing(base, tokens["OPERATOR_TOKEN"]))


if __name__ == "__main__":
    main()
