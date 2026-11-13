@echo off
chcp 65001 >nul
cd /d "%~dp0"
set OUT=%~dp0runs\run_%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%_%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%
set OUT=%OUT: =0%
python "%~dp0run_bench.py" --file "%~dp0testset\提示词测试集.xlsx" --out "%OUT%" --skip-optimize --skip-decompose
echo.
pause
