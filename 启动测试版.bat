@echo off
rem 注意：chcp 必须紧跟 @echo off，放在文件最前面。
rem cmd.exe 是按**系统代码页(GBK)**解析 .bat 字节的，而本文件是 UTF-8；
rem 如果 UTF-8 中文串里恰好出现能拼成 ")" 的字节对，括号会被当成 if 块的
rem 结束符，直接报 "... was unexpected at this time."。
rem 先把代码页切到 65001，后面的解析都按 UTF-8 走，才安全。
chcp 65001 >nul 2>nul
rem ===========================================================================
rem  三角洲行动 · 钓鱼成本核算器  ——  【测试版】启动器
rem
rem  用途：在「生产环境」(源码树) 里直接以源码方式运行程序，供手动验证。
rem        不需要打包、不需要 exe。
rem
rem  用法：双击本文件即可。
rem        也可以在本目录开命令行：
rem            启动测试版.bat            普通启动
rem            启动测试版.bat check      只做数据自检，不开窗口
rem
rem  与「启动核算器.bat」的区别：
rem    * Python 查找更可靠（py -3 / python / 常见安装路径都试）
rem    * 启动失败时把错误留在屏幕上并暂停，不会一闪而过
rem    * 退出后不暂停（正常关窗口就是正常退出）
rem    * 支持 check 一个参数
rem ===========================================================================

setlocal EnableExtensions
chcp 65001 >nul 2>nul
cd /d "%~dp0"

set "TITLE=三角洲钓鱼成本核算器 - 测试版(源码运行)"
title %TITLE%

rem ---- 1. 确认在正确的位置 ------------------------------------------------
if not exist "main.py" (
    echo.
    echo [错误] 这个启动器必须放在项目根目录，和 main.py 在一起。
    echo        当前目录: %CD%
    echo        找不到:   %CD%\main.py
    echo.
    pause
    exit /b 1
)
if not exist "src\diaoyu\gui.py" (
    echo.
    echo [错误] 找不到源码 src\diaoyu\gui.py
    echo        请确认整个项目目录是完整的。
    echo.
    pause
    exit /b 1
)

rem ---- 2. 找一个能用的 Python ---------------------------------------------
set "PY="

rem 优先用虚拟环境（如果项目里有）
if exist ".venv\Scripts\python.exe" set "PY=%CD%\.venv\Scripts\python.exe"
if not defined PY if exist "venv\Scripts\python.exe" set "PY=%CD%\venv\Scripts\python.exe"

rem 其次用 py 启动器（Windows 官方推荐方式，能自动挑最新版 3.x）
if not defined PY (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)

rem 再次用 PATH 里的 python
if not defined PY (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)

rem 最后试几个常见安装位置
if not defined PY (
    for %%D in (
        "%LOCALAPPDATA%\Programs\Python\Python313"
        "%LOCALAPPDATA%\Programs\Python\Python312"
        "C:\Python313"
        "C:\Python312"
        "C:\Program Files\Python313"
        "C:\Program Files\Python312"
    ) do (
        if not defined PY if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
    )
)

if not defined PY (
    echo.
    echo [错误] 找不到可用的 Python。
    echo.
    echo   这个启动器是「源码运行」用的，需要机器上装 Python 3.13 或更高。
    echo   如果你手上是打包好的免安装版，请改用那个文件夹里的
    echo   「启动.cmd」或「三角洲钓鱼成本核算器.exe」。
    echo.
    echo   安装 Python: https://www.python.org/downloads/
    echo   安装时记得勾选 "Add python.exe to PATH"。
    echo.
    pause
    exit /b 1
)

rem ---- 3. 版本检查（需要 3.10+，开发用的是 3.13） --------------------------
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [错误] Python 版本太低，需要 3.10 或更高。
    %PY% -c "import sys; print('        当前版本:', sys.version.split()[0])"
    echo.
    pause
    exit /b 1
)

rem ---- 4. 判断模式 --------------------------------------------------------
rem 注意：不要写成 `if ... set A & set B`，`&` 会把命令截断，
rem 第二个 set 会在**任何**情况下执行，模式就串了。
set "ARGS="
set "MODE=普通启动"
if /i "%~1"=="check" (
    set "ARGS=--check"
    set "MODE=数据自检"
)

echo.
echo   三角洲行动 · 钓鱼成本核算器  [测试版 / 源码运行]
echo   ------------------------------------------------------------------
%PY% -c "import sys; print('  Python :', sys.version.split()[0], '(', sys.executable, ')')"
echo   模式   : %MODE%
echo   目录   : %CD%
echo   ------------------------------------------------------------------
echo.

if /i "%~1"=="check" (
    %PY% main.py %ARGS%
    echo.
    echo   [自检完成] 上面是结果。按任意键关闭窗口。
    pause >nul
    exit /b 0
)

rem ---- 5. 启动图形界面 ----------------------------------------------------
rem 用 pythonw 启动可以不留控制台；但调试时想看到报错，所以这里用 python，
rem 并把控制台最小化交给用户自己处理。出错时窗口会留着显示 traceback。
%PY% main.py %ARGS%

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo   ------------------------------------------------------------------
    echo   [程序异常退出] 退出码 %RC%
    echo.
    echo   常见原因：
    echo     1) 数据文件被改坏  ^>  把 data 目录改名后重启，会重新生成一份
    echo     2) 缺少 tcl/tk      ^>  重装 Python 时勾上 "tcl/tk and IDLE"
    echo     3) 权限/路径问题    ^>  换个普通目录(不要放 C:\Program Files)
    echo.
    echo   想看详细数据自检：
    echo       %PY% main.py --check
    echo   ------------------------------------------------------------------
    echo.
    echo   按任意键关闭。
    pause >nul
)

endlocal
exit /b 0
