@echo off
echo.
echo ========================================
echo Image to PDF Converter - PyQt5 (Optimized)
echo Building EXE (No DLL Issues!)
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found!
    pause
    exit /b 1
)

echo [1/3] Installing PyQt5 and dependencies...
pip install -r requirements_pyqt5.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies!
    pause
    exit /b 1
)

echo.
echo [2/3] Installing PyInstaller...
pip install pyinstaller
if errorlevel 1 (
    echo ERROR: Failed to install PyInstaller!
    pause
    exit /b 1
)

echo.
echo [3/3] Building EXE (takes 2-3 minutes)...
pyinstaller --onefile --windowed image_to_pdf_converter_pyqt5_optimized.py
if errorlevel 1 (
    echo ERROR: Build failed!
    pause
    exit /b 1
)

echo.
echo Cleaning up...
rmdir /s /q build 2>nul
del *.spec 2>nul

echo.
echo ========================================
echo SUCCESS! Your EXE is ready!
echo ========================================
echo.
echo Location: %cd%\dist\image_to_pdf_converter_pyqt5_optimized.exe
echo.
echo Next: Double-click the EXE to run it!
echo.
pause
