@echo off
set PYTHONPATH=src
uvicorn app.main:app --reload
