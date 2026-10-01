@echo off
REM ---------------------------------------------------------------
REM Launch the Lock-In Pump-Probe app.
REM Double-click this file, or run it from any shell (cmd/PowerShell).
REM The console window stays open so you can read the device logs
REM ([Spec], [Pump delay stage], [LockIn] ...) and any traceback.
REM ---------------------------------------------------------------
set PYEXE=C:\Users\mguizzardi\AppData\Local\anaconda3\python.exe

if not exist "%PYEXE%" (
    echo ERROR: Python not found at "%PYEXE%"
    echo Edit PYEXE in this file to point at your Anaconda python.exe
    pause
    exit /b 1
)

echo Starting Lock-In Pump-Probe...
"%PYEXE%" "%~dp0app\main.py"

if errorlevel 1 (
    echo.
    echo The app exited with an error ^(see the messages above^).
    pause
)
