@echo off
cd /d %~dp0
if "%REPLY_MODE%"=="" set REPLY_MODE=login_ok
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" server\run_all.py
) else if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" server\run_all.py
) else (
  py server\run_all.py
)
