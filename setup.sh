#!/usr/bin/env bash
# NWS Dashboard - Quick start script for Windows/Mac/Linux

set -e

PROJECT_DIR="$HOME/nws_dashboard"
LOG_DIR="/var/log/nws_dashboard"
DATA_DIR="$HOME/.nws_dashboard"

echo "=== NWS Dashboard Setup ==="
echo ""

# Create directories
mkdir -p "$PROJECT_DIR"
mkdir -p "$LOG_DIR" 2>/dev/null || echo "Note: May need sudo for $LOG_DIR"
mkdir -p "$DATA_DIR"

cd "$PROJECT_DIR"

# Install Python dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Initialize cache directory
touch "$DATA_DIR/latest_afd.txt"

echo ""
echo "Dashboard ready!"
echo ""
echo "To start the server:"
echo "  cd $PROJECT_DIR"
echo "  python dashboard.py"
echo ""
echo "Then open: http://localhost:5000"
echo ""
echo "Logs are at: $LOG_DIR/fetcher.log (or ~/nws_dashboard/fetcher.log if /var/log fails)"
