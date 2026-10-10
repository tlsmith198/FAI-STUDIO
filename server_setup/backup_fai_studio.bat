@echo off
REM Nightly backup of the FAI Studio database (schedule with Windows Task Scheduler on the DB server).
REM Adjust the PostgreSQL bin path and backup folder. Uses PGPASSWORD or %APPDATA%\postgresql\pgpass.conf.
set PGBIN=C:\Program Files\PostgreSQL\16\bin
set BACKUP_DIR=D:\Backups\FAI_Studio
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set STAMP=%%i
"%PGBIN%\pg_dump.exe" -h localhost -U fai_app -F c -f "%BACKUP_DIR%\fai_studio_%STAMP%.dump" fai_studio
REM keep 30 days
forfiles /p "%BACKUP_DIR%" /m *.dump /d -30 /c "cmd /c del @path" 2>nul
