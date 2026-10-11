@echo off
rem Atalho para o sem-docker.ps1 (roda sem Docker: Python + PostgreSQL instalados no Windows).
rem Duplo clique: liga o sistema.  No terminal: sem-docker.cmd usuario | parar | zerar
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sem-docker.ps1" %*
if "%~1"=="" pause
