#!/bin/bash
set -e

echo "[1/4] Stopping any running Patroni processes..."
sudo pkill -f 'patroni /etc/patroni' || true
sleep 2

echo "[2/4] Stopping any running Postgres on /tmp/n2 and /tmp/n3..."
sudo -u postgres /usr/pgsql-18/bin/pg_ctl -D /tmp/n2 stop -m immediate 2>/dev/null || true
sudo -u postgres /usr/pgsql-18/bin/pg_ctl -D /tmp/n3 stop -m immediate 2>/dev/null || true
sleep 2

echo "[3/4] Cleaning etcd state for zone-n2..."
etcdctl del --prefix /service/zone-n2/
remaining=$(etcdctl get --prefix /service/zone-n2/ --keys-only | wc -l)
if [ "$remaining" -ne 0 ]; then
  echo "WARNING: $remaining keys still in /service/zone-n2/"
  etcdctl get --prefix /service/zone-n2/ --keys-only
  exit 1
fi

echo "[4/4] Done. etcd cleared for zone-n2."
echo ""
echo "Current etcd contents:"
etcdctl get --prefix / --keys-only