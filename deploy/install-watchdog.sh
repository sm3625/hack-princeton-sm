#!/usr/bin/env bash
# Run as root on the station host from /opt/forest-guardian-judges.
set -euo pipefail
cd "$(dirname "$0")"

chmod +x watchdog.sh
install -d /etc/systemd/system/forest-guardian-judges.service.d
install -m 644 systemd/restart.conf \
  /etc/systemd/system/forest-guardian-judges.service.d/restart.conf
install -m 644 systemd/forest-guardian-watchdog.service systemd/forest-guardian-watchdog.timer \
  /etc/systemd/system/
install -m 644 nginx/station-down.html /var/www/station-down.html

# Renew certificates automatically even if the watchdog is off.
systemctl enable --now certbot.timer 2>/dev/null || echo "certbot.timer not found: check how TLS is renewed"

systemctl daemon-reload
systemctl enable --now forest-guardian-watchdog.timer
systemctl restart forest-guardian-judges
systemctl --no-pager status forest-guardian-judges forest-guardian-watchdog.timer | head -20
echo
echo "Next: add deploy/nginx/station-down.conf to the nginx server block, then: nginx -t && systemctl reload nginx"
