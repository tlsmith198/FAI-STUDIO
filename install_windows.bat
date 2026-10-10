@echo off
REM ---- ADMI FAI Studio: install / upgrade on a Windows workstation ----
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python is required. Install Python 3.12 from https://www.python.org/downloads/
  echo ^(tick "Add python.exe to PATH"^) and run this again.
  pause
  exit /b 1
)
REM Prefer Python 3.12 (widest package support); fall back to whatever is installed
set PYV=
for %%v in (3.12 3.13 3.11 3.14 3.10) do (
  if not defined PYV (
    py -%%v -c "import sys" >nul 2>nul && set PYV=%%v
  )
)
if not defined PYV set PYV=3
echo Using Python %PYV%
if exist .venv rmdir /s /q .venv
py -%PYV% -m venv .venv
if errorlevel 1 (
  echo Failed to create the virtual environment.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Package install failed - see the messages above.
  pause
  exit /b 1
)
echo.
echo Installing OCR for scanned drawings ^(optional^)...
python -m pip install --prefer-binary -r requirements-ocr.txt
if errorlevel 1 python -m pip install --only-binary=:all: -r requirements-ocr.txt
if errorlevel 1 (
  echo.
  echo  NOTE: OCR could not be installed on this Python. FAI Studio works normally;
  echo  only auto-ballooning of SCANNED drawings is unavailable. Vector CAD PDFs are unaffected.
)
REM Desktop shortcut
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\FAI Studio.lnk');$s.TargetPath='%~dp0.venv\Scripts\pythonw.exe';$s.Arguments='\"%~dp0run.py\"';$s.WorkingDirectory='%~dp0';$s.Save()"
echo.
echo Installed. Start it with the "FAI Studio" desktop shortcut or run_windows.bat
pause
