@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title Pai AI Movie Studio (v2.3) - Master Launcher
color 0B

:: 1. Python Environment Detection
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

set "CLI_MODE="
if not "%~1"=="" set "CLI_MODE=1"

:: 2. Command-Line Arguments & Shortcuts Dispatcher
if "%~1"=="" goto MENU
if /i "%~1"=="--web" goto WEBUI
if /i "%~1"=="web" goto WEBUI
if /i "%~1"=="1" goto WEBUI
if /i "%~1"=="--batch" goto RECAP_BATCH
if /i "%~1"=="batch" goto RECAP_BATCH
if /i "%~1"=="2" goto RECAP_BATCH
if /i "%~1"=="--recap" goto RECAP_SINGLE
if /i "%~1"=="recap" goto RECAP_SINGLE
if /i "%~1"=="3" goto RECAP_SINGLE
if /i "%~1"=="--hardsub" goto HARDSUB
if /i "%~1"=="hardsub" goto HARDSUB
if /i "%~1"=="4" goto HARDSUB
if /i "%~1"=="--subtitle" goto SUBTITLE
if /i "%~1"=="subtitle" goto SUBTITLE
if /i "%~1"=="5" goto SUBTITLE
if /i "%~1"=="--clean" goto CLEAN
if /i "%~1"=="clean" goto CLEAN
if /i "%~1"=="6" goto CLEAN
if /i "%~1"=="--health" goto HEALTH
if /i "%~1"=="health" goto HEALTH
if /i "%~1"=="7" goto HEALTH
if /i "%~1"=="--docker" goto DOCKER
if /i "%~1"=="docker" goto DOCKER
if /i "%~1"=="8" goto DOCKER
if /i "%~1"=="--update" goto UPDATE
if /i "%~1"=="update" goto UPDATE
if /i "%~1"=="9" goto UPDATE

:: Drag-and-drop file onto Start_Studio.bat
if exist "%~1" (
    set "DRAG_FILE=%~1"
    goto DRAG_MENU
)

set "input_src=%~1"
goto RECAP_DIRECT

:DRAG_MENU
cls
echo ===============================================================================
echo            PAI AI MOVIE STUDIO - FILE DETECTED
echo ===============================================================================
echo.
echo Input File: "%DRAG_FILE%"
echo.
echo Select Processing Engine for this file:
echo.
echo    [1] Movie Recap Studio (AI Dubbing + Recap Narration)
echo    [2] Original Audio and Burmese Hardsub Studio (Engine 3 - 100%% Audio Preserved)
echo    [3] YouTube Subtitle and Transcript Studio (Engine 2 - Pure Subtitles)
echo    [0] Return to Main Menu
echo.
echo ===============================================================================
set "dd_choice="
set /p "dd_choice=Select Engine [1-3, Default=1]: "
if not defined dd_choice set "dd_choice=1"
if "%dd_choice%"=="1" (
    set "input_src=%DRAG_FILE%"
    goto RECAP_DIRECT
)
if "%dd_choice%"=="2" (
    set "hs_input=%DRAG_FILE%"
    goto HARDSUB_CONFIG
)
if "%dd_choice%"=="3" (
    set "sub_input=%DRAG_FILE%"
    goto SUBTITLE_CONFIG
)
goto MENU

:MENU
cls
echo ===============================================================================
echo            PAI AI MOVIE STUDIO (v2.3) - ALL-IN-ONE MASTER LAUNCHER
echo ===============================================================================
echo.
echo    [1] Launch Interactive Web UI Dashboard (Recommended / Default: Enter)
echo    [2] Run Movie Recap Studio (Batch Mode - Process all in "movies/")
echo    [3] Run Movie Recap Studio for Single Video / URL
echo    [4] Run Original Audio and Burmese Hardsub Studio (Engine 3)
echo    [5] Run YouTube Subtitle and Transcript Studio (Engine 2)
echo    [6] Open Studio Cleanup Utility (Delete cache / old outputs)
echo    [7] Run System Health and Hardware Diagnostics
echo    [8] Run Pai AI Studio in Docker Container
echo    [9] Check & Pull Latest Updates from GitHub (git pull)
echo    [0] Exit
echo.
echo ===============================================================================
set "choice="
set /p "choice=Select an option [0-9, press Enter for Web UI]: "

if not defined choice set "choice=1"
if "%choice%"=="1" goto WEBUI
if "%choice%"=="2" goto RECAP_BATCH
if "%choice%"=="3" goto RECAP_SINGLE
if "%choice%"=="4" goto HARDSUB
if "%choice%"=="5" goto SUBTITLE
if "%choice%"=="6" goto CLEAN
if "%choice%"=="7" goto HEALTH
if "%choice%"=="8" goto DOCKER
if "%choice%"=="9" goto UPDATE
if "%choice%"=="0" goto EXIT
goto MENU

:WEBUI
cls
echo ===============================================================================
echo LAUNCHING PAI AI MOVIE STUDIO - WEB UI DASHBOARD...
echo ===============================================================================
echo.
echo [*] Checking Port 5000 availability...
:: Only a previous Studio instance (command line contains web_ui.py) is stopped. Any other program
:: that happens to use port 5000 is left untouched and reported instead of being force-killed.
powershell -NoProfile -Command "$p = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if ($p) { $c = (Get-CimInstance Win32_Process -Filter ('ProcessId=' + $p.OwningProcess)).CommandLine; if ($c -match 'web_ui\.py') { Stop-Process -Id $p.OwningProcess -Force; Write-Host '[*] Stopped the previous Studio instance.' } else { Write-Host '[!] Port 5000 is used by another program (not the Studio). Close it first.' } }"

echo [*] Opening Browser at http://localhost:5000 ...
start /b "" "%PYTHON_EXE%" -c "import socket, time, webbrowser; exec('for _ in range(30):\n time.sleep(1)\n try:\n  with socket.create_connection((\x27127.0.0.1\x27, 5000), timeout=0.5):\n   webbrowser.open(\x27http://localhost:5000\x27)\n   break\n except OSError:\n  pass')" >nul 2>&1

echo.
echo ===============================================================================
echo  Dashboard Link : http://localhost:5000
echo  To Stop Server : Press Ctrl + C in this window
echo ===============================================================================
echo.

"%PYTHON_EXE%" web_ui.py
if errorlevel 1 (
    echo.
    echo [ERROR] Web UI stopped with an error code.
    pause
)
if defined CLI_MODE exit /b 0
goto MENU

:RECAP_BATCH
cls
echo ===============================================================================
echo STARTING BATCH MOVIE RECAP GENERATION FOR "movies/" FOLDER...
echo ===============================================================================
"%PYTHON_EXE%" main.py --batch
if defined CLI_MODE exit /b 0
echo.
echo ===============================================================================
echo Batch Processing Complete! Press any key to return to menu...
pause >nul
goto MENU

:RECAP_SINGLE
cls
echo ===============================================================================
echo ENTER VIDEO URL OR LOCAL FILE PATH:
echo ===============================================================================
echo Tip: Paste a full YouTube link or local file path (e.g. C:\videos\movie.mp4)
set "input_src="
set /p "input_src=Input URL or File Path: "
if not defined input_src goto MENU

echo.
echo Select Narration / Translation Style:
echo    [1] Movie Recap Storyteller (Default)
echo    [2] Chinese Drama Audio Novel (တရုတ်ဒရမ်မာ အသံထွက်ဝတ္ထုဟန်)
echo    [3] Wuxia / Cultivation (သိုင်းကား / ကျင့်ကြံရေး)
echo    [4] Cinematic Persona Dubbing
set "rc_style_choice="
set /p "rc_style_choice=Choice [1-4, Enter for Recap]: "
set "rc_style=recap"
if "%rc_style_choice%"=="2" set "rc_style=drama_novel"
if "%rc_style_choice%"=="3" set "rc_style=wuxia"
if "%rc_style_choice%"=="4" set "rc_style=persona"

:RECAP_DIRECT
cls
if not defined rc_style set "rc_style=recap"
echo ===============================================================================
echo PROCESSING: !input_src! [Style: %rc_style%]
echo ===============================================================================
"%PYTHON_EXE%" main.py "!input_src!" --style "%rc_style%"
if defined CLI_MODE exit /b 0
echo.
echo ===============================================================================
echo Processing Complete! Press any key to return to menu...
pause >nul
goto MENU

:HARDSUB
cls
echo ===============================================================================
echo ORIGINAL AUDIO AND BURMESE HARDSUB STUDIO (ENGINE 3)
echo ===============================================================================
echo Features: 100%% Original Audio Preserved + Subtitle Blur + Anti-Copyright
echo.
set "hs_input="
set /p "hs_input=Enter Video URL or File Path: "
if not defined hs_input goto MENU

:HARDSUB_CONFIG
echo.
echo Select Format:
echo    [1] Both 16:9 Landscape + 9:16 Vertical Reels (Default)
echo    [2] 16:9 YouTube Landscape Only
echo    [3] 9:16 Facebook Reels / TikTok Only
set "hs_fmt_choice="
set /p "hs_fmt_choice=Choice [1-3, Enter for Both]: "
set "hs_fmt=both"
if "%hs_fmt_choice%"=="2" set "hs_fmt=16:9"
if "%hs_fmt_choice%"=="3" set "hs_fmt=9:16"

echo.
echo Select Resolution:
echo    [1] 1080p Full HD (Default)
echo    [2] 720p Fast HD
set "hs_res_choice="
set /p "hs_res_choice=Choice [1-2, Enter for 1080p]: "
set "hs_res=1080p"
if "%hs_res_choice%"=="2" set "hs_res=720p"

echo.
echo Select Subtitle Style Preset:
echo    [1] Cinema Box (Netflix Dark Box - Default)
echo    [2] TikTok Yellow (Vibrant Pop)
echo    [3] Classic White (Deep Shadow)
echo    [4] Cyber Cyan (Modern Blue)
echo    [5] Thriller Crimson (Dark Red Box)
set "hs_style_choice="
set /p "hs_style_choice=Choice [1-5, Enter for Box Black]: "
set "hs_style=box_black"
if "%hs_style_choice%"=="2" set "hs_style=yellow_pop"
if "%hs_style_choice%"=="3" set "hs_style=white_stroke"
if "%hs_style_choice%"=="4" set "hs_style=cyan_cyber"
if "%hs_style_choice%"=="5" set "hs_style=crimson_box"

cls
echo ===============================================================================
echo [*] Starting Original Audio and Burmese Hardsub Studio Engine...
echo ===============================================================================
"%PYTHON_EXE%" hardsub_engine.py "!hs_input!" --format "%hs_fmt%" --res "%hs_res%" --style "%hs_style%" --blur auto --color-grading
if defined CLI_MODE exit /b 0
echo.
echo ===============================================================================
echo Hardsub Studio Complete! Check the outputs/ folder.
echo ===============================================================================
pause
goto MENU

:SUBTITLE
cls
echo ===============================================================================
echo YOUTUBE SUBTITLE AND TRANSCRIPT STUDIO (ENGINE 2)
echo ===============================================================================
echo Features: 100%% Timed Spoken Burmese Subtitles + Deliverable Reports
echo.
set "sub_input="
set /p "sub_input=Enter YouTube URL or Video File: "
if not defined sub_input goto MENU

:SUBTITLE_CONFIG
set "sub_proj_name="
set /p "sub_proj_name=Enter Project Name (Optional, press Enter to auto-name): "

echo.
echo Select Source Language:
echo    [1] Auto-detect (Default)
echo    [2] English (en)
echo    [3] Chinese (zh)
echo    [4] Japanese (ja)
echo    [5] Korean (ko)
echo    [6] Thai (th)
set "sub_lang_choice="
set /p "sub_lang_choice=Choice [1-6, Enter for Auto]: "
set "sub_src_lang=auto"
if "%sub_lang_choice%"=="2" set "sub_src_lang=en"
if "%sub_lang_choice%"=="3" set "sub_src_lang=zh"
if "%sub_lang_choice%"=="4" set "sub_src_lang=ja"
if "%sub_lang_choice%"=="5" set "sub_src_lang=ko"
if "%sub_lang_choice%"=="6" set "sub_src_lang=th"

set "sub_name_arg="
if not "!sub_proj_name!"=="" set sub_name_arg=--name "!sub_proj_name!"

echo.
echo Enable Old Subtitle Blur Removal (Vision AI Boxblur) and Video Rendering?
echo    [1] Yes - Erase Old Subtitles + Burn Burmese Subtitles into Video (Default)
echo    [2] No - Export Subtitle and Transcript Files Only (.srt/.txt)
set "sub_blur_choice="
set /p "sub_blur_choice=Choice [1-2, Enter for Yes]: "
set "sub_render_args=--render-video --blur-mode auto"
if "%sub_blur_choice%"=="2" (
    set "sub_render_args=--no-render"
    goto SUBTITLE_RUN
)

echo.
echo Select Video Format for Subtitled Output:
echo    [1] 16:9 YouTube Landscape (Default)
echo    [2] 9:16 Facebook Reels / TikTok Vertical Canvas
echo    [3] Both Formats (16:9 + 9:16)
set "sub_fmt_choice="
set /p "sub_fmt_choice=Choice [1-3, Enter for 16:9]: "
set "sub_fmt=16:9"
if "%sub_fmt_choice%"=="2" set "sub_fmt=9:16"
if "%sub_fmt_choice%"=="3" set "sub_fmt=both"
set "sub_render_args=!sub_render_args! --format %sub_fmt%"

echo.
echo Select Subtitle Style Preset:
echo    [1] Cinema Box (Netflix Dark Box - Default)
echo    [2] TikTok Yellow (Vibrant Pop)
echo    [3] Classic White (Deep Shadow)
echo    [4] Cyber Cyan (Modern Blue)
echo    [5] Thriller Crimson (Dark Red Box)
set "sub_style_choice="
set /p "sub_style_choice=Choice [1-5, Enter for Box Black]: "
set "sub_style=box_black"
if "%sub_style_choice%"=="2" set "sub_style=yellow_pop"
if "%sub_style_choice%"=="3" set "sub_style=white_stroke"
if "%sub_style_choice%"=="4" set "sub_style=cyan_cyber"
if "%sub_style_choice%"=="5" set "sub_style=crimson_box"
set "sub_render_args=!sub_render_args! --style %sub_style%"

:SUBTITLE_RUN
cls
echo ===============================================================================
echo [*] Starting YouTube Subtitle and Transcript Studio Engine...
echo ===============================================================================
"%PYTHON_EXE%" subtitle_engine.py -i "!sub_input!" !sub_name_arg! --source-lang "%sub_src_lang%" !sub_render_args!
if defined CLI_MODE exit /b 0
echo.
echo ===============================================================================
echo Subtitle Studio Complete! Check the outputs/ folder.
echo ===============================================================================
pause
goto MENU

:CLEAN
cls
echo ===============================================================================
echo STUDIO CLEANUP UTILITY
echo ===============================================================================
"%PYTHON_EXE%" main.py --clean
if defined CLI_MODE exit /b 0
echo.
echo Press any key to return to menu...
pause >nul
goto MENU

:HEALTH
cls
echo ===============================================================================
echo RUNNING SYSTEM HEALTH AND HARDWARE DIAGNOSTICS...
echo ===============================================================================
"%PYTHON_EXE%" -c "import sys, os, brain.config as cfg; from agents.video_merger_agent import detect_hardware_encoder, _get_ffmpeg_bin; ff = _get_ffmpeg_bin(); enc = detect_hardware_encoder(); c = cfg.load_config(); keys = c.get('gemini', {}).get('api_keys', []); model = c.get('gemini', {}).get('model', 'Unknown'); print('  [*] Python Version     :', sys.version.split()[0]); print('  [*] Active FFmpeg      :', ff or 'NOT FOUND'); print('  [*] Video Accelerator  :', enc.get('label', 'Default'), '[' + enc.get('codec', 'libx264') + ']'); print('  [*] Gemini Model       :', model); print('  [*] Configured API Keys:', len(keys), 'key(s)'); print('  [*] Working Directory  :', os.getcwd())"
if defined CLI_MODE exit /b 0
echo.
echo ===============================================================================
echo Press any key to return to menu...
pause >nul
goto MENU

:DOCKER
cls
echo ===============================================================================
echo LAUNCHING PAI AI MOVIE STUDIO IN DOCKER CONTAINER...
echo ===============================================================================
where docker >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker was not detected on this system!
    echo Please install Docker Desktop: https://www.docker.com/products/docker-desktop/
    echo.
    pause
    goto MENU
)
echo [*] Starting Studio containers via Docker Compose...
docker compose up -d
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to start Docker container.
    pause
    goto MENU
)
echo.
echo [*] Docker container is active and running!
echo [*] Opening Browser at http://localhost:5000 ...
start http://localhost:5000
echo.
echo Press any key to return to menu...
pause >nul
goto MENU

:UPDATE
cls
echo ===============================================================================
echo CHECKING AND PULLING LATEST UPDATES FROM GITHUB...
echo ===============================================================================
where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git was not detected on this system!
    echo Please install Git from https://git-scm.com/
    pause
    goto MENU
)
echo [*] Fetching and pulling latest changes from origin/main...
git pull origin main
if errorlevel 1 (
    echo [WARN] git pull returned an error. Please check your network or local changes.
) else (
    echo [OK] Code updated successfully!
)
echo.
echo [*] Checking Python dependencies...
"%PYTHON_EXE%" -m pip install -r requirements.txt --quiet
echo [OK] Dependencies check completed.
echo.
echo ===============================================================================
echo Update completed! Press any key to return to menu...
echo ===============================================================================
pause >nul
goto MENU

:EXIT
exit /b 0
