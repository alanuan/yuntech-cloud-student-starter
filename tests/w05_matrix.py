#!/usr/bin/env python3
"""W5 idempotency matrix: 5 rows against the deployed host. Prints no tokens, passwords or headers.

Usage: python tests/w05_matrix.py
Row 4 restarts the service on the host over SSH; row 5 counts the row with psql on the host.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from w04_matrix import FIXTURES, ROOT, call, current_base_url, read_env  # noqa: E402

# The password is read from the host's secret file into the environment there; it is never an argument.
COUNT_SCRIPT = r"""sudo bash -c 'set -a; . /etc/inspection/app.env; set +a
  PGPASSWORD="$DB_PASSWORD" psql -At "host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem" \
    -v event_id="%s"' <<'SQL'
SELECT count(*) FROM events WHERE event_id = :'event_id';
SQL
"""


def ssh(base, script):
    config = read_env(ROOT / ".local" / "config.env")
    host = base.split("//", 1)[1]
    result = subprocess.run(
        [config.get("SSH_BIN", "ssh"), "-i", config["SSH_KEY"], "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=15", "-o", "LogLevel=ERROR", "ec2-user@" + host, "bash -s"],
        input=script.encode("utf-8"), capture_output=True, timeout=120, check=False)
    if result.returncode:
        sys.exit("STOP: the command on the host failed (exit %d)" % result.returncode)
    return result.stdout.decode("utf-8").strip()


def main():
    base = current_base_url()
    tokens = read_env(ROOT / ".local" / "app.env")
    reporter, operator = tokens["REPORTER_TOKEN"], tokens["OPERATOR_TOKEN"]
    event = json.loads((FIXTURES / "event_valid.json").read_text(encoding="utf-8"))
    event["event_id"] = event["event_id"].rsplit("-", 1)[0] + "-" + datetime.now(timezone.utc).strftime("%m%d%H%M%S")
    changed = dict(event, note="a different note for the same event_id")

    health = json.loads(call(base, "GET", "/health")[1])
    print("version:", health.get("version"))
    print("db_configured:", health.get("db_configured"))

    failed = 0

    def row(number, what, expected, status, text, ok=None):
        nonlocal failed
        ok = (status == expected) if ok is None else ok
        failed += not ok
        print("#%d %s | 預期 %s | 實際 %s | %s\n   %s" % (number, what, expected, status, "符合" if ok else "不符", text))

    status, first = call(base, "POST", "/events", reporter, event)
    row(1, "送一筆新事件", 201, status, first)
    status, text = call(base, "POST", "/events", reporter, event)
    row(2, "原樣重送", 200, status, text, status == 200 and text == first)
    status, text = call(base, "POST", "/events", reporter, changed)
    row(3, "同 ID、note 不同", 409, status, text)

    ssh(base, "sudo systemctl restart inspection\n")
    for _ in range(10):
        time.sleep(2)
        try:
            status, text = call(base, "GET", "/events/" + event["event_id"], operator)
        except OSError:
            continue
        if status != 502:
            break
    row(4, "sudo systemctl restart inspection 後查 #1", "200 還在", status, text, status == 200 and text == first)

    count = ssh(base, COUNT_SCRIPT % event["event_id"])
    row(5, "在 EC2 上用 psql 查 #1 的筆數", 1, count, "count = " + count, count == "1")
    print("結果: %d/5 符合" % (5 - failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
