#!/usr/bin/env bash
# What CI's lint job runs (it calls this script), and the pre-commit hook.
# Uses the .venv's ruff/flake8 when there, else the ones on PATH (CI).
set -euo pipefail
cd "$(dirname "$0")/.."
bin=.venv/bin/
[[ -x ${bin}ruff ]] || bin=
ruff=${bin}ruff
if [[ -x ${bin}flake8 ]]; then flake8=(${bin}flake8); elif [[ -n $bin ]]; then flake8=(uvx flake8@7.3.0); else flake8=(flake8); fi
py=${PYTHON:-python3}

"$ruff" check .
"$ruff" format --check .
"${flake8[@]}" qstac  # the plugins.qgis.org code-quality check (W503 on, see .flake8)
# Pure-stdlib self-checks (test_index and test_raster need QGIS: run them by hand)
for t in collections catalogs detect facets search indices; do
  "$py" -m "tests.test_$t" >/dev/null
done

# The CODING_STANDARDS.md rules marked (checked)
fail() { echo "$1 (CODING_STANDARDS.md):"; echo "$2"; exit 1; }
hits=$(grep -rnE "urlopen\(|build_opener\(" qstac --include=*.py | grep -v "^qstac/stac/net.py:" || true)
[[ -z $hits ]] || fail "HTTP outside stac.net.open_url()" "$hits"
hits=$(grep -rn "signed_assets(" qstac --include=*.py | grep -v "def signed_assets(" || true)
[[ $(grep -c . <<<"$hits") == 1 && $hits == *"lambda _task: signed_assets("* ]] \
  || fail "signed_assets() called outside LayerLoader.sign_then()" "$hits"
echo "check.sh: ok"
