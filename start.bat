@echo off
REM copyright by berlonak
REM telegram: @Kilax123
cd /d "%~dp0"
if not exist .env (
  echo [ERROR] Skopiruy .env.example v .env i zapolni kluchi i Telegram ID.
  pause
  exit /b 1
)
python -m pip install -r requirements.txt
if errorlevel 1 (pause & exit /b 1)
python main.py
pause
