$ErrorActionPreference = "Stop"

Write-Host "[1/4] Criando ambiente virtual..."
python -m venv .venv

Write-Host "[2/4] Atualizando pip do ambiente virtual..."
& .\.venv\Scripts\python.exe -m pip install --upgrade pip

Write-Host "[3/4] Instalando dependencias Python..."
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host "[4/4] Instalando Chromium do Playwright..."
& .\.venv\Scripts\python.exe -m playwright install chromium

Write-Host "Instalacao concluida. Para executar: .\run_full.ps1"
