#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
ACTION="${1:-start}"
MODE="${2:-lan}"
PORT="${INMUSIC_PORT:-5080}"
RUNDIR=".runtime"
PIDFILE="$RUNDIR/inmusic.pid"
LOGFILE="$RUNDIR/inmusic.log"
mkdir -p "$RUNDIR"

is_running() {
  [[ -s "$PIDFILE" ]] || return 1
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}
stop_server() {
  if is_running; then
    local pid
    pid="$(cat "$PIDFILE")"
    echo "Останавливаю InMusic (PID $pid)..."
    kill -TERM "$pid"
    for _ in {1..30}; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "Сервер не завершился штатно; отправляю SIGKILL."
      kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
    echo "InMusic остановлен."
  else
    rm -f "$PIDFILE"
    echo "InMusic сейчас не запущен."
  fi
}
start_server() {
  if is_running; then
    echo "InMusic уже запущен (PID $(cat "$PIDFILE"))."
    echo "Лог: $LOGFILE"
    return 0
  fi
  rm -f "$PIDFILE"
  export INMUSIC_PORT="$PORT"
  case "$MODE" in
    local) export INMUSIC_HOST=127.0.0.1; unset INMUSIC_COOKIE_SECURE ;;
    public) export INMUSIC_HOST=127.0.0.1; export INMUSIC_COOKIE_SECURE=1 ;;
    lan|zerotier|radmin) export INMUSIC_HOST=0.0.0.0; unset INMUSIC_COOKIE_SECURE ;;
    *) echo "Неизвестный режим: $MODE" >&2; exit 2 ;;
  esac
  nohup python3 app.py >>"$LOGFILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$PIDFILE"
  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PIDFILE"
    echo "Не удалось запустить сервер. Последние строки журнала:" >&2
    tail -n 30 "$LOGFILE" >&2 || true
    exit 1
  fi
  echo "InMusic запущен (PID $pid, режим $MODE, порт $PORT)."
  if [[ "$MODE" == public ]]; then
    echo "Локальный адрес: http://127.0.0.1:$PORT/InMusic"
  else
    echo "Локальный адрес: http://127.0.0.1:$PORT/InMusic"
  fi
  echo "Журнал: $LOGFILE"
  echo "Остановка: ./inmusic-control.sh stop"
  echo "Перезапуск после изменения системных файлов: ./inmusic-control.sh restart $MODE"
}
case "$ACTION" in
  start) start_server ;;
  stop) stop_server ;;
  restart) stop_server; start_server ;;
  status) if is_running; then echo "InMusic работает (PID $(cat "$PIDFILE"))."; else echo "InMusic остановлен."; exit 1; fi ;;
  logs) touch "$LOGFILE"; tail -n 100 -f "$LOGFILE" ;;
  *) echo "Использование: $0 {start|stop|restart|status|logs} [local|lan|zerotier|radmin|public]"; exit 2 ;;
esac
