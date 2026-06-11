@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
python xmind_to_feishu.py task-status
pause
