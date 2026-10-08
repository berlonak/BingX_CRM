#!/usr/bin/env bash
# copyright by berlonak
# telegram: @Kilax123
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then echo 'Скопируй .env.example в .env и укажи ключи и ID.'; exit 1; fi
python3 -m pip install -r requirements.txt
python3 main.py
