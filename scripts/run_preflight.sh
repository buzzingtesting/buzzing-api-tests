#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
mkdir -p reports
run_dir=$(mktemp -d reports/preflight-XXXXXX)
echo "预检产物目录 $run_dir"

BUZZING_ENV=preview "${PYTHON_BIN:-.venv/bin/python}" -m pytest -v -m preflight \
    --tb=short \
    --log-cli-level=INFO \
    --log-file="$run_dir/http.log" --log-file-level=INFO \
    --junitxml="$run_dir/junit.xml" \
    --alluredir="$run_dir/allure-results"
