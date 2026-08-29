@echo off
rem ProtoBot launcher for the portable release.
rem No install step: anything you would type as "protobot ..." goes after this
rem script, e.g. "protobot.bat run", "protobot.bat login", "protobot.bat setup".
rem Only Python 3.12+ is required.
setlocal

where python >nul 2>nul
if %errorlevel%==0 (
    python -m protobot.cli_app %*
    exit /b %errorlevel%
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m protobot.cli_app %*
    exit /b %errorlevel%
)

echo [error] Python 3.12 or newer is required but not found.
echo         Install it from https://www.python.org/downloads/ and run this again.
exit /b 1
