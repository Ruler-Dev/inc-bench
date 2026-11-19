#!/usr/bin/env sh
# inc-bench: avvia la web UI. Se il processo si chiude da solo, lo riavvia e annota il motivo in data/webui_eventi.log.
cd "$(dirname "$0")" || exit 1
mkdir -p data
PY="${PYTHON:-python3}"
ARGS=""
while true; do
  echo "[$(date -Iseconds)] avvio web UI" >> data/webui_eventi.log
  "$PY" -u -X faulthandler webui.py $ARGS
  code=$?
  echo "[$(date -Iseconds)] web UI uscita con codice $code" >> data/webui_eventi.log
  [ "$code" -eq 130 ] && exit 0   # Ctrl+C
  echo "La web UI si e' chiusa (codice $code). Riavvio fra 5 secondi... (Ctrl+C per uscire)"
  ARGS="--no-browser"
  sleep 5
done
