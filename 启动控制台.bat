@echo off
chcp 936 >nul
title PyramidSeek 跑批控制台
cd /d "%~dp0"

set "PORT=5055"
rem 子进程的 UTF-8 由 console.runner 强制注入，这里不设 PYTHONUTF8，
rem 否则控制台自身的 print 会变成 UTF-8 字节、在 936 代码页的窗口里反而乱码

echo ============================================================
echo   PyramidSeek 跑批控制台
echo   工作目录: %CD%
echo   地址:     http://127.0.0.1:%PORT%
echo ============================================================
echo.

rem 找一个装了依赖的解释器：先试 python，再试 Windows 的 py 启动器
set "PY=python"
python -c "import flask, pandas, duckdb" >nul 2>&1
if errorlevel 1 (
  set "PY=py -3"
  py -3 -c "import flask, pandas, duckdb" >nul 2>&1
)

%PY% -c "import flask, pandas, duckdb" >nul 2>&1
if errorlevel 1 (
  echo [错误] 找不到可用的 Python，或依赖不完整（flask / pandas / duckdb）。
  echo.
  echo   当前 python:
  where python 2>nul
  echo.
  echo   请任选其一修复：
  echo     1^) 安装 Python 3.11+ 并勾选 Add Python to PATH
  echo     2^) 在本目录执行: pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul
if not errorlevel 1 (
  echo [提示] 端口 %PORT% 已被占用，控制台可能已在运行。
  echo        正在打开浏览器 http://127.0.0.1:%PORT%
  echo.
  start "" "http://127.0.0.1:%PORT%"
  timeout /t 3 >nul
  exit /b 0
)

echo 解释器: %PY%
echo 正在启动，浏览器约 3 秒后自动打开...
echo.
echo   提示: 关闭本窗口 = 停止控制台（正在跑的跑批也会中断）；
echo         跑批过程中可以关掉浏览器，跑批不受影响。
echo.
start "" /min cmd /c "ping -n 4 127.0.0.1 >nul & start http://127.0.0.1:%PORT%"
%PY% -m console.app --port %PORT%

echo.
echo 控制台已退出。
pause
