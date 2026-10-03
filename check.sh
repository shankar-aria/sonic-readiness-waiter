#!/bin/bash
# Compile and lint checks for sonic_readiness_waiter.py.
#   bash check.sh            (install tools first: pip install -r requirements.txt)
set -u
cd "$(dirname "$0")" || exit 2
F=sonic_readiness_waiter.py
rc=0
step() { printf '== %s\n' "$1"; }
step "compile"
python3 -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')" "$F" || rc=1
step "syntax compatible with Python 3.9"
python3 -c "import ast, sys; ast.parse(open(sys.argv[1]).read(), feature_version=(3, 9))" "$F" || rc=1
step "lint (ruff)"
ruff check --no-cache --target-version py39 "$F" || rc=1
[ "$rc" = 0 ] && echo "ALL CHECKS PASSED" || echo "CHECKS FAILED"
exit "$rc"
