@echo off
rem Fetch the official embeddable Python into runtime\ so anki-radar runs on a
rem machine with no Python installed. Copy the whole folder anywhere and it works.
rem
rem ASCII only, CRLF line endings, on purpose: chcp switches the console code page
rem and cmd.exe keeps reading this file by byte offset afterwards (same note as
rem run.bat). Run this once; run.bat picks the runtime up automatically.
setlocal
chcp 65001 >nul

rem One line to change when moving to a newer Python. Any 3.x works: the radar
rem imports nothing outside the standard library.
set "VER=3.13.15"

set "HERE=%~dp0"
set "DEST=%HERE%runtime"
set "URL=https://www.python.org/ftp/python/%VER%/python-%VER%-embed-amd64.zip"

if exist "%DEST%\python.exe" (
  echo Runtime already present, skipping download.
  goto :patch
)

echo Downloading %URL%
if not exist "%DEST%" mkdir "%DEST%"
rem Download AND unpack from inside the target directory, using a relative file
rem name. Two traps here, both hit on 2026-09-27, both of which report something
rem that sounds like a network problem and is not one:
rem
rem   1. Call tar by bare name and you may get Git's GNU tar, which cannot read
rem      zip at all: "This does not look like a tar archive".
rem      So call the Windows one by full path.
rem   2. Hand bsdtar an absolute path and it reads "C:\..." as a remote host:
rem      "Cannot connect to C: resolve failed".
rem      So unpack from inside the directory with a relative name.
pushd "%DEST%"
curl -L --fail --max-time 300 -o py-embed.zip "%URL%"
if errorlevel 1 (popd & goto :fail)
echo Extracting to %DEST%
"%SystemRoot%\System32\tar.exe" -xf py-embed.zip
if errorlevel 1 (
  echo Windows tar not usable, falling back to PowerShell.
  powershell -NoProfile -Command "Expand-Archive -LiteralPath 'py-embed.zip' -DestinationPath '.' -Force"
)
if not exist "%DEST%\python.exe" (popd & goto :fail)
del py-embed.zip
popd

:patch
rem The embeddable package ships pythonXY._pth, which PINS sys.path and disables
rem site. The radar's own modules sit one level up from runtime\, so that
rem directory must be listed in there, or `import ui` dies with
rem ModuleNotFoundError while `python -c "import json"` still works fine --
rem a confusing failure that looks like the download went wrong.
"%DEST%\python.exe" "%HERE%tools\prepare_runtime.py" "%DEST%"
if errorlevel 1 goto :fail

:verify
echo.
echo Verifying...
"%DEST%\python.exe" "%HERE%tools\verify_runtime.py"
if errorlevel 1 goto :fail
echo.
echo Runtime ready. Double-click run.bat as usual.
endlocal
exit /b 0

:fail
echo.
echo FAILED. Nothing was changed on your machine outside %DEST%.
echo If the download is the problem, fetch the zip by hand:
echo   %URL%
echo and unzip it into %DEST%, then run this file again.
pause
endlocal
exit /b 1
