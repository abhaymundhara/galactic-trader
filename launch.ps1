param()
Set-Location $PSScriptRoot

if (-Not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "⚠️  Created .env from .env.example — fill in your Alpaca keys and then re-run."
    exit 1
}

if (-Not (Test-Path ".venv")) {
    Write-Host "📦 Creating virtual environment..."
    python -m venv .venv
}

& .\.venv\Scripts\Activate.ps1
pip install -q --upgrade pip
pip install -q -r requirements.txt

Write-Host "🚀 Starting Galactic Trader on http://localhost:8080"
python main.py
