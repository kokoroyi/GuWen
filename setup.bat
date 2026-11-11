@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo [GuWen] Creating .venv and installing Python dependencies...

if exist ".venv\Scripts\python.exe" goto :install

set "PYTHON=python"
where py >nul 2>nul && set "PYTHON=py -3.12"
%PYTHON% -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo [GuWen] Python 3.12 not found, trying python on PATH...
  python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo [GuWen] venv creation failed. Install Python 3.11 or 3.12 first.
  pause
  exit /b 1
)

:install
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if (3,11)<=sys.version_info<(3,13) else 1)"
if errorlevel 1 (
  echo [GuWen] This project requires Python 3.11 or 3.12. Delete .venv and install a supported version.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo [GuWen] Done. Double-click GuWen.exe or run start.bat.
pause
exit /b 0

:failed
echo.
echo [GuWen] Dependency installation failed. Check the message above.
pause
exit /b 1
