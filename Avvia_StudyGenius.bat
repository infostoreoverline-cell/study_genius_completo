@echo off
setlocal
cd /d "%~dp0"
title StudyGenius
where py >nul 2>&1
if %errorlevel% equ 0 (
    py -3 scripts\bootstrap.py
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo Installa Python 3.12 da https://www.python.org/downloads/windows/
        echo Durante l'installazione seleziona Add Python to PATH.
        pause
        exit /b 1
    )
    python scripts\bootstrap.py
)
if errorlevel 1 (
    echo.
    echo Avvio non riuscito. Leggi il messaggio sopra e la guida README.md.
    pause
)
