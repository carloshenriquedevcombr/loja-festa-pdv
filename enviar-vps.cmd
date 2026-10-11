@echo off
rem Atalho para o enviar-vps.ps1 (envia e instala o sistema na VPS pelo SSH, sem GitHub).
rem Duplo clique: envia.  No terminal: enviar-vps.cmd testar | status | logs | destino
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0enviar-vps.ps1" %*
if "%~1"=="" pause
