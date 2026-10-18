# PYTHONPATH=src makes the package importable even when the editable-install .pth file is
# skipped (older uv marks .venv hidden on macOS, and Python >= 3.12.11 ignores hidden .pth
# files). `uv self update` fixes the root cause.
RUN := PYTHONPATH=src uv run
GS  := $(RUN) python -m gopher_scout.cli

.PHONY: install test lint fmt typecheck check serve discover profile analyse critique research

install:
	uv sync

test:
	$(RUN) pytest -q

lint:
	$(RUN) ruff check src tests

fmt:
	$(RUN) ruff format src tests
	$(RUN) ruff check --fix src tests

typecheck:
	$(RUN) mypy src

check: lint typecheck test

serve:
	$(GS) serve

discover:
	$(GS) discover

profile:
	$(GS) profile

analyse:
	$(GS) analyse

critique:
	$(GS) critique

research:
	$(GS) research
