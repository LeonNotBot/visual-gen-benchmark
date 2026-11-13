@echo off
chcp 65001 >nul
rem 卸载文件服务（需要管理员权限：右键，以管理员身份运行）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0文件服务.ps1" uninstall
echo.
pause
