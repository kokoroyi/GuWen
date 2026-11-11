@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo [GuWen] Creating .venv and installing Python dependencies...
if not exist ".venv\Scripts\python.exe" (
  set "PYTHON=python"
  where py >nul 2>nul && set "PYTHON=py -3"
  %PYTHON% -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo [GuWen] venv creation failed. Install Python 3.11-3.12 first.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo [GuWen] Done. Run start.bat, then open http://127.0.0.1:8770/
pause
exit /b 0

:failed
echo.
echo [GuWen] Dependency installation failed. Check the message above.
pause
exit /b 1
