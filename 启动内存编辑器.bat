@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
  start "" "C:\Users\Admin\AppData\Local\Programs\Python\Python312\pythonw.exe" "mem_editor_gui.py"
) else (
  start "" pythonw "mem_editor_gui.py"
)