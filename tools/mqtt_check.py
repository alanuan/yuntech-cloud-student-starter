#!/usr/bin/env python3
"""W6: connect as the gateway or the consumer, publish or subscribe once, and print the broker's reason codes.

  python tools/mqtt_check.py --as consumer --sub cxe/<code>/gw/events --wait 60
  python tools/mqtt_check.py --as gw --pub cxe/<code>/gw/events
Reason codes below 128 mean success; 135 means not authorised.
"""
import argparse
import json
import sys
import time

from iot_local import Session, code


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as", dest="role", choices=("gw", "consumer"), required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--pub", metavar="TOPIC")
    action.add_argument("--sub", metavar="TOPIC_FILTER")
    parser.add_argument("--wait", type=int, default=10, help="seconds to keep listening after subscribing")
    args = parser.parse_args()

    def on_message(client, userdata, message):
        print("recv topic=%s payload=%s" % (message.topic, message.payload.decode("utf-8", "replace")), flush=True)

    session = Session(args.role, on_message)
    print("connack=%d" % session.connack, flush=True)
    if session.connack >= 128:
        return 1
    if args.pub:
        answer = session.publish(args.pub, json.dumps({"check": "t2", "code": code()}, separators=(",", ":")))
        print("puback=%s" % ("none" if answer is None else answer), flush=True)
    else:
        answer = session.subscribe(args.sub)
        print("suback=%s" % ("none" if answer is None else answer), flush=True)
        if answer is not None and answer < 128:
            time.sleep(args.wait)
    session.close()
    return 0 if answer is not None and answer < 128 else 1


if __name__ == "__main__":
    sys.exit(main())
