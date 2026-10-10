@echo off
REM ---- Build a stand-alone FAI Studio folder (no Python needed on the other PCs) ----
cd /d "%~dp0"
call .venv\Scripts\activate.bat || (echo Run install_windows.bat first & pause & exit /b 1)
python -m pip install pyinstaller
pyinstaller --noconfirm --clean --windowed --name "FAI Studio" ^
  --add-data "fai_studio\schema.sql;fai_studio" ^
  --add-data "fai_studio\assets;fai_studio\assets" ^
  --collect-submodules fai_studio ^
  --collect-all rapidocr ^
  --collect-all onnxruntime ^
  --hidden-import cv2 ^
  run.py
echo.
echo Done. Copy the folder  dist\FAI Studio  to each workstation (or a network share) and run "FAI Studio.exe".
pause
