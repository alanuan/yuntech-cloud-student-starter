#!/usr/bin/env bash
# Deploy ONE committed version to the existing host over SSH, then install the secret file.
# Spec: labs/04-web-api/README.md ("權杖與 deploy.sh"). Runs in Codespaces bash and Git Bash on Windows.
#
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
SECRET_LINES=$(tr -d '\r' < "$SECRET" | sed '/^$/d')
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
read -r -p "Type deploy to continue: " answer
[ "$answer" = deploy ] || stop "cancelled; nothing was changed"

INSTALLER=.local/installer-$SHA-$$.sh
trap 'rm -f "$INSTALLER"' EXIT
"$PY" deploy/make_user_data.py "$SHA" "$INSTALLER" >/dev/null

# 2. One SSH session. Installer and secrets travel on standard input only: never in arguments,
#    user data or Git. The host keeps them in a root-only temporary file while it runs.
REMOTE='sudo bash -c '\''umask 077; f=$(mktemp) && cat > $f && bash $f; rc=$?; rm -f $f; exit $rc'\'''
{
    cat "$INSTALLER"
    printf '\n%s\n' 'install -d -m 755 /etc/inspection' 'umask 077' \
        "cat > /etc/inspection/app.env.new <<'APP_ENV_END'"
    printf '%s\n' "$SECRET_LINES"
    printf '%s\n' 'APP_ENV_END' 'chown root:root /etc/inspection/app.env.new' \
        'chmod 600 /etc/inspection/app.env.new' 'mv -f /etc/inspection/app.env.new /etc/inspection/app.env' \
        'systemctl restart inspection'
} | MSYS_NO_PATHCONV=1 "$SSH_BIN" -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 \
        "ec2-user@$HOST" "$REMOTE" >/dev/null
echo "Installer finished; checking /health"

# 3. Read back: the version must be this commit and the tokens must be loaded.
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if curl --silent --max-time 8 "http://$HOST/health" | "$PY" -c '
import json, sys
body = json.load(sys.stdin)
sys.exit(0 if body.get("version") == sys.argv[1] and body.get("auth_configured") is True else 1)' "$SHA" 2>/dev/null
    then
        echo "OK: http://$HOST/health reports version $SHA and auth_configured=true"
        exit 0
    fi
    sleep 5
done
stop "/health did not report version $SHA with auth_configured=true within 60 s"
