@echo off
REM Sobe a Biblioteca localmente em http://localhost:8020 (painel em /admin).
REM Antes, na 1a vez:
REM   docker compose -f ..\docker-compose.dev.yml up -d
REM   copy ..\.env.example .env
REM   pip install -r requirements.txt
cd /d "%~dp0"
python -m uvicorn app.main:app --reload --port 8020
