@echo off
REM Launcher for Chinese Chess Training Master (AI Xiangqi Trainer)
REM Safe path handling - work from the directory where this .bat lives

setlocal enabledelayedexpansion

REM Switch console code page to UTF-8 so any Chinese log output renders correctly
chcp 65001 >nul 2>&1

REM Resolve project dir = folder containing this .bat file (trailing slash removed)
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
cd /d "%PROJECT_DIR%"

echo ================================================
echo   AI Chinese Chess Training Master - Starting...
echo   Project dir: %PROJECT_DIR%
echo ================================================
echo.

REM -------------------------------------------------------------------
REM Locate a real Python interpreter.
REM Priority:
REM   0) Project virtual environment (venv, preferred)
REM   1) Iterate `where python` results, skipping Microsoft Store stub
REM   2) Fallback: plain `python` (user must fix PATH themselves)
REM -------------------------------------------------------------------
set "PYTHON_EXE="

if exist "%PROJECT_DIR%\venv\Scripts\python.exe" (
    set "PYTHON_EXE=%PROJECT_DIR%\venv\Scripts\python.exe"
    goto :found_python
)

REM Scan `where python` and ignore WindowsApps placeholder
for /f "usebackq delims=" %%P in (`where python 2^>nul`) do (
    set "CANDIDATE=%%P"
    if /i not "!CANDIDATE:WindowsApps=!" == "!CANDIDATE!" (
        REM skip MS Store stub
        echo   [skip] MS Store placeholder: !CANDIDATE!
    ) else (
        if exist "!CANDIDATE!" (
            set "PYTHON_EXE=!CANDIDATE!"
            goto :found_python
        )
    )
)

REM Last resort
set "PYTHON_EXE=python"

:found_python
echo   Using Python: %PYTHON_EXE%
"%PYTHON_EXE%" --version 2>&1
echo.

REM -------------------------------------------------------------------
REM Launch the GUI (main.py in project root)
REM -------------------------------------------------------------------
if not exist "%PROJECT_DIR%\main.py" (
    echo [ERROR] main.py not found in project directory: %PROJECT_DIR%
    echo         Please make sure the .bat is placed next to main.py.
    echo.
    pause
    exit /b 1
)

echo   Launching GUI via: "%PYTHON_EXE%" main.py
echo   (close this console AFTER closing the training window)
echo.
echo ------------------------------------------------

"%PYTHON_EXE%" main.py
set "EXIT_CODE=%ERRORLEVEL%"

echo ------------------------------------------------
echo.
if %EXIT_CODE% NEQ 0 (
    echo [WARN] Process exited with code %EXIT_CODE%.
    echo        If you see an ImportError, run:
    echo        "%PYTHON_EXE%" -m pip install PyQt6
    echo.
    pause
)

endlocal
exit /b %EXIT_CODE%
