@echo off
rem Hermes TTS 命令型 provider 的入口：把参数原样转给 bailian_tts.py
rem 优先用 py -3 启动器，找不到时退回 PATH 上的 python。
where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -3 "%~dp0bailian_tts.py" %*
) else (
  python "%~dp0bailian_tts.py" %*
)
