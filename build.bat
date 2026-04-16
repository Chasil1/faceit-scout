@echo off
echo Installing PyInstaller...
pip install pyinstaller

echo.
echo Building executable...
pyinstaller faceit_scout.spec --clean

echo.
echo Done! Check dist\FaceitScout.exe
pause
