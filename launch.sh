#!/bin/bash
cd "$(dirname "$0")"

# Kill any previous instance on port 8000
lsof -ti tcp:8000 | xargs kill -9 2>/dev/null

python3 -m uvicorn main:app &
SERVER_PID=$!

# Wait for server to accept connections
for i in {1..20}; do
  sleep 0.3
  if curl -s http://localhost:8000 > /dev/null 2>&1; then
    break
  fi
done

open http://localhost:8000
wait $SERVER_PID
