# ViFi developer tasks. Tab-indented (Make is opinionated).
# Discoverable via `make help`. Aliased into shell completion via
# `complete -W "$(grep '^[a-z_-]\\+:' Makefile | sed 's/:.*//')" make`.

.PHONY: help install test lint format type check sbom security-scan \
        compose-up compose-up-dev compose-down compose-rebuild \
        clean coverage docs

help:
	@echo "ViFi developer tasks"
	@echo
	@echo "Setup:"
	@echo "  install         Install Python deps (production)"
	@echo "  install-dev     Install Python deps (production + test + lint)"
	@echo "  install-hooks   Install pre-commit hooks"
	@echo
	@echo "Quality:"
	@echo "  test            Run pytest"
	@echo "  coverage        Run pytest with coverage report"
	@echo "  lint            Run ruff lint"
	@echo "  format          Run ruff format"
	@echo "  type            Run mypy on strict-typed modules"
	@echo "  check           Run lint + type + test"
	@echo "  security-scan   Run pip-audit + bandit"
	@echo
	@echo "Build:"
	@echo "  sbom            Generate CycloneDX SBOM"
	@echo
	@echo "Compose:"
	@echo "  compose-up-dev  Bring up full stack with simulator (dev profile)"
	@echo "  compose-up      Bring up full stack without simulator"
	@echo "  compose-rebuild Rebuild images and restart"
	@echo "  compose-down    Stop everything"
	@echo
	@echo "Docs:"
	@echo "  docs            Render Markdown docs index (placeholder)"
	@echo
	@echo "Cleanup:"
	@echo "  clean           Remove build artifacts + caches"

install:
	pip install -r requirements.txt

install-dev: install
	pip install -r requirements-dev.txt
	pre-commit install

install-hooks:
	pre-commit install

test:
	pytest -v

coverage:
	pytest --cov --cov-report=term-missing --cov-report=html
	@echo "HTML report: htmlcov/index.html"

lint:
	ruff check .

format:
	ruff format .
	ruff check --fix .

type:
	mypy pseudonymize.py config.py __version__.py
	@echo "--- lenient (warn-only) ---"
	-mypy security.py audit.py observability.py

check: lint type test

security-scan:
	pip-audit
	bandit -r . -x tests,build

sbom:
	mkdir -p sbom
	# cyclonedx-bom 4.x ships a `cyclonedx-py` CLI; the args differ
	# slightly between major versions. This is the 4.x form.
	cyclonedx-py -r -i requirements.txt --format json \
	    -o sbom/python-deps.json || \
	    cyclonedx-py requirements -i requirements.txt --of JSON \
	        -o sbom/python-deps.json
	@echo "SBOM: sbom/python-deps.json"
	@echo "For container SBOM, run: docker sbom vifi-ml-api > sbom/container.json"

compose-up-dev:
	docker compose --profile dev up -d --build
	@echo "Dashboard: http://localhost:8501"
	@echo "API:       http://localhost:8000/health"

compose-up:
	docker compose up -d --build

compose-rebuild:
	docker compose down
	docker compose --profile dev up -d --build --force-recreate

compose-down:
	docker compose --profile dev down

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
	rm -rf htmlcov .coverage build dist *.egg-info sbom

docs:
	@echo "Markdown docs in: README.md, SECURITY.md, COMPLIANCE.md, CHANGELOG.md, docs/"
	@ls -1 docs/ 2>/dev/null || echo "(no docs/ directory yet)"
