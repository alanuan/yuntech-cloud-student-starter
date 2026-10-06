#!/usr/bin/env python3
"""W5: private subnets, DB subnet group, SG-db and one private PostgreSQL RDS for the host.

Spec: labs/05-private-rds/README.md ("db-up.sh 規格（五條）"). Every created ID is written to
.local/resources.json at once, so a second run resumes instead of creating a second database.
The password goes to .local/db.env only: it is never printed and never a command-line argument.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
RESOURCES = LOCAL / "resources.json"
DB_ENV = LOCAL / "db.env"
DB_NAME = "inspection"
DB_USER = "inspection"


def stop(message):
    sys.exit("STOP: " + message)


def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and not name.startswith("#"):
            values[name] = value.strip().strip('"')
    return values


def private_write(path, content):
    """Owner-only file with LF line endings (mode 600; an owner-only ACL on Windows)."""
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        os.chmod(path, 0o600)
        stream.write(content)
    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", os.environ["USERNAME"] + ":(R,W)"],
                       capture_output=True, check=True)


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


def load():
    return json.loads(RESOURCES.read_text(encoding="utf-8"))


def record(**values):
    resources = load()
    resources.update(values)
    RESOURCES.write_text(json.dumps(resources, indent=2) + "\n", encoding="utf-8", newline="\n")


def tags(resources, name):
    pairs = {"Name": name, "course": resources.get("course", "yuntech-115-1"), "week": "w05",
             "group": resources["group"], "owner": resources["owner"]}
    return [{"Key": key, "Value": value} for key, value in pairs.items()]


def tag_spec(resources, kind, name):
    return "ResourceType=%s,Tags=[%s]" % (kind, ",".join("{Key=%(Key)s,Value=%(Value)s}" % tag for tag in tags(resources, name)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cidr-a", default="172.31.200.0/24")
    parser.add_argument("--cidr-b", default="172.31.201.0/24")
    args = parser.parse_args()
    if not RESOURCES.exists():
        stop("missing .local/resources.json; it must record the host (instance_id, security_group_id)")
    res = load()
    suffix = "%s-%s" % (res["group"], res["owner"])
    db_id = res.get("db_instance_id", "inspection-" + suffix)

    host = aws("ec2", "describe-instances", "--instance-ids", res["instance_id"])["Reservations"][0]["Instances"][0]
    vpc, host_az = host["VpcId"], host["Placement"]["AvailabilityZone"]
    host_sgs = [group["GroupId"] for group in host["SecurityGroups"]]
    if res["security_group_id"] not in host_sgs:
        stop("the host no longer uses the recorded security group")
    host_sg = res["security_group_id"]

    # Rule 1: two /24 inside the VPC that overlap no existing subnet, in two different AZs.
    vpc_net = ipaddress.ip_network(aws("ec2", "describe-vpcs", "--vpc-ids", vpc)["Vpcs"][0]["CidrBlock"])
    existing = aws("ec2", "describe-subnets", "--filters", "Name=vpc-id,Values=" + vpc)["Subnets"]
    mine = {res.get("db_subnet_a_id"), res.get("db_subnet_b_id")}
    wanted = [ipaddress.ip_network(args.cidr_a), ipaddress.ip_network(args.cidr_b)]
    for net in wanted:
        if net.prefixlen != 24 or not net.subnet_of(vpc_net):
            stop("%s must be a /24 inside %s" % (net, vpc_net))
        for subnet in existing:
            if subnet["SubnetId"] not in mine and net.overlaps(ipaddress.ip_network(subnet["CidrBlock"])):
                stop("%s overlaps existing subnet %s" % (net, subnet["SubnetId"]))
    options = aws("rds", "describe-orderable-db-instance-options", "--engine", "postgres",
                  "--db-instance-class", "db.t3.micro", "--max-items", "1")["OrderableDBInstanceOptions"]
    zones = sorted(zone["Name"] for zone in options[0]["AvailabilityZones"]) if options else []
    if host_az not in zones or len(zones) < 2:
        stop("db.t3.micro PostgreSQL is not offered in the host AZ plus one more")
    az_a, az_b = host_az, next(zone for zone in zones if zone != host_az)

    print("Account profile %s, region %s, VPC %s" % (PROFILE, REGION, vpc))
    print("Will create (steps already recorded in .local/resources.json are skipped):")
    print("  route table with the local route only, associated with both new subnets")
    print("  private subnet %s in %s; private subnet %s in %s" % (wanted[0], az_a, wanted[1], az_b))
    print("  DB subnet group inspection-%s; SG-db allowing TCP 5432 from %s only" % (suffix, host_sg))
    print("  RDS %s: PostgreSQL, db.t3.micro, 20 GiB gp3, encrypted, single AZ, NOT public, database %s" % (db_id, DB_NAME))
    print("Cost: the instance bills per hour while running; its storage bills even while stopped.")
    print("Removal: delete the RDS instance, then SG-db, subnet group, subnets and route table, by recorded ID.")
    try:
        answer = input("Type create to continue: ").strip()
    except EOFError:
        answer = ""
    if answer != "create":
        stop("cancelled; nothing was changed")

    if "db_route_table_id" not in res:
        table = aws("ec2", "create-route-table", "--vpc-id", vpc, "--tag-specifications",
                    tag_spec(res, "route-table", "inspection-db-private-" + suffix))["RouteTable"]
        record(db_route_table_id=table["RouteTableId"])
    for key, net, zone in (("db_subnet_a_id", wanted[0], az_a), ("db_subnet_b_id", wanted[1], az_b)):
        if key not in load():
            subnet = aws("ec2", "create-subnet", "--vpc-id", vpc, "--cidr-block", str(net), "--availability-zone", zone,
                         "--tag-specifications", tag_spec(res, "subnet", "inspection-db-%s-%s" % (zone, suffix)))["Subnet"]
            record(**{key: subnet["SubnetId"]})
        assoc_key = key.replace("_id", "_association_id")
        if assoc_key not in load():
            assoc = aws("ec2", "associate-route-table", "--route-table-id", load()["db_route_table_id"],
                        "--subnet-id", load()[key])
            record(**{assoc_key: assoc["AssociationId"]})
    res = load()
    routes = aws("ec2", "describe-route-tables", "--route-table-ids", res["db_route_table_id"])["RouteTables"][0]["Routes"]
    if [route.get("GatewayId") for route in routes] != ["local"]:
        stop("the private route table must contain the local route only")

    # Rule 2: subnet group, and SG-db whose only inbound rule is 5432 from the host's SG.
    if "db_subnet_group_name" not in res:
        aws("rds", "create-db-subnet-group", "--db-subnet-group-name", "inspection-" + suffix,
            "--db-subnet-group-description", "W5 private subnets for the inspection database",
            "--subnet-ids", res["db_subnet_a_id"], res["db_subnet_b_id"],
            "--tags", *["Key=%(Key)s,Value=%(Value)s" % tag for tag in tags(res, "inspection-" + suffix)])
        record(db_subnet_group_name="inspection-" + suffix)
    if "db_security_group_id" not in res:
        group = aws("ec2", "create-security-group", "--group-name", "inspection-db-" + suffix, "--vpc-id", vpc,
                    "--description", "W5 PostgreSQL 5432 from the inspection host SG only",
                    "--tag-specifications", tag_spec(res, "security-group", "inspection-db-" + suffix))
        record(db_security_group_id=group["GroupId"])
    res = load()
    if not res.get("db_security_group_rule_id"):
        rule = aws("ec2", "authorize-security-group-ingress", "--group-id", res["db_security_group_id"], "--ip-permissions",
                   "IpProtocol=tcp,FromPort=5432,ToPort=5432,UserIdGroupPairs=[{GroupId=%s}]" % host_sg)
        record(db_security_group_rule_id=rule["SecurityGroupRules"][0]["SecurityGroupRuleId"])

    # Rule 4: the password is generated here and stored only in .local/db.env.
    if not DB_ENV.exists():
        private_write(DB_ENV, "DB_NAME=%s\nDB_USER=%s\nDB_PORT=5432\nDB_PASSWORD=%s\n"
                      % (DB_NAME, DB_USER, secrets.token_urlsafe(24)))

    # Rule 3: the RDS instance. The request, password included, goes through a private file.
    res = load()
    if "db_instance_id" not in res:
        request = {
            "DBInstanceIdentifier": db_id, "Engine": "postgres", "DBInstanceClass": "db.t3.micro",
            "AllocatedStorage": 20, "StorageType": "gp3", "StorageEncrypted": True, "MultiAZ": False,
            "PubliclyAccessible": False, "DBName": DB_NAME, "MasterUsername": DB_USER,
            "MasterUserPassword": read_env(DB_ENV)["DB_PASSWORD"], "AvailabilityZone": az_a,
            "DBSubnetGroupName": res["db_subnet_group_name"], "VpcSecurityGroupIds": [res["db_security_group_id"]],
            "Tags": tags(res, db_id),
        }
        request_file = LOCAL / ("rds-request-%d.json" % os.getpid())
        try:
            private_write(request_file, json.dumps(request))
            aws("rds", "create-db-instance", "--cli-input-json", "file://" + str(request_file.resolve()))
        finally:
            request_file.unlink(missing_ok=True)
        record(db_instance_id=db_id, db_create_started=int(time.time()))

    # Rule 5: wait, then read back.
    started = load().get("db_create_started", int(time.time()))
    while True:
        db = aws("rds", "describe-db-instances", "--db-instance-identifier", db_id)["DBInstances"][0]
        if db["DBInstanceStatus"] == "available":
            break
        if time.time() - started > 30 * 60:
            stop("RDS is still %s after 30 minutes; do not rerun blindly" % db["DBInstanceStatus"])
        print("  RDS status: %s (%.1f min)" % (db["DBInstanceStatus"], (time.time() - started) / 60), flush=True)
        time.sleep(30)
    if "db_available_minutes" not in load():
        record(db_available_minutes=round((time.time() - started) / 60, 1), db_resource_id=db["DbiResourceId"])
    env = read_env(DB_ENV)
    if env.get("DB_HOST") != db["Endpoint"]["Address"]:
        env["DB_HOST"] = db["Endpoint"]["Address"]
        private_write(DB_ENV, "".join("%s=%s\n" % item for item in env.items()))
    if db["PubliclyAccessible"]:
        stop("the database is publicly accessible; fix this before going on")
    print("Read back:")
    print("  RDS ...%s  status=%s  PubliclyAccessible=%s" % (db["DbiResourceId"][-4:], db["DBInstanceStatus"],
                                                              str(db["PubliclyAccessible"]).lower()))
    print("  engine=%s %s  class=%s  storage=%d GiB %s  encrypted=%s  multi_az=%s  az=%s" % (
        db["Engine"], db["EngineVersion"], db["DBInstanceClass"], db["AllocatedStorage"], db["StorageType"],
        str(db["StorageEncrypted"]).lower(), str(db["MultiAZ"]).lower(), db["AvailabilityZone"]))
    print("  creation to available: about %s minutes" % load()["db_available_minutes"])
    print("  endpoint and password are in .local/db.env (not shown)")


if __name__ == "__main__":
    main()
