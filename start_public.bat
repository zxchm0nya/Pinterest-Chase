@echo off
chcp 65001 >nul
REM start_public.bat — запуск сервера как ГЛОБАЛЬНОГО через Cloudflare Tunnel.
REM Даёт публичный https-адрес (вида https://xxx.trycloudflare.com) без
REM Radmin/проброса портов + DDoS-защита и CDN от Cloudflare.
REM Требует: python и cloudflared (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/).

set TRUST_PROXY=1

where cloudflared >nul 2>nul
if errorlevel 1 (
  echo [ОШИБКА] cloudflared не найден в PATH.
  echo Скачай cloudflared для Windows с официального сайта Cloudflare:
  echo https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
  echo Положи cloudflared.exe рядом или добавь в PATH, потом запусти снова.
  pause
  exit /b 1
)

echo Запускаю server.py (локально на 8787)...
start "Pinterest Chase server" python server.py
timeout /t 3 >nul

echo Открываю публичный туннель. Скопируй URL вида https://xxx.trycloudflare.com
echo и вставь его в index.html в REMOTE_BACKEND_URL, затем запушь на GitHub.
echo Не закрывай это окно, пока идёт игра.
cloudflared tunnel --url http://localhost:8787
