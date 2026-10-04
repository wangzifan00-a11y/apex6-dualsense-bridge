@echo off
chcp 65001 >nul
if exist "%~dp0八爪鱼震动桥.exe" (
    start "" "%~dp0八爪鱼震动桥.exe" --setup
) else (
    echo Please open the complete release folder and run the bridge with --setup.
    pause
)
