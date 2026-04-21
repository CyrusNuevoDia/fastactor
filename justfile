# fastactor task runner.
# Usage:
#   just                  # list recipes
#   just test             # full suite (skips src/tests/e2e/)
#   just test gen_server  # tests matching name (-k), skips e2e
#   just test e2e         # run only e2e tests
#   just test e2e chaos   # e2e tests matching name (-k)
#   just debug            # stop at first failure, drop into ipdb
#   just debug agent      # same, with -k filter
#   just lint             # ruff check + ty type check
#   just fmt              # ruff format
#   just repl             # ipython with `from fastactor.otp import *`

set shell := ["bash", "-uc"]
set positional-arguments

default:
    @just --list --unsorted

# Run tests. Skips src/tests/e2e/ unless the first arg is `e2e`.
# Remaining args are joined into a `-k` keyword filter.
test *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "${1:-}" = "e2e" ]; then
        shift
        if [ $# -gt 0 ]; then
            uv run pytest src/tests/e2e -k "$*"
        else
            uv run pytest src/tests/e2e
        fi
    elif [ $# -gt 0 ]; then
        uv run pytest src --ignore=src/tests/e2e -k "$*"
    else
        uv run pytest src --ignore=src/tests/e2e
    fi

# Run tests with -x (stop on first failure) and ipdb on error.
# Skips src/tests/e2e/ unless the first arg is `e2e`.
debug *args:
    #!/usr/bin/env bash
    set -euo pipefail
    flags="-x --pdb --pdbcls=ipdb.__main__:Pdb"
    if [ "${1:-}" = "e2e" ]; then
        shift
        if [ $# -gt 0 ]; then
            uv run pytest $flags src/tests/e2e -k "$*"
        else
            uv run pytest $flags src/tests/e2e
        fi
    elif [ $# -gt 0 ]; then
        uv run pytest $flags src --ignore=src/tests/e2e -k "$*"
    else
        uv run pytest $flags src --ignore=src/tests/e2e
    fi

# Lint with ruff and type-check with ty.
lint:
    uv run ruff check src
    uv run ty check src

# Format with ruff (and apply lint fixes).
fmt:
    uv run ruff format src
    uv run ruff check --fix src

# ipython with fastactor preloaded.
repl:
    uv run ipython -i -c "from fastactor.otp import *"
