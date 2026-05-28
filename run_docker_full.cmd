@echo off
cd /d "%~dp0"
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build
