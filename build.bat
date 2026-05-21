@echo off
REM ===========================================================
REM  Build the distributable CrunchbaseScraper.exe folder.
REM  Run this from the project root after any code change.
REM  Requires: Python 3 installed and on PATH.
REM ===========================================================
echo.
echo Installing build dependencies...
python -m pip install -r requirements.txt
python -m pip install pyinstaller

echo.
echo Building the executable bundle...
python -m PyInstaller --onedir --name CrunchbaseScraper --console --noconfirm --clean ^
  --add-data "templates;templates" ^
  --collect-all nodriver ^
  --collect-all cv2 ^
  app.py

echo.
echo ===========================================================
echo  Done.  Distributable folder:  dist\CrunchbaseScraper
echo  Zip that folder and send it to employees.
echo ===========================================================
pause
