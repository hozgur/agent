@echo off
setlocal
set VENV=.venv

if "%1"=="venv" goto venv
if "%1"=="install" goto install
if "%1"=="run" goto run
if "%1"=="clean" goto clean

echo Usage: make.bat [venv^|install^|run^|clean]
exit /b 1

:venv
py -3.11 -m venv %VENV%
call %VENV%\Scripts\activate.bat
python -m pip install --upgrade pip wheel
exit /b %ERRORLEVEL%

:install
call %~f0 venv
call %VENV%\Scripts\activate.bat
pip install -r requirements.txt
pip install -e .
exit /b %ERRORLEVEL%

:run
call %VENV%\Scripts\activate.bat
agent --help
exit /b %ERRORLEVEL%

:clean
rd /s /q %VENV% 2>nul
rd /s /q build 2>nul
rd /s /q dist 2>nul
for /d %%G in (*.egg-info) do rd /s /q "%%G"
exit /b 0


