@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [INFO] creating venv...
  python -m venv .venv
  if errorlevel 1 ( echo [FAIL] python -m venv failed & exit /b 1 )
)

".venv\Scripts\python.exe" -c "import fitz, requests" 1>nul 2>nul
if errorlevel 1 (
  echo [INFO] installing requirements...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 ( echo [FAIL] pip install failed & exit /b 1 )
)

if not exist "config.json" (
  echo [FAIL] config.json not found. copy config.example.json config.json and edit it.
  exit /b 1
)

".venv\Scripts\python.exe" ocr_pdf.py --config config.json %*
