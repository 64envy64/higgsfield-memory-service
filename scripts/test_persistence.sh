#!/usr/bin/env bash
# Persistence test: write a turn, restart the stack (keeping the volume),
# then verify the turn is still retrievable.

set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
USER_ID="persist-$(date +%s)"
SESSION_ID="persist-sess-$(date +%s)"

extract_id() {
    # portable JSON .id extractor
    python -c "import json,sys; print(json.load(sys.stdin)['id'])"
}

wait_health() {
    echo "==> waiting for /health"
    for _ in $(seq 1 60); do
        if curl -sf "$BASE/health" >/dev/null; then return; fi
        sleep 1
    done
    echo "service did not become healthy"; exit 1
}

echo "==> ensure stack is up"
docker compose up -d
wait_health

echo "==> POST /turns (user=$USER_ID)"
RAW_TURN=$(curl -sf -X POST "$BASE/turns" \
  -H 'Content-Type: application/json' \
  -d "{
    \"session_id\": \"$SESSION_ID\",
    \"user_id\": \"$USER_ID\",
    \"messages\": [
      {\"role\": \"user\", \"content\": \"Persist me across a restart please.\"},
      {\"role\": \"assistant\", \"content\": \"Got it.\"}
    ],
    \"timestamp\": \"2025-03-15T10:30:00Z\",
    \"metadata\": {\"test\": \"persistence\"}
  }")
TURN_ID=$(echo "$RAW_TURN" | extract_id)
echo "    turn id: $TURN_ID"

echo "==> docker compose down (volume preserved)"
docker compose down

echo "==> docker compose up -d"
docker compose up -d
wait_health

echo "==> verifying raw turn survived the restart (direct DB count)"
COUNT=$(docker compose exec -T db psql -U memory -d memory -tA \
  -c "SELECT count(*) FROM turns WHERE user_id = '$USER_ID';" | tr -d '[:space:]')
echo "    rows in turns for $USER_ID: $COUNT"
if [ "$COUNT" -lt 1 ]; then
    echo "FAIL: turn was lost across restart"; exit 1
fi

echo "==> verifying HTTP shape (/users/.../memories returns 200)"
curl -sf "$BASE/users/$USER_ID/memories" >/dev/null
echo "    GET /users/$USER_ID/memories OK"

echo "==> verifying /recall sees the turn after restart"
RECALL_CONTEXT=$(curl -sf -X POST "$BASE/recall" \
  -H 'Content-Type: application/json' \
  -d "{
    \"query\": \"Persist me across restart\",
    \"session_id\": \"${SESSION_ID}-probe\",
    \"user_id\": \"$USER_ID\",
    \"max_tokens\": 256
  }" | python -c "import json,sys; print(json.load(sys.stdin).get('context',''))")
if ! printf '%s' "$RECALL_CONTEXT" | grep -qi "persist me across"; then
    echo "FAIL: /recall did not return the pre-restart turn"; exit 1
fi
echo "    /recall OK"

echo "==> verifying /search sees the turn after restart"
SEARCH_RESULTS=$(curl -sf -X POST "$BASE/search" \
  -H 'Content-Type: application/json' \
  -d "{
    \"query\": \"Persist me across restart\",
    \"user_id\": \"$USER_ID\",
    \"limit\": 5
  }" | python -c "import json,sys; print(json.dumps(json.load(sys.stdin).get('results', [])))")
if ! printf '%s' "$SEARCH_RESULTS" | grep -qi "persist me across"; then
    echo "FAIL: /search did not return the pre-restart turn"; exit 1
fi
echo "    /search OK"

echo "==> cleanup"
curl -sf -X DELETE "$BASE/users/$USER_ID" -o /dev/null -w "    DELETE HTTP %{http_code}\n"
echo "==> PERSISTENCE OK"
