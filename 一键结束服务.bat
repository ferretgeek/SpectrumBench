@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>nul
pushd "%~dp0" || goto :fatal

set "PORT=18976"
set "STOP_MARKER=.token_benchmark_stop_requested"
set "PYTHON_EXE="
set "FOUND=0"
set "FAILED=0"
set "EXIT_CODE=0"
set "STEP_CODE=0"
set "STILL_LISTENING=0"

if defined CODEX_PYTHON if exist "%CODEX_PYTHON%" set "PYTHON_EXE=%CODEX_PYTHON%"
if not defined PYTHON_EXE for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"

where netstat >nul 2>nul
set "STEP_CODE=%ERRORLEVEL%"
if not "%STEP_CODE%"=="0" goto :missing_tool

call :message stop_header "%PORT%"

netstat -ano -p tcp | findstr /r /c:":%PORT% .*LISTENING" >nul
set "STEP_CODE=%ERRORLEVEL%"
if not "%STEP_CODE%"=="0" goto :nothing_to_stop

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command "$ProgressPreference='SilentlyContinue'; try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:%PORT%/healthz' -TimeoutSec 2; if ($r.app -eq 'SpectrumBench' -and $r.status -eq 'ok') { exit 0 } } catch {}; exit 1" >nul 2>nul
set "STEP_CODE=%ERRORLEVEL%"
if not "%STEP_CODE%"=="0" goto :identity_refused

for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /r /c:":%PORT% .*LISTENING"') do call :stop_pid "%%P"

if "%FOUND%"=="0" goto :nothing_to_stop
if not "%FAILED%"=="0" goto :stop_failed

"%SystemRoot%\System32\ping.exe" -n 2 127.0.0.1 >nul 2>nul
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /r /c:":%PORT% .*LISTENING"') do set "STILL_LISTENING=1"
if "%STILL_LISTENING%"=="1" goto :verify_failed

call :message stop_success "%PORT%"
goto :success

:stop_pid
set "PID=%~1"
if "%PID%"=="" exit /b 0
if "%PID%"=="0" exit /b 0
set "FOUND=1"
tasklist /FI "PID eq %PID%" 2>nul | findstr /r /c:"[ ]%PID%[ ]" >nul
set "STEP_CODE=%ERRORLEVEL%"
if not "%STEP_CODE%"=="0" exit /b 0
call :message stopping_pid "%PID%"
type nul > "%STOP_MARKER%"
taskkill /PID "%PID%" /F >nul 2>nul
set "STEP_CODE=%ERRORLEVEL%"
if "%STEP_CODE%"=="0" exit /b 0
set "FAILED=1"
del /q "%STOP_MARKER%" >nul 2>nul
call :message taskkill_failed "%PID%" "%STEP_CODE%"
exit /b 0

:nothing_to_stop
del /q "%STOP_MARKER%" >nul 2>nul
call :message nothing_to_stop "%PORT%"
goto :success

:missing_tool
set "EXIT_CODE=2"
call :message missing_netstat
goto :finish

:identity_refused
set "EXIT_CODE=5"
call :message identity_refused "%PORT%"
goto :finish

:stop_failed
set "EXIT_CODE=3"
del /q "%STOP_MARKER%" >nul 2>nul
call :message stop_failed "%PORT%"
goto :finish

:verify_failed
set "EXIT_CODE=4"
del /q "%STOP_MARKER%" >nul 2>nul
call :message verify_failed "%PORT%"
goto :finish

:message
if not defined PYTHON_EXE goto :message_fallback
"%PYTHON_EXE%" -X utf8 "stress_tool\launcher_messages.py" "%~1" "%~2" "%~3"
exit /b 0

:message_fallback
echo %~1 %~2 %~3
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
