#!/bin/bash
cd "$(dirname "$0")" || exit 1
if [[ ! -x .venv/bin/python ]]; then
    echo "请先在项目目录运行 bash scripts/setup_local.sh 准备环境。"
    exit 1
fi
exec .venv/bin/python local_app.py
