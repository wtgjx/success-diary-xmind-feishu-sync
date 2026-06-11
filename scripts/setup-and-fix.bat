@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
echo.
echo === 成功日记 XMind 自动同步飞书：首次配置 ===
echo.
set /p XMIND_PATH=请输入你的 XMind 文件完整路径:
set /p FEISHU_DOC=请输入你的飞书文档链接或 token:
python xmind_to_feishu.py setup --xmind "%XMIND_PATH%" --doc "%FEISHU_DOC%"
if errorlevel 1 goto error
python xmind_to_feishu.py doctor --fix
if errorlevel 1 goto error
echo.
echo 配置完成。以后保存 XMind 会自动同步飞书。
pause
exit /b 0

:error
echo.
echo 配置失败，请把上面的错误信息发给维护者。
pause
exit /b 1
