.PHONY: install sandbox stop status repl test test-integration clean help

help:
	@echo "SchemaPilot Developer Commands:"
	@echo "  make install     Install SchemaPilot and dependencies locally in editable mode"
	@echo "  make sandbox     Spin up PostgreSQL and MySQL Docker test databases"
	@echo "  make stop        Stop database sandbox containers"
	@echo "  make status      Check running database sandbox containers"
	@echo "  make repl        Launch the interactive SchemaPilot command-line REPL shell"
	@echo "  make test        Run the unit test suite"
	@echo "  make test-integration  Run the live engine integration matrix (needs docker)"
	@echo "  make clean       Clean build artifacts and temporary files"

install:
	pip install -e .

sandbox:
	docker compose up -d

stop:
	docker compose down

status:
	docker ps --filter "name=schemapilot-"

repl:
	python3 -m schemapilot.cli

test:
	pytest tests -q --ignore=tests/integration

test-integration:
	SCHEMAPILOT_IT=1 pytest tests/integration -q

clean:
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ __pycache__ schemapilot/__pycache__
	find . -type d -name "__pycache__" -exec rm -r {} +
