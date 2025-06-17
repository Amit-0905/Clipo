#!/bin/bash

echo "Starting MongoDB and Redis..."
docker-compose up -d mongodb redis

echo "Waiting for services to start..."
sleep 10

echo "Starting Celery worker..."
celery -A main.celery_app worker --loglevel=info &

echo "Starting FastAPI application..."
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
