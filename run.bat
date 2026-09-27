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
rem Prefer the runtime that setup-runtime.bat put next to this file: it is the
rem official embeddable Python, self contained, so this whole folder can be copied
rem to any Windows machine and nothing needs to be installed.
set "PY=%~dp0runtime\python.exe"
if exist "%PY%" goto :haspy

rem Fall back to an installed Anaconda, which is how this ran before the bundled
rem runtime existed. Anaconda needs Library\bin on PATH or `import ssl` fails with
rem a DLL load error, and every source is HTTPS -- the bundled runtime has no such
rem fragility, which is part of why it is preferred above.
set "PY=%USERPROFILE%\anaconda3\python.exe"
set "PATH=%USERPROFILE%\anaconda3\Library\bin;%USERPROFILE%\anaconda3;%PATH%"
if exist "%PY%" goto :haspy

echo No Python found.
echo   Easiest fix: run setup-runtime.bat once. It downloads the official
echo   embeddable Python (about 10 MB) into runtime\ and verifies it.
echo   Or edit the PY line in this file to point at your own python.exe
pause
exit /b 1

:haspy
rem Double-clicking (no arguments) opens the local web page: it scans the Anki
rem forum right away (seconds) and leaves Reddit to a button, because a Reddit
rem pass needs a 20s pause between requests (~5 min). Close it with Ctrl-C.
rem One-off command line runs still work: run.bat --forum-only --no-open
set "ARGS=%*"
if "%~1"=="" set "ARGS=--serve"
if /I "%~1"=="--all" set "ARGS="
"%PY%" "%~dp0radar.py" %ARGS%
if errorlevel 1 pause
