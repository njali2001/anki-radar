@echo off
rem anki-radar launcher. Double-click, or pass arguments: run.bat --forum-only
rem
rem ASCII only, CRLF line endings, on purpose: chcp switches the console code page
rem and cmd.exe keeps reading this file by byte offset afterwards. A multi-byte
rem character above would shift those offsets and break the rest of the script.
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PY=%USERPROFILE%\anaconda3\python.exe"
rem Anaconda needs Library\bin on PATH or `import ssl` fails (DLL load failed),
rem and both sources are HTTPS.
set "PATH=%USERPROFILE%\anaconda3\Library\bin;%USERPROFILE%\anaconda3;%PATH%"
if not exist "%PY%" (
  echo Python not found at %PY%
  echo Edit the PY line in this file to point at your python.exe
  pause
  exit /b 1
)
rem Double-clicking (no arguments) opens the local web page: it scans the Anki
rem forum right away (seconds) and leaves Reddit to a button, because a Reddit
rem pass needs a 20s pause between requests (~5 min). Close it with Ctrl-C.
rem One-off command line runs still work: run.bat --forum-only --no-open
set "ARGS=%*"
if "%~1"=="" set "ARGS=--serve"
if /I "%~1"=="--all" set "ARGS="
"%PY%" "%~dp0radar.py" %ARGS%
if errorlevel 1 pause
