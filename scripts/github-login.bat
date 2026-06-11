@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
set "PATH=%ProgramFiles%\GitHub CLI;%PATH%"
echo === GitHub CLI 登录 ===
echo.
echo 这个窗口会打开 GitHub 授权页面。
echo 如果页面显示验证码，验证码通常已经复制到剪贴板。
echo.
gh auth login --web --clipboard --git-protocol https --hostname github.com 2>&1
echo.
echo === 登录状态 ===
gh auth status 2>&1
echo.
pause
