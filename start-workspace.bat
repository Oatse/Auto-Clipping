@echo off
REM Start the Auto-Clipping workspace: app + Premiere Pro + bridge.
REM
REM Safe to run twice - every component is probed before anything is
REM started, so a second run adopts what is already alive instead of
REM launching duplicates.
REM
REM   start-workspace.bat              start everything, open the app
REM   start-workspace.bat --status     report what is running, start nothing
REM   start-workspace.bat --no-premiere    app only

cd /d "%~dp0"
python -m launcher %*
if errorlevel 1 (
  echo.
  echo The workspace did not start cleanly - see the messages above.
  pause
)
