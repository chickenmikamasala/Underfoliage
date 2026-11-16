#!/bin/bash
# Double-click to start the local LaTeX editor and open it in your browser.
cd "$(dirname "$0")"
PORT=8765
if ! curl -s -o /dev/null "http://127.0.0.1:$PORT/api/status"; then
  echo "Starting Typeset on http://127.0.0.1:$PORT ..."
  nohup python3 server.py "$PORT" > build/server.log 2>&1 &
  for i in $(seq 1 30); do
    curl -s -o /dev/null "http://127.0.0.1:$PORT/api/status" && break
    sleep 0.3
  done
else
  echo "Typeset is already running on http://127.0.0.1:$PORT"
fi
open "http://127.0.0.1:$PORT"
echo "Leave this window open while you work; close it (or press Ctrl+C) to stop the server."
wait
