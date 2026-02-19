@echo off
echo Installing dependencies...
pip install -r requirements.txt
echo.
echo Starting RoyalCrash Backend...
echo WebSocket: ws://localhost:8080/ws
echo API: http://localhost:8080
echo.
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
