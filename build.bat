@echo off
echo Installing dependencies...
pip install -r requirements.txt
pip install pyinstaller pywebview

echo.
echo Building PropCopy.exe...
pyinstaller propcopy.spec --clean

echo.
if exist dist\PropCopy.exe (
    echo Build complete: dist\PropCopy.exe
) else (
    echo BUILD FAILED - check output above for errors
)
pause
