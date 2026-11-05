#!/bin/bash
# Re-escaneia o dashboard prelive-favorites a cada execucao, cobrindo os jogos
# das proximas ~24h (hoje + amanha, modo pre-live = so jogos ainda nao iniciados).
# Agendado via crontab a cada 4 horas. Nao precisa de API key (o servidor ja tem).

BASE="http://localhost:8891"
DIR="/Users/renan/.openclaw/workspace/dashboards/prelive-favorites"
LOG="$DIR/cron-scan.log"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# servidor no ar?
if ! curl -s -m 8 "$BASE/health" | grep -q '"ok": true'; then
  echo "$(ts) ERRO: dashboard offline em $BASE" >> "$LOG"
  exit 1
fi

dd=$(curl -s -m 10 "$BASE/api/default-date")
today=$(printf '%s' "$dd"    | python3 -c "import sys,json;print(json.load(sys.stdin)['today'])" 2>/dev/null)
tomorrow=$(printf '%s' "$dd" | python3 -c "import sys,json;print(json.load(sys.stdin)['tomorrow'])" 2>/dev/null)

if [ -z "$today" ] || [ -z "$tomorrow" ]; then
  echo "$(ts) ERRO: nao consegui obter datas" >> "$LOG"
  exit 1
fi

scan_and_wait() {
  local d="$1"
  curl -s -m 10 -X POST "$BASE/api/scan" -H 'Content-Type: application/json' \
    -d "{\"date\":\"$d\",\"force\":true}" >/dev/null
  local st n
  for _ in $(seq 1 72); do
    read -r st n < <(curl -s -m 5 "$BASE/api/state" \
      | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status'),len(d.get('qualified') or []))" 2>/dev/null)
    if [ "$st" = "ready" ]; then
      echo "$(ts) $d -> ready ($n entradas)" >> "$LOG"
      return
    fi
    sleep 5
  done
  echo "$(ts) $d -> TIMEOUT" >> "$LOG"
}

away_scan() {
  local d="$1"
  local r
  r=$(curl -s -m 180 -X POST "$BASE/api/away/scan" -H 'Content-Type: application/json' \
    -d "{\"date\":\"$d\",\"include_played\":false}" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('qualified',0),d.get('star4',0))" 2>/dev/null)
  echo "$(ts) $d visitante -> ${r:-erro}" >> "$LOG"
}

echo "$(ts) === scan proximas-24h (hoje=$today amanha=$tomorrow) ===" >> "$LOG"
scan_and_wait "$today"
scan_and_wait "$tomorrow"
away_scan "$today"
away_scan "$tomorrow"
