@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>nul
pushd "%~dp0" || goto :fatal

set "PORT=18976"
set "APP_URL=http://127.0.0.1:%PORT%"
set "STOP_MARKER=.token_benchmark_stop_requested"
set "EXIT_CODE=0"
set "STEP_CODE=0"
set "PYTHON_EXE="
set "BROWSER_ARG="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

if defined CODEX_PYTHON goto :use_configured_python
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE goto :missing_python
goto :check_python_version

:use_configured_python
set "PYTHON_EXE=%CODEX_PYTHON%"
if not exist "%PYTHON_EXE%" goto :missing_python
goto :check_python_version

:check_python_version
"%PYTHON_EXE%" -X utf8 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
set "STEP_CODE=%ERRORLEVEL%"
if not "%STEP_CODE%"=="0" goto :unsupported_python

:check_dependencies
"%PYTHON_EXE%" -X utf8 -c "import fastapi, uvicorn, openai, websockets, tiktoken"
set "STEP_CODE=%ERRORLEVEL%"
if not "%STEP_CODE%"=="0" goto :missing_dependencies
if "%CODEX_DRY_RUN%"=="1" goto :dry_run_success

:check_existing_service
"%PYTHON_EXE%" -X utf8 -c "import json,urllib.request; data=json.load(urllib.request.urlopen('%APP_URL%/healthz', timeout=1)); raise SystemExit(0 if data.get('app') == 'SpectrumBench' and data.get('status') == 'ok' else 1)" >nul 2>nul
set "STEP_CODE=%ERRORLEVEL%"
if "%STEP_CODE%"=="0" goto :already_running

call :message start "%APP_URL%" "%CODEX_NO_BROWSER%"

if "%CODEX_NO_BROWSER%"=="1" set "BROWSER_ARG=--no-browser"
del /q "%STOP_MARKER%" >nul 2>nul
"%PYTHON_EXE%" -X utf8 -u "token_stress_test.py" %BROWSER_ARG%
set "STEP_CODE=%ERRORLEVEL%"
if exist "%STOP_MARKER%" goto :stopped_by_user
if not "%STEP_CODE%"=="0" goto :run_failed

call :message normal_end
goto :success

:stopped_by_user
del /q "%STOP_MARKER%" >nul 2>nul
call :message stopped_by_user
goto :success

:dry_run_success
call :message dry_run
goto :success

:already_running
call :message already_running "%APP_URL%"
if not "%CODEX_NO_BROWSER%"=="1" start "" "%APP_URL%" >nul 2>nul
goto :success

:missing_python
set "EXIT_CODE=2"
echo.
echo Python 3.10 or newer was not found.
echo Install Python and add python.exe to PATH, or set CODEX_PYTHON.
goto :finish

:unsupported_python
set "EXIT_CODE=5"
call :message unsupported_python "%PYTHON_EXE%"
goto :finish

:missing_dependencies
set "EXIT_CODE=3"
call :message missing_dependencies "%PYTHON_EXE%"
goto :finish

:run_failed
set "EXIT_CODE=%STEP_CODE%"
if "%EXIT_CODE%"=="0" set "EXIT_CODE=4"
call :message run_failed "%EXIT_CODE%"
goto :finish

:message
"%PYTHON_EXE%" -X utf8 "stress_tool\launcher_messages.py" "%~1" "%~2" "%~3"
exit /b 0

:success
set "EXIT_CODE=0"
goto :finish

:finish
popd
goto :finish_no_pop

:fatal
set "EXIT_CODE=10"
echo.
echo Cannot enter the launcher directory.

:finish_no_pop
if not "%CODEX_NO_PAUSE%"=="1" pause
endlocal & exit /b %EXIT_CODE%
