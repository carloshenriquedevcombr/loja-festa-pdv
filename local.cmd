@echo off
rem Atalho para o local.ps1 que funciona mesmo com scripts bloqueados no Windows.
rem Duplo clique: sobe o sistema.  No terminal: local.cmd celular | testes | usuario | logs | parar | zerar
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0local.ps1" %*
if "%~1"=="" pause
