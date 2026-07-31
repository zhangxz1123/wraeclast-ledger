@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 goto run_py
)

where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 goto run_python
)

where python3 >nul 2>nul
if not errorlevel 1 (
  python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 goto run_python3
)

set "BUNDLED_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%BUNDLED_PYTHON%" (
  "%BUNDLED_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 goto run_bundled_python
)

echo Wraeclast Ledger requires Python 3.11 or newer.
echo Install Python from https://www.python.org/downloads/ and run this launcher again.
exit /b 1

:run_py
py -3 -m poe_advisor serve --open
exit /b %errorlevel%

:run_python
python -m poe_advisor serve --open
exit /b %errorlevel%

:run_python3
python3 -m poe_advisor serve --open
exit /b %errorlevel%

:run_bundled_python
"%BUNDLED_PYTHON%" -m poe_advisor serve --open
exit /b %errorlevel%
