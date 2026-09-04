@echo off
REM start_public.bat - run the game server GLOBALLY via Cloudflare Tunnel.
REM You get a public https URL, no Radmin and no port forwarding needed.
REM Requires: python and cloudflared.
REM cloudflared download page:
REM https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

set TRUST_PROXY=1

where cloudflared >nul 2>nul
if errorlevel 1 (
  echo [ERROR] cloudflared not found in PATH.
  echo Download cloudflared for Windows from the Cloudflare site.
  echo Put cloudflared.exe next to this file or add it to PATH, then run again.
  pause
  exit /b 1
)

echo Starting server.py on port 8787...
start "Pinterest Chase server" python server.py
timeout /t 3 >nul

echo Opening public tunnel. Copy the https URL and paste it as REMOTE_BACKEND_URL in index.html, then push to GitHub.
echo Keep this window open while playing.
cloudflared tunnel --url http://localhost:8787
