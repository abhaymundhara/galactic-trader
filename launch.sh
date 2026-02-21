#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "⚠️  Created .env from .env.example — fill in your Alpaca keys and then re-run."
    exit 1
fi


pip install -q --upgrade pip
pip install -q -r requirements.txt

echo "🚀 Starting Galactic Trader on http://localhost:${PORT:-8080}"
python main.py
