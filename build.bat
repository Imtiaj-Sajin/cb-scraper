@echo off
setlocal
REM ===========================================================
REM  Build a distributable VERSION of Crunchbase Scraper.
REM
REM  Run this from the project root after you update the code.
REM  It asks for a version number and produces a folder named
REM  _v<version>  (e.g. _v1.1) containing the ready-to-run app.
REM
REM  Zip that _v<version> folder and send it to employees.
REM  Requires: Python 3 installed and on PATH.
REM ===========================================================
echo.
echo ===========================================================
echo   Build a distributable version of Crunchbase Scraper
echo ===========================================================
echo.
set /p VER=Enter version number (e.g. 1.0, 1.1, 2.0):
if "%VER%"=="" ( echo No version entered. Aborting. & pause & exit /b 1 )
set OUT=_v%VER%

echo.
echo Installing build dependencies...
python -m pip install -r requirements.txt
python -m pip install pyinstaller

echo.
echo Building version %VER% ... (this takes a few minutes)
python -m PyInstaller --onedir --name CrunchbaseScraper --console --noconfirm --clean ^
  --distpath "%TEMP%\cbs_dist" ^
  --workpath "%TEMP%\cbs_work" ^
  --add-data "templates;templates" ^
  --collect-all nodriver ^
  --collect-all cv2 ^
  app.py

if not exist "%TEMP%\cbs_dist\CrunchbaseScraper\CrunchbaseScraper.exe" (
  echo.
  echo BUILD FAILED - executable was not produced. See messages above.
  pause & exit /b 1
)

echo.
echo Packaging into  %OUT%  ...
if exist "%OUT%" rmdir /s /q "%OUT%"
move "%TEMP%\cbs_dist\CrunchbaseScraper" "%OUT%" >nul
copy /y "START HERE.txt" "%OUT%\START HERE.txt" >nul

REM clean up scratch files
del /q CrunchbaseScraper.spec >nul 2>&1
rmdir /s /q "%TEMP%\cbs_dist" >nul 2>&1
rmdir /s /q "%TEMP%\cbs_work" >nul 2>&1

echo.
echo ===========================================================
echo   DONE.  Version folder created:   %OUT%\
echo.
echo   To give it to employees:
echo     right-click the  %OUT%  folder
echo     -^> Send to -^> Compressed (zipped) folder
echo     then send the resulting  %OUT%.zip
echo ===========================================================
pause
