#!/bin/bash
# Double-click to start Underleaf, the local LaTeX editor, and open it in your browser.
# Double-clicking again restarts it, so code changes take effect.
# Keep the Terminal window open while you work; close it or press Ctrl+C to stop Underleaf.
cd "$(dirname "$0")"
PORT=8765

# Stop an Underleaf server that is already running on this port, so the newest code is used.
for pid in $(lsof -nP -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null); do
  if ps -o command= -p "$pid" | grep -q "server.py"; then
    echo "Restarting Underleaf..."
    kill "$pid"
  fi
done
for i in $(seq 1 20); do
  lsof -nP -tiTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 || break
  sleep 0.25
done

# Open the browser as soon as the server answers.
(
  for i in $(seq 1 40); do
    curl -s -o /dev/null "http://127.0.0.1:$PORT/api/status" && break
    sleep 0.25
  done
  open "http://127.0.0.1:$PORT"
) &

echo "Underleaf is running at http://127.0.0.1:$PORT"
echo "Keep this window open while you work. Close it or press Ctrl+C to stop Underleaf."
echo
# Run the server in this window, so closing the window stops it.
exec python3 server.py "$PORT"
