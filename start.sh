#!/bin/bash

# Workflow Manager Startup Script

echo "Starting Workflow Manager..."
echo ""

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed"
    exit 1
fi

# Check if Node.js is installed
if ! command -v node &> /dev/null; then
    echo "Error: Node.js is not installed"
    exit 1
fi

# Start backend
echo "Starting backend server..."
cd backend

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install backend dependencies
echo "Installing backend dependencies..."
pip install -q -r requirements.txt

# Start backend in background
echo "Backend starting on http://localhost:5000"
python app.py &
BACKEND_PID=$!

cd ..

# Start frontend
echo ""
echo "Starting frontend server..."
cd frontend

# Install frontend dependencies if node_modules doesn't exist
if [ ! -d "node_modules" ]; then
    echo "Installing frontend dependencies (this may take a few minutes)..."
    npm install
fi

# Start frontend
echo "Frontend starting on http://localhost:3000"
npm start &
FRONTEND_PID=$!

cd ..

echo ""
echo "=========================================="
echo "Workflow Manager is running!"
echo "Backend:  http://localhost:5000"
echo "Frontend: http://localhost:3000"
echo "=========================================="
echo ""
echo "Press Ctrl+C to stop all servers"

# Wait for Ctrl+C
trap "echo 'Stopping servers...'; kill $BACKEND_PID $FRONTEND_PID; exit" INT
wait

# Made with Bob
