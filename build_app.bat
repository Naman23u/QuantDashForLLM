@echo off
cd /d "%~dp0"
echo ===================================================
echo Building QuantDash Desktop Executable with PyInstaller
echo ===================================================

echo [1/3] Terminating any running QuantDash instances...
taskkill /f /im QuantDash.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :5050 ^| findstr LISTENING') do taskkill /f /pid %%a >nul 2>&1

echo [2/3] Detecting PyInstaller environment...
set PYTHON_CMD=python
if exist "..\.venv\Scripts\python.exe" (
    set PYTHON_CMD=..\.venv\Scripts\python.exe
)

echo [3/3] Compiling standalone package...
%PYTHON_CMD% -m PyInstaller --name "QuantDash" --windowed --noconfirm --clean --collect-all webview --hidden-import quant_metrics --hidden-import worker_engine --hidden-import file_dialog_helper --add-data "static;static" --add-data "templates;templates" desktop_app.py

if %ERRORLEVEL% equ 0 (
    echo [4/4] Setting up portable distribution directories...
    if not exist "dist\QuantDash\Strategy_Files" mkdir "dist\QuantDash\Strategy_Files"
    if not exist "dist\QuantDash\Market_Data" mkdir "dist\QuantDash\Market_Data"
    
    echo Populating distribution bundle with strategies and market data...
    if exist "Strategy_Files" xcopy /s /y /i "Strategy_Files\*.py" "dist\QuantDash\Strategy_Files\" >nul 2>&1
    if exist "Market_Data" xcopy /s /y /i "Market_Data\*.parquet" "dist\QuantDash\Market_Data\" >nul 2>&1
    if exist "Market_Data" xcopy /s /y /i "Market_Data\*.csv" "dist\QuantDash\Market_Data\" >nul 2>&1

    
    echo.
    echo ===================================================
    echo Build Successful! 
    echo Executable located in: dist\QuantDash\QuantDash.exe
    echo ===================================================
) else (
    echo.
    echo [ERROR] PyInstaller build failed with exit code %ERRORLEVEL%.
)

echo.
pause
