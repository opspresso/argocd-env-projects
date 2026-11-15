# A redirect is not a successful readiness response. Callers own the bounded startup wait.
http_healthy() {
  http_health_status=$(curl -sS -o /dev/null --max-time 10 -w '%{http_code}' "$1") || return 1
  case "$http_health_status" in
    2[0-9][0-9]) return 0 ;;
    *) return 1 ;;
  esac
}
