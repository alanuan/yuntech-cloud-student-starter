#!/usr/bin/env bash
# Deploy ONE committed version to the existing host over SSH, then install the secret file.
# Spec: labs/04-web-api/README.md ("權杖與 deploy.sh"). Runs in Codespaces bash and Git Bash on Windows.
#
# W6 (labs/06-iot/README.md, "規格 E"): also places the consumer's certificate, private key and Root CA.
# Reads (never prints) .local/app.env. Settings come from .local/config.env:
#   LAB_PROFILE (default learnerlab), LAB_REGION (default us-east-1), SSH_KEY (private key path),
#   SSH_BIN (default ssh), PYTHON (optional interpreter path). The host is the instance_id recorded in .local/resources.json.
set -euo pipefail
cd "$(dirname "$0")/.."

stop() { echo "STOP: $*" >&2; exit 1; }

CONFIG=.local/config.env
SECRET=.local/app.env
RESOURCES=.local/resources.json
[ -f "$CONFIG" ] || stop "missing $CONFIG"
[ -f "$SECRET" ] || stop "missing $SECRET; generate the two tokens first"
[ -f "$RESOURCES" ] || stop "missing $RESOURCES; it must record instance_id"
# shellcheck disable=SC1090
. "$CONFIG"
LAB_PROFILE=${LAB_PROFILE:-learnerlab}
LAB_REGION=${LAB_REGION:-us-east-1}
SSH_BIN=${SSH_BIN:-ssh}
[ -n "${SSH_KEY:-}" ] || stop "SSH_KEY is not set in $CONFIG"

PY=
for candidate in "${PYTHON:-}" python3 python py; do
    [ -n "$candidate" ] || continue
    if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then PY=$candidate; break; fi
done
[ -n "$PY" ] || stop "Python 3 not found"

lab_aws() { aws --profile "$LAB_PROFILE" --region "$LAB_REGION" --no-cli-pager "$@" | tr -d '\r'; }

# 1. Only a committed version is deployed.
[ -z "$(git status --porcelain -- app deploy/nginx.conf deploy/make_user_data.py)" ] \
    || stop "uncommitted changes under app/ or deploy/; commit first (only committed files are packaged)"
SHA=$(git rev-parse --verify 'HEAD^{commit}')

# The secret file must be private and well formed before anything is sent.
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) echo "Note: Windows file system, POSIX mode check skipped; $SECRET relies on your profile ACL." ;;
    *) [ "$(stat -c '%a' "$SECRET")" = 600 ] || stop "$SECRET must be mode 600" ;;
esac
# W5: the database settings written by db-up.sh travel in the same secret file.
DB_SECRET=.local/db.env
if [ -f "$DB_SECRET" ]; then
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) ;;
        *) [ "$(stat -c '%a' "$DB_SECRET")" = 600 ] || stop "$DB_SECRET must be mode 600" ;;
    esac
    SECRET_LINES=$(cat "$SECRET" "$DB_SECRET" | tr -d '\r' | sed '/^$/d')
else
    SECRET_LINES=$(tr -d '\r' < "$SECRET" | sed '/^$/d')
fi
# W6: once iot-up.sh has run, the consumer's settings join the secret file and its three files are sent too.
IOT_DIR=.local/iot
IOT_REMOTE=/etc/inspection/iot
IOT_FILES="AmazonRootCA1.pem consumer.cert.pem consumer.private.key"
WANT_IOT=false
if [ -f "$IOT_DIR/consumer.private.key" ]; then
    WANT_IOT=true
    for name in endpoint $IOT_FILES; do [ -s "$IOT_DIR/$name" ] || stop "missing $IOT_DIR/$name; rerun deploy/iot-up.sh"; done
    CODE=$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["iot_consumer_policy"][4:-9])' "$RESOURCES" | tr -d '\r')
    [[ "$CODE" =~ ^[a-z0-9]+-[a-z0-9]+$ ]] || stop "iot_consumer_policy in $RESOURCES is not cxe-<code>-consumer"
    DEVICES=$(for n in 01 02 03 04 05 06 07 08 09 10; do printf '%s-d%s,' "$CODE" "$n"; done)
    SECRET_LINES=$(printf '%s\n' "$SECRET_LINES" \
        "IOT_ENDPOINT=$(tr -d '\r\n' < "$IOT_DIR/endpoint")" "IOT_TOPIC=cxe/$CODE/gw/events" \
        "IOT_CLIENT_ID=consumer-$CODE" "IOT_DEVICE_IDS=${DEVICES%,}" "IOT_CA=$IOT_REMOTE/AmazonRootCA1.pem" \
        "IOT_CERT=$IOT_REMOTE/consumer.cert.pem" "IOT_KEY=$IOT_REMOTE/consumer.private.key")
fi
if grep -qvE '^[A-Z][A-Z0-9_]*=[^[:space:]]+$' <<<"$SECRET_LINES"; then stop "$SECRET has a line that is not KEY=VALUE"; fi
for name in REPORTER_TOKEN OPERATOR_TOKEN; do
    grep -qE "^$name=.{20,}$" <<<"$SECRET_LINES" || stop "$SECRET lacks $name"
done

INSTANCE=$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["instance_id"])' "$RESOURCES" | tr -d '\r')
INFO=$(lab_aws ec2 describe-instances --instance-ids "$INSTANCE" \
    --query 'Reservations[0].Instances[0].[State.Name,PublicIpAddress]' --output text) \
    || stop "cannot read instance $INSTANCE; refresh the Learner Lab credentials?"
read -r STATE HOST <<<"$INFO"
[ "$STATE" = running ] || stop "instance $INSTANCE is $STATE; start it first"
[ -n "$HOST" ] && [ "$HOST" != None ] || stop "instance $INSTANCE has no public address"

# 4. Show the target and the commit, then wait for confirmation.
echo "Target host : $INSTANCE at $HOST (profile $LAB_PROFILE, $LAB_REGION)"
echo "Commit      : $SHA"
echo "Actions     : run the packaged installer, replace /etc/inspection/app.env (root, 600), restart inspection"
if $WANT_IOT; then echo "              place 3 consumer files in $IOT_REMOTE (inspection, 600), restart inspection-consumer"; fi
read -r -p "Type deploy to continue: " answer
[ "$answer" = deploy ] || stop "cancelled; nothing was changed"

INSTALLER=.local/installer-$SHA-$$.sh
trap 'rm -f "$INSTALLER"' EXIT
"$PY" deploy/make_user_data.py "$SHA" "$INSTALLER" >/dev/null

# 2. One SSH session. Installer and secrets travel on standard input only: never in arguments,
#    user data or Git. The host keeps them in a root-only temporary file while it runs.
REMOTE='sudo bash -c '\''umask 077; f=$(mktemp) && cat > $f && bash $f; rc=$?; rm -f $f; exit $rc'\'''
{
    # The session's umask 077 protects the secrets below, but the installer's files (the vendored MQTT
    # library above all) must stay readable by the inspection user.
    printf '%s\n' 'umask 022'
    cat "$INSTALLER"
    printf '\n%s\n' 'chmod -R go+rX /opt/inspection/vendor' 'install -d -m 755 /etc/inspection' 'umask 077'
    if $WANT_IOT; then
        # The consumer runs as inspection: files owned by root would be unreadable and it would restart forever.
        printf '%s\n' "install -d -o inspection -g inspection -m 700 $IOT_REMOTE"
        for name in $IOT_FILES; do
            printf '%s\n' "cat > $IOT_REMOTE/$name <<'IOT_FILE_END'"
            tr -d '\r' < "$IOT_DIR/$name" | sed '/^$/d'
            printf '%s\n' 'IOT_FILE_END' "chown inspection:inspection $IOT_REMOTE/$name" "chmod 600 $IOT_REMOTE/$name"
        done
    fi
    printf '%s\n' "cat > /etc/inspection/app.env.new <<'APP_ENV_END'"
    printf '%s\n' "$SECRET_LINES"
    printf '%s\n' 'APP_ENV_END' 'chown root:root /etc/inspection/app.env.new' \
        'chmod 600 /etc/inspection/app.env.new' 'mv -f /etc/inspection/app.env.new /etc/inspection/app.env' \
        'systemctl restart inspection'
    if $WANT_IOT; then printf '%s\n' 'systemctl restart inspection-consumer'; fi
} | MSYS_NO_PATHCONV=1 "$SSH_BIN" -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 \
        "ec2-user@$HOST" "$REMOTE" >/dev/null
echo "Installer finished; checking /health"

# 3. Read back: the version must be this commit and the secrets must be loaded.
WANT_DB=false
if grep -qE '^DB_HOST=.+$' <<<"$SECRET_LINES"; then WANT_DB=true; fi
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if curl --silent --max-time 8 "http://$HOST/health" | "$PY" -c '
import json, sys
body = json.load(sys.stdin)
ok = body.get("version") == sys.argv[1] and body.get("auth_configured") is True
sys.exit(0 if ok and body.get("db_configured", False) is (sys.argv[2] == "true") else 1)' "$SHA" "$WANT_DB" 2>/dev/null
    then
        echo "OK: http://$HOST/health reports version $SHA, auth_configured=true, db_configured=$WANT_DB"
        if $WANT_IOT; then
            sleep 5  # a consumer that cannot start shows as activating or failed only after its first exit
            "$SSH_BIN" -i "$SSH_KEY" -o ConnectTimeout=15 -o LogLevel=ERROR "ec2-user@$HOST" \
                'systemctl is-active inspection inspection-consumer' \
                || stop "a service is not active; see sudo journalctl -u inspection-consumer -n 30"
        fi
        exit 0
    fi
    sleep 5
done
stop "/health did not report version $SHA with auth_configured=true and db_configured=$WANT_DB within 60 s"
