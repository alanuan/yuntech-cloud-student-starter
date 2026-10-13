#!/usr/bin/env python3
"""W6: two IoT identities (gateway, consumer), each with one certificate and one exact-resource policy.

Spec: labs/06-iot/README.md ("規格 A"). Every created ID is written to .local/resources.json at once,
so a second run skips what exists: no second certificate, no overwritten private key.
Private keys go straight from the AWS CLI into .local/iot/; they are never printed or captured here.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
IOT = LOCAL / "iot"
RESOURCES = LOCAL / "resources.json"
ROOT_CA_URL = "https://www.amazontrust.com/repository/AmazonRootCA1.pem"


def stop(message):
    sys.exit("STOP: " + message)


def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and not name.startswith("#"):
            values[name] = value.strip().strip('"')
    return values


CONFIG = read_env(LOCAL / "config.env") if (LOCAL / "config.env").exists() else {}
PROFILE = CONFIG.get("LAB_PROFILE", "learnerlab")
REGION = CONFIG.get("LAB_REGION", "us-east-1")


def aws(*args):
    result = subprocess.run(["aws", "--profile", PROFILE, "--region", REGION, "--no-cli-pager", "--output", "json", *args],
                            capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        # Only the service's own error line; never the full stderr.
        found = re.search(r"An error occurred \(([A-Za-z0-9._-]+)\)[^:]*: (.*)", result.stderr)
        stop("%s %s failed: %s" % (args[0], args[1], "%s: %s" % found.groups() if found else "CommandFailed"))
    return json.loads(result.stdout or "{}")


def owner_only(path):
    """Mode 700/600; on Windows an ACL that only the current user can use."""
    os.chmod(path, 0o700 if path.is_dir() else 0o600)
    if os.name == "nt":
        grant = os.environ["USERNAME"] + (":(OI)(CI)F" if path.is_dir() else ":(R,W)")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant], capture_output=True, check=True)


def load():
    return json.loads(RESOURCES.read_text(encoding="utf-8"))


def record(**values):
    resources = load()
    resources.update(values)
    RESOURCES.write_text(json.dumps(resources, indent=2) + "\n", encoding="utf-8", newline="\n")


def statements(code, account):
    arn = "arn:aws:iot:%s:%s:" % (REGION, account)
    topic = "cxe/%s/gw/events" % code
    return {
        "gw": [("iot:Connect", arn + "client/gw-" + code), ("iot:Publish", arn + "topic/" + topic)],
        "consumer": [("iot:Connect", arn + "client/consumer-" + code), ("iot:Subscribe", arn + "topicfilter/" + topic),
                     ("iot:Receive", arn + "topic/" + topic)],
    }


def main():
    if not RESOURCES.exists():
        stop("missing .local/resources.json")
    res = load()
    code = "%s-%s" % (res["group"], res["owner"])
    if not re.fullmatch(r"[a-z0-9]+-[a-z0-9]+", code):
        stop("group and owner in .local/resources.json must be lower-case letters and digits")
    account = aws("sts", "get-caller-identity")["Account"]

    print("Account ...%s, profile %s, region %s, code %s" % (account[-4:], PROFILE, REGION, code))
    print("Will create (items already recorded in .local/resources.json are skipped):")
    print("  2 IoT certificates, active: gateway and consumer; private keys only in .local/iot/")
    print("  policy cxe-%s-gw: Connect as gw-%s, Publish to cxe/%s/gw/events" % (code, code, code))
    print("  policy cxe-%s-consumer: Connect as consumer-%s, Subscribe and Receive on the same topic" % (code, code))
    print("Cost: certificates and policies are free while idle; messages this week stay far below one cent.")
    print("Network exposure: none added; clients connect out to the account's IoT endpoint on 8883.")
    print("Removal: detach each policy, deactivate and delete each certificate, delete each policy, by recorded ID.")
    try:
        answer = input("Type create to continue: ").strip()
    except EOFError:
        answer = ""
    if answer != "create":
        stop("cancelled; nothing was changed")

    IOT.mkdir(exist_ok=True)
    owner_only(IOT)
    endpoint = aws("iot", "describe-endpoint", "--endpoint-type", "iot:Data-ATS")["endpointAddress"]
    (IOT / "endpoint").write_text(endpoint + "\n", encoding="utf-8", newline="\n")
    ca = IOT / "AmazonRootCA1.pem"
    if not ca.exists():
        with urllib.request.urlopen(ROOT_CA_URL, timeout=30) as response:
            data = response.read()
        if not data.startswith(b"-----BEGIN CERTIFICATE-----"):
            stop("the Root CA download is not a certificate")
        ca.write_bytes(data)
        owner_only(ca)

    for role, rules in statements(code, account).items():
        id_key, policy_key, policy = "iot_%s_cert_id" % role, "iot_%s_policy" % role, "cxe-%s-%s" % (code, role)
        cert_file, key_file = IOT / (role + ".cert.pem"), IOT / (role + ".private.key")
        if id_key not in load():
            if cert_file.exists() or key_file.exists():
                stop("%s files exist but no certificate ID is recorded; do not overwrite, check them by hand" % role)
            # The CLI writes the certificate and the private key to the two files itself; --query keeps
            # everything except the certificate ID out of its output.
            created = aws("iot", "create-keys-and-certificate", "--set-as-active", "--certificate-pem-outfile", str(cert_file),
                          "--private-key-outfile", str(key_file), "--query", "certificateId")
            record(**{id_key: created})
            owner_only(cert_file)
            owner_only(key_file)
        if policy_key not in load():
            document = {"Version": "2012-10-17",
                        "Statement": [{"Effect": "Allow", "Action": action, "Resource": resource} for action, resource in rules]}
            # Through a file: a JSON argument does not survive every shell's quoting.
            policy_file = IOT / (policy + ".json")
            policy_file.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
            aws("iot", "create-policy", "--policy-name", policy, "--policy-document", "file://" + str(policy_file.resolve()))
            record(**{policy_key: policy})
        attached_key = "iot_%s_policy_attached" % role
        if not load().get(attached_key):
            arn = aws("iot", "describe-certificate", "--certificate-id", load()[id_key])["certificateDescription"]["certificateArn"]
            aws("iot", "attach-policy", "--policy-name", policy, "--target", arn)
            record(**{attached_key: True})

    print("Read back:")
    for role in ("gw", "consumer"):
        res = load()
        description = aws("iot", "describe-certificate", "--certificate-id", res["iot_%s_cert_id" % role])["certificateDescription"]
        document = aws("iot", "get-policy", "--policy-name", res["iot_%s_policy" % role])["policyDocument"]
        print("  %-8s certificate ...%s  status=%s  policy=%s  wildcard_in_policy=%s" % (
            role, description["certificateId"][-4:], description["status"], res["iot_%s_policy" % role],
            str("*" in document).lower()))
        if description["status"] != "ACTIVE" or "*" in document:
            stop("the %s identity is not as specified" % role)
    print("  endpoint, Root CA, certificates and private keys are in .local/iot/ (not shown)")


if __name__ == "__main__":
    main()
