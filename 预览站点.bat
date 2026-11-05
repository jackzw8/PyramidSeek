@echo off
chcp 936 >nul
title PyramidSeek 站点预览（public/）
cd /d "%~dp0"

set "PORT=8000"
set "ROOT=%~dp0assets\public"

echo ============================================================
echo   PyramidSeek 站点预览
echo   目录: %ROOT%
echo   地址: http://127.0.0.1:%PORT%/
echo ============================================================
echo.

rem 找一个可用的解释器：先试 python，再试 Windows 的 py 启动器
set "PY=python"
python -c "import http.server" >nul 2>&1
if errorlevel 1 (
  set "PY=py -3"
  py -3 -c "import http.server" >nul 2>&1
)
%PY% -c "import http.server" >nul 2>&1
if errorlevel 1 (
  echo [错误] 找不到可用的 Python。
  echo.
  where python 2>nul
  pause
  exit /b 1
)

if not exist "%ROOT%\index.html" (
  echo [错误] 找不到 %ROOT%\index.html
  echo        请先跑一次批（产物在 public/data/），或确认目录结构。
  pause
  exit /b 1
)

netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul
if not errorlevel 1 (
  echo [提示] 端口 %PORT% 已被占用，预览服务可能已在运行。
  echo        正在打开浏览器 http://127.0.0.1:%PORT%/
  echo.
  start "" "http://127.0.0.1:%PORT%/"
  timeout /t 3 >nul
  exit /b 0
)

echo 正在启动，浏览器约 2 秒后自动打开...
echo.
echo   提示: 按 Ctrl+C 停止预览。
echo         本页面读的是 public/ 里的产物 —— 跑完批刷新页面即可看到新数据；
echo         上线走的是 dist/，与这里无关。
echo.
start "" /min cmd /c "ping -n 3 127.0.0.1 >nul & start http://127.0.0.1:%PORT%/"
%PY% -m http.server %PORT% -d "%ROOT%"

echo.
echo 预览已停止。
pause
