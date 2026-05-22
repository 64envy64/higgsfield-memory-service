.PHONY: help build up down logs ps health smoke test test-unit test-quality test-persistence clean

help:
	@echo "Targets:"
	@echo "  up               - docker compose up -d (build + start)"
	@echo "  down             - docker compose down (keeps the data volume)"
	@echo "  logs             - tail api logs"
	@echo "  health           - probe /health"
	@echo "  smoke            - run the curl smoke test from the spec"
	@echo "  test             - run all internal tests (contract + quality)"
	@echo "  test-persistence - down + up + verify previously-written facts survive"
	@echo "  clean            - down + remove the data volume (destroys all memories)"

build:
	docker compose build

up:
	docker compose up -d --build
	@echo "Waiting for /health ..."
	@until curl -sf http://localhost:8080/health > /dev/null; do sleep 1; done
	@echo "Ready."

down:
	docker compose down

logs:
	docker compose logs -f api

ps:
	docker compose ps

health:
	@curl -sf http://localhost:8080/health | python -m json.tool || echo "not ready"

smoke:
	@bash scripts/smoke.sh

test: test-unit test-quality

test-unit:
	docker compose exec -T api python -m pytest tests/contract -v

test-quality:
	docker compose exec -T api python -m pytest tests/quality -v

test-persistence:
	bash scripts/test_persistence.sh

clean:
	docker compose down -v
