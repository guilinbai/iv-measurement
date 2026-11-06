@echo off
chcp 936 >nul
cd /d "%~dp0"
title IV-4CH-Monitor

set "PYEXE="
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set PYEXE=%LOCALAPPDATA%\Programs\Python\Python310\python.exe
if not defined PYEXE if exist "C:\Python313\python.exe" set PYEXE=C:\Python313\python.exe
if not defined PYEXE if exist "C:\Python312\python.exe" set PYEXE=C:\Python312\python.exe
if not defined PYEXE if exist "C:\Python311\python.exe" set PYEXE=C:\Python311\python.exe
if not defined PYEXE set PYEXE=python

echo.
echo   Python : %PYEXE%
echo   Folder : %CD%
echo   Starting GUI, please wait...
echo.

"%PYEXE%" main_gui.py %*
set RC=%ERRORLEVEL%

if not "%RC%"=="0" goto FAILED
exit /b 0

:FAILED
echo.
echo   ==========================================================
echo   [程序异常退出，返回码 %RC%]
echo   请把上面这段错误信息截图保存，方便排查问题。
echo   ==========================================================
echo.
pause
exit /b %RC%
