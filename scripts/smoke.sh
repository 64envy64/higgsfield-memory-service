#!/usr/bin/env bash
# Minimal smoke test taken from the task spec.
# Run: make up && make smoke

set -euo pipefail

BASE="${BASE:-http://localhost:8080}"

if command -v jq >/dev/null 2>&1; then
    JQ="jq ."
else
    JQ="cat"
fi

echo "==> GET /health"
curl -sf "$BASE/health" | $JQ

echo
echo "==> POST /turns"
curl -sf -X POST "$BASE/turns" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "smoke-1",
    "user_id": "user-1",
    "messages": [
      {"role": "user", "content": "I just moved to Berlin from NYC last month. Loving it so far."},
      {"role": "assistant", "content": "That sounds exciting! Berlin is a great city. How are you settling in?"}
    ],
    "timestamp": "2025-03-15T10:30:00Z",
    "metadata": {}
  }' | $JQ

echo
echo "==> POST /recall (cross-session)"
curl -sf -X POST "$BASE/recall" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "Where does this user live?",
    "session_id": "smoke-2",
    "user_id": "user-1",
    "max_tokens": 512
  }' | $JQ

echo
echo "==> GET /users/user-1/memories"
curl -sf "$BASE/users/user-1/memories" | $JQ

echo
echo "==> DELETE /users/user-1 (cleanup)"
curl -sf -X DELETE "$BASE/users/user-1" -o /dev/null -w "HTTP %{http_code}\n"
