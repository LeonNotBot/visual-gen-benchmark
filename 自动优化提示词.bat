@echo off
chcp 65001 >nul
cd /d "%~dp0"
python "%~dp0auto_optimize.py" --file "%~dp0testset\提示词测试集.xlsx"
echo.
pause
