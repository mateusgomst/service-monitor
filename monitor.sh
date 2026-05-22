#!/usr/bin/env bash
# Service Monitor - verifica serviços, sites do nginx e recursos do servidor.
# Envia alertas para o Telegram quando algo cai e quando volta.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.env"
STATE_DIR="$SCRIPT_DIR/state"
LOG_FILE="$SCRIPT_DIR/monitor.log"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "ERRO: $CONFIG_FILE não encontrado. Copie config.env.example para config.env." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG_FILE"

mkdir -p "$STATE_DIR"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

notify() {
  local msg="$1"
  log "NOTIFY: $msg"
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ "${TELEGRAM_BOT_TOKEN}" = "cole_o_token_aqui" ]; then
    log "Telegram não configurado, pulando envio."
    return 0
  fi
  curl -s --max-time 10 \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    -d "parse_mode=Markdown" \
    --data-urlencode "text=${msg}" > /dev/null || log "Falha ao enviar telegram."
}

# alert_down <chave> <mensagem>
alert_down() {
  local key="$1" msg="$2"
  local flag="$STATE_DIR/${key}.down"
  local now epoch_now epoch_flag diff_min
  now=$(date +%s)

  if [ ! -f "$flag" ]; then
    notify "🔴 *${SERVER_NAME}* - $msg"
    echo "$now" > "$flag"
  elif [ "${REALERT_MINUTES:-0}" -gt 0 ]; then
    epoch_flag=$(cat "$flag" 2>/dev/null || echo "$now")
    diff_min=$(( (now - epoch_flag) / 60 ))
    if [ "$diff_min" -ge "$REALERT_MINUTES" ]; then
      notify "🔴 *${SERVER_NAME}* (ainda caído há ${diff_min}min) - $msg"
      echo "$now" > "$flag"
    fi
  fi
}

# alert_up <chave> <mensagem>
alert_up() {
  local key="$1" msg="$2"
  local flag="$STATE_DIR/${key}.down"
  if [ -f "$flag" ]; then
    notify "✅ *${SERVER_NAME}* - $msg"
    rm -f "$flag"
  fi
}

sanitize_key() {
  echo "$1" | tr -c 'A-Za-z0-9._-' '_'
}

# ========== 1. Serviços systemd ==========
check_services() {
  for svc in $SERVICES; do
    local key
    key="svc_$(sanitize_key "$svc")"
    if systemctl is-active --quiet "$svc"; then
      alert_up "$key" "serviço \`${svc}\` voltou a rodar"
    else
      local status
      status=$(systemctl is-active "$svc" 2>&1 || true)
      alert_down "$key" "serviço \`${svc}\` está *${status}*"
    fi
  done
}

# ========== 2. Sites do nginx ==========
# Extrai server_names dos arquivos em sites-enabled e tenta um HTTP/HTTPS.
check_nginx_sites() {
  [ -d "$NGINX_SITES_DIR" ] || { log "Dir nginx não existe: $NGINX_SITES_DIR"; return; }

  # Coleta pares "porta server_name" únicos
  local tmp
  tmp=$(mktemp)
  for conf in "$NGINX_SITES_DIR"/*; do
    [ -f "$conf" ] || continue
    awk '
      /^[[:space:]]*server[[:space:]]*{/ { in_server=1; ssl=0; port="80"; names=""; next }
      in_server && /^[[:space:]]*}/ {
        if (names != "") {
          n=split(names, arr, " ")
          for (i=1;i<=n;i++) if (arr[i] != "") print port" "arr[i]
        }
        in_server=0; next
      }
      in_server && /listen[[:space:]]/ {
        if ($0 ~ /ssl|443/) { ssl=1; port="443" }
        else { match($0, /listen[[:space:]]+([0-9]+)/, m); if (m[1] != "") port=m[1] }
      }
      in_server && /server_name[[:space:]]/ {
        line=$0
        sub(/.*server_name[[:space:]]+/, "", line)
        sub(/;.*/, "", line)
        names=names" "line
      }
    ' "$conf" >> "$tmp"
  done

  # Para cada (porta, server_name), faz a verificação
  sort -u "$tmp" | while read -r port name; do
    [ -z "$name" ] && continue
    # ignora lista
    local skip=0
    for ig in $IGNORE_SITES; do
      [ "$name" = "$ig" ] && skip=1 && break
    done
    [ "$skip" -eq 1 ] && continue

    local scheme="http"
    [ "$port" = "443" ] && scheme="https"

    local url="${scheme}://${name}"
    [ "$port" != "80" ] && [ "$port" != "443" ] && url="${scheme}://${name}:${port}"

    local key
    key="site_$(sanitize_key "${name}_${port}")"

    # --resolve força ir pro 127.0.0.1 mesmo que o DNS não exista (caso .local)
    local code
    code=$(curl -k -s -o /dev/null -w "%{http_code}" \
      --max-time "$HTTP_TIMEOUT" \
      --resolve "${name}:${port}:127.0.0.1" \
      "$url" 2>/dev/null || echo "000")

    # 2xx, 3xx, 401, 403 = nginx + backend respondendo. 502/504 = backend caído.
    if [[ "$code" =~ ^(2..|3..|401|403|405)$ ]]; then
      alert_up "$key" "site \`${name}\` (porta ${port}) voltou - HTTP ${code}"
    else
      alert_down "$key" "site \`${name}\` (porta ${port}) com problema - HTTP ${code}"
    fi
  done

  rm -f "$tmp"
}

# ========== 3. Memória ==========
check_memory() {
  local used
  used=$(free | awk '/^Mem:/ {printf "%.0f", $3/$2*100}')
  if [ "$used" -ge "${MEM_THRESHOLD:-85}" ]; then
    local top
    top=$(ps -eo pid,user,%mem,comm --sort=-%mem | head -6 | awk 'NR>1 {printf "%s %s %s%% %s\n", $1,$2,$3,$4}')
    alert_down "mem" "memória em *${used}%* (limite ${MEM_THRESHOLD}%)\nTop processos:\n\`\`\`\n${top}\n\`\`\`"
  else
    alert_up "mem" "memória normalizou (${used}%)"
  fi
}

# ========== 4. Disco ==========
check_disk() {
  df --output=target,pcent | awk 'NR>1' | while read -r mount pct; do
    # ignora pseudo filesystems / loops
    case "$mount" in
      /snap/*|/run/*|/dev/*|/sys/*|/proc/*|/boot/efi) continue ;;
    esac
    local p="${pct%\%}"
    local key
    key="disk_$(sanitize_key "$mount")"
    if [ "$p" -ge "${DISK_THRESHOLD:-85}" ]; then
      alert_down "$key" "disco \`${mount}\` em *${p}%*"
    else
      alert_up "$key" "disco \`${mount}\` normalizou (${p}%)"
    fi
  done
}

# ========== Execução ==========
case "${1:-run}" in
  run)
    check_services
    check_nginx_sites
    check_memory
    check_disk
    ;;
  test)
    notify "🧪 Teste do service-monitor em *${SERVER_NAME}* - se você está lendo isso, o Telegram está OK."
    ;;
  sites)
    # lista os sites que seriam checados (útil para depurar)
    echo "Sites detectados em $NGINX_SITES_DIR:"
    for conf in "$NGINX_SITES_DIR"/*; do
      [ -f "$conf" ] || continue
      grep -E 'server_name|listen' "$conf" | sed "s|^|  $(basename "$conf"): |"
    done
    ;;
  reset)
    rm -f "$STATE_DIR"/*.down
    echo "Estado limpo."
    ;;
  *)
    echo "Uso: $0 [run|test|sites|reset]"
    exit 1
    ;;
esac
