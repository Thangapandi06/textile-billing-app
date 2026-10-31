@echo off
cd /d %~dp0
pip install -r requirements.txt
start "" http://localhost:8080
uvicorn main:app --host 127.0.0.1 --port 8080
