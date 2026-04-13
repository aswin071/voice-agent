#!/bin/bash
# Docker entrypoint script for combined mode (API + Worker)
set -e

# Function to handle shutdown gracefully
cleanup() {
    echo "Received shutdown signal, stopping services..."
    if [ -n "$WORKER_PID" ]; then
        kill $WORKER_PID 2>/dev/null || true
    fi
    if [ -n "$API_PID" ]; then
        kill $API_PID 2>/dev/null || true
    fi
    wait
    exit 0
}

trap cleanup SIGTERM SIGINT

# Start the voice agent worker in background
echo "Starting LiveKit voice agent worker..."
python simple_agent.py dev &
WORKER_PID=$!

# Wait a moment for worker to initialize
sleep 3

# Start the FastAPI server in background
echo "Starting FastAPI server..."
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2 &
API_PID=$!

# Log process IDs
echo "Voice agent worker PID: $WORKER_PID"
echo "FastAPI server PID: $API_PID"

# Wait for both processes
wait
