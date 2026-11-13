@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
rem 临时手动启动文件服务（关掉窗口就停）。长期使用请运行 安装文件服务.bat
set "ROOT=%~dp0runs"
if not "%EVAL_FILE_ROOT%"=="" set "ROOT=%EVAL_FILE_ROOT%"
if not exist "%ROOT%" mkdir "%ROOT%"
echo 共享目录：!ROOT!
echo 浏览器打开 http://本机IP:8000/ ，按 Ctrl+C 停止
python -m http.server 8000 --bind 0.0.0.0 --directory "%ROOT%"
pause
