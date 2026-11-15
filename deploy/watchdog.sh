#!/usr/bin/env bash
# Keeps the public station up. Run every 30 s by forest-guardian-watchdog.timer.
#
# 1. Health: GET /api/health with a short timeout. After FAILS_BEFORE_RESTART
#    consecutive failures (hung event loop, crashed worker, wedged port),
#    restart the station service.
# 2. TLS: the public certificate is a short-lived Let's Encrypt IP cert. If it
#    expires within CERT_MIN_SECONDS, renew it and reload nginx.
set -u

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8002/api/health}"
SERVICE="${SERVICE:-forest-guardian-judges}"
FAILS_BEFORE_RESTART="${FAILS_BEFORE_RESTART:-3}"
STATE_DIR="${STATE_DIR:-/run/forest-guardian-watchdog}"
TLS_HOST="${TLS_HOST-127.0.0.1:443}"
CERT_MIN_SECONDS="${CERT_MIN_SECONDS:-172800}"  # 2 days
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
RENEW_CMD="${RENEW_CMD:-certbot renew --quiet}"

mkdir -p "$STATE_DIR"
fail_file="$STATE_DIR/fails"

log() { echo "watchdog: $*"; }

if curl -fsS -m 5 -o /dev/null "$HEALTH_URL"; then
  echo 0 >"$fail_file"
else
  fails=$(( $(cat "$fail_file" 2>/dev/null || echo 0) + 1 ))
  echo "$fails" >"$fail_file"
  log "health check failed ($fails/$FAILS_BEFORE_RESTART): $HEALTH_URL"
  if [ "$fails" -ge "$FAILS_BEFORE_RESTART" ]; then
    log "restarting $SERVICE"
    $SYSTEMCTL restart "$SERVICE"
    echo 0 >"$fail_file"
  fi
fi

# Certificate checks run hourly, not on every health probe.
last_cert=$(cat "$STATE_DIR/cert_checked" 2>/dev/null || echo 0)
now=$(date +%s)
if [ -n "$TLS_HOST" ] && [ "$((now - last_cert))" -ge 3600 ]; then
  echo "$now" >"$STATE_DIR/cert_checked"
  limit=""; command -v timeout >/dev/null && limit="timeout 10"
  cert=$(echo | $limit openssl s_client -connect "$TLS_HOST" 2>/dev/null \
    | openssl x509 2>/dev/null)
  if [ -z "$cert" ]; then
    log "could not read TLS certificate from $TLS_HOST"
  elif ! echo "$cert" | openssl x509 -noout -checkend "$CERT_MIN_SECONDS" >/dev/null; then
    log "TLS certificate expires within ${CERT_MIN_SECONDS}s; renewing"
    if timeout 300 $RENEW_CMD; then
      $SYSTEMCTL reload nginx
    else
      log "certificate renewal failed"
    fi
  fi
fi
exit 0
