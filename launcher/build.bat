@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "CSC=C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" (
  echo [GuWen] csc.exe not found. Install .NET Framework 4.x first.
  pause
  exit /b 1
)

"%CSC%" /nologo /target:winexe /optimize+ ^
  /win32icon:..\app\static\guwen.ico ^
  /r:System.dll /r:System.Windows.Forms.dll /r:System.Drawing.dll ^
  /out:GuWen.exe TrayLauncher.cs

if errorlevel 1 (
  echo [GuWen] build failed.
  pause
  exit /b 1
)
echo [GuWen] built launcher\GuWen.exe
pause
