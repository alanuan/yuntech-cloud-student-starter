#!/usr/bin/env python3
"""W6 simulated gateway: 32 messages for one run, each sent only after the previous PUBACK came back.

  python tools/gateway_sim.py --code <code> --run r1
10 devices x 3 events, then the very first event again unchanged, then one event from <code>-d99.
"""
import argparse
from datetime import datetime, timezone
import json
import re
import sys

from iot_local import ROOT, Session, stop

PREFIX = re.compile(r"^[A-Za-z0-9-]{1,40}$")


def messages(code, prefix):
    events = []
    for device in range(1, 11):
        for serial in range(1, 4):
            events.append({"event_id": "%s-d%02d-%02d" % (prefix, device, serial),
                           "device_id": "%s-d%02d" % (code, device),
                           "observed_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                           "type": "status"})
    events.append(events[0])  # resent as is: the API answers 200 and adds nothing
    events.append(dict(events[0], event_id=prefix + "-d99-01", device_id=code + "-d99"))  # not on the device list
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    prefix = "%s-%s" % (args.code, args.run)
    if not PREFIX.fullmatch(prefix):
        stop("the prefix must match ^[A-Za-z0-9-]{1,40}$")
    topic = "cxe/%s/gw/events" % args.code
    session = Session("gw")
    if session.connack >= 128:
        stop("connack=%d" % session.connack)
    acknowledged = 0
    for event in messages(args.code, prefix):
        answer = session.publish(topic, json.dumps(event, separators=(",", ":")))
        if answer is not None and answer < 128:
            acknowledged += 1
        else:
            print("event_id=%s puback=%s" % (event["event_id"], "none" if answer is None else answer))
    session.close()
    (ROOT / ".local" / "w06-last-run.json").write_text(
        json.dumps({"prefix": prefix, "puback_ok": acknowledged, "sent": 32}) + "\n", encoding="utf-8", newline="\n")
    print("prefix=%s puback_ok=%d/32" % (prefix, acknowledged))
    return 0 if acknowledged == 32 else 1


if __name__ == "__main__":
    sys.exit(main())
