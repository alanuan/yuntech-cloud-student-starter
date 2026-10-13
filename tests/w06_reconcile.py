#!/usr/bin/env python3
"""W6 reconciliation for one run: PUBACKs, consumer counts, database rows and the device filter.

Usage: python tests/w06_reconcile.py --code <code> --run r1
Prints no tokens, passwords or keys. The prefix is checked by pattern and reaches psql only through -v.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from w04_matrix import ROOT, call, current_base_url, read_env  # noqa: E402

PREFIX = re.compile(r"^[A-Za-z0-9-]{1,40}$")
RESULTS = ("created", "duplicate", "rejected", "error")
# The prefix was matched against PREFIX, so it holds no shell or pattern characters; grep -F takes it literally.
COUNT_SCRIPT = """sudo journalctl -u inspection-consumer -o cat --no-pager \\
  | grep -F 'event_id=%s-' | grep -o 'result=[a-z]*' | sort | uniq -c
"""
# The password is read from the host's secret file into the environment there; it is never an argument.
DB_SCRIPT = r"""sudo bash -c 'set -a; . /etc/inspection/app.env; set +a
  PGPASSWORD="$DB_PASSWORD" psql -At "host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem" \
    -v prefix="%s-"' <<'SQL'
SELECT count(*) FROM events WHERE left(event_id, length(:'prefix')) = :'prefix';
SQL
"""


def ssh(base, script):
    config = read_env(ROOT / ".local" / "config.env")
    result = subprocess.run(
        [config.get("SSH_BIN", "ssh"), "-i", config["SSH_KEY"], "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=15", "-o", "LogLevel=ERROR", "ec2-user@" + base.split("//", 1)[1], "bash -s"],
        input=script.encode("utf-8"), capture_output=True, timeout=120, check=False)
    # grep finding nothing ends with 1; anything else is a real failure.
    if result.returncode not in (0, 1):
        sys.exit("STOP: the command on the host failed (exit %d)" % result.returncode)
    return result.stdout.decode("utf-8").strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    prefix = "%s-%s" % (args.code, args.run)
    if not PREFIX.fullmatch(prefix):
        sys.exit("STOP: the prefix must match ^[A-Za-z0-9-]{1,40}$")
    base = current_base_url()

    health = json.loads(call(base, "GET", "/health")[1])
    print("version:", health.get("version"))
    print("db_configured:", health.get("db_configured"))
    print("本次前綴:", prefix)

    last = json.loads((ROOT / ".local" / "w06-last-run.json").read_text(encoding="utf-8"))
    if last.get("prefix") != prefix:
        sys.exit("STOP: .local/w06-last-run.json is for %s, not %s" % (last.get("prefix"), prefix))
    puback = last["puback_ok"]

    counts, waited = {}, 0
    while True:
        counts = dict.fromkeys(RESULTS, 0)
        for line in ssh(base, COUNT_SCRIPT % prefix).splitlines():
            number, _, name = line.strip().partition(" result=")
            if name in counts:
                counts[name] = int(number)
        if sum(counts.values()) >= 32 or waited >= 60:
            break
        time.sleep(5)
        waited += 5
    in_database = ssh(base, DB_SCRIPT % prefix)

    device = args.code + "-d01"
    status, text = call(base, "GET", "/events?device_id=" + device, read_env(ROOT / ".local" / "app.env")["OPERATOR_TOKEN"])
    events = json.loads(text).get("events", []) if status == 200 else []
    other_devices = sum(1 for event in events if event.get("device_id") != device)
    mine = [event["event_id"] for event in events if event["event_id"].startswith(prefix + "-")]

    consumer = "／".join(str(counts[name]) for name in RESULTS)
    rows = [
        ("閘道收到的 PUBACK", "32", str(puback)),
        ("consumer 計數：建立／重複／拒絕／錯誤", "30／1／1／0", consumer),
        ("資料庫本次前綴筆數", "30", in_database),
        ("GET /events?device_id=%s 本次前綴筆數" % device, "3", str(len(mine))),
    ]
    failed = 0
    for what, expected, actual in rows:
        failed += expected != actual
        print("%s | 預期 %s | 實際 %s | %s" % (what, expected, actual, "符合" if expected == actual else "不符"))
    print("篩選回應: HTTP %d，共 %d 筆，其中其他設備 %d 筆；本次前綴: %s" % (status, len(events), other_devices, sorted(mine)))
    print("consumer 計數等待: %d 秒" % waited)
    print("結果: %d/4 符合" % (4 - failed))
    return 1 if failed or other_devices else 0


if __name__ == "__main__":
    sys.exit(main())
