@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting things up, this takes a few minutes...
  py -m venv .venv
)
set PY=".venv\Scripts\python.exe"
set PIP=%PY% -m pip install --quiet --disable-pip-version-check
%PY% -c "import flask, shazamio, yt_dlp, imageio_ffmpeg, numpy, torch, demucs.apply, anthropic" 2>nul
if errorlevel 1 (
  %PIP% -r requirements.txt
  %PIP% torch --index-url https://download.pytorch.org/whl/cpu
  %PIP% --no-deps demucs
)
%PY% app.py
pause
