@echo off
chcp 65001 >nul
rem 安装文件服务：开机自启 + 放行 8000 端口（需要管理员权限：右键，以管理员身份运行）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0文件服务.ps1" install
echo.
pause
