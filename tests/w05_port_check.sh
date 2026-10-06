#!/usr/bin/env bash
# W5 T2: from outside the VPC, the database port must NOT be reachable. Prints no endpoint.
set -u
cd "$(dirname "$0")/.."
DB_HOST=$(grep '^DB_HOST=' .local/db.env | cut -d= -f2 | tr -d '\r')
[ -n "$DB_HOST" ] || { echo "STOP: DB_HOST is not in .local/db.env"; exit 1; }
started=$(date +%s)
if timeout 8 bash -c "echo > /dev/tcp/$DB_HOST/5432" 2>/dev/null; then
    echo "連得到：錯了，立刻檢查"
else
    echo "連不到：符合設計"
fi
echo "（等了 $(( $(date +%s) - started )) 秒，上限 8 秒）"
