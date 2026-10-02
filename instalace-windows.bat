@echo off
rem ClipFarm - instalace pro Windows: Python, yt-dlp, ffmpeg a Node.js (YouTube ho potrebuje).
rem Staci jednou dvakrat kliknout. Bez diakritiky schvalne: stary cmd ji neumi.
echo Instaluji Python, yt-dlp, ffmpeg a Node.js pres winget...
echo.
for %%p in (Python.Python.3.13 yt-dlp.yt-dlp Gyan.FFmpeg OpenJS.NodeJS.LTS) do (
    winget install -e --id %%p --accept-package-agreements --accept-source-agreements
)
echo.
echo Hotovo. Ted dvakrat klikni na ClipFarm.py (ve stejne slozce jako tenhle soubor).
echo Poprve vytvori zastupce ClipFarm na plose a v nabidce Start, priste staci ten.
pause
