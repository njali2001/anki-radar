@echo off
rem anki-radar 启动器。双击即可，也可以带参数：run.bat --sample
rem
rem 【要先把 Anaconda 的 Library\bin 加进 PATH】：这台机器上的 Python 不把它
rem 放进搜索路径时 import ssl 会失败（DLL load failed），而 Reddit 的 API 是 HTTPS。
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set "PY=%USERPROFILE%\anaconda3\python.exe"
set "PATH=%USERPROFILE%\anaconda3\Library\bin;%USERPROFILE%\anaconda3;%PATH%"
if not exist "%PY%" (
  echo 找不到 %PY% —— 请改这个文件里的 PY 变量指向你的 python.exe
  pause
  exit /b 1
)
"%PY%" "%~dp0radar.py" %*
if errorlevel 1 pause
