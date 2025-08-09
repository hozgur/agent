PYTHON ?= python3
PIP ?= pip3
VENV ?= .venv

.PHONY: venv install dev clean lint fmt build run

venv:
	$(PYTHON) -m venv $(VENV)
	. $(VENV)/bin/activate && $(PIP) install --upgrade pip wheel

install: venv
	. $(VENV)/bin/activate && $(PIP) install -r requirements.txt
	. $(VENV)/bin/activate && $(PIP) install -e .

dev: install
	. $(VENV)/bin/activate && pre-commit install || true

build:
	. $(VENV)/bin/activate && python -m build || true

run:
	. $(VENV)/bin/activate && agent --help

clean:
	rm -rf $(VENV) build dist *.egg-info


