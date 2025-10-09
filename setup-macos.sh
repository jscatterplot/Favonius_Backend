#!/bin/bash

# TimescaleDB setup script for macOS
# Based on TigerDB recommendations

echo "Setting up TimescaleDB for macOS..."

# Check if Homebrew is installed
if ! command -v brew &> /dev/null; then
    echo "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# Install TimescaleDB via Homebrew
echo "Installing TimescaleDB..."
brew install timescaledb

# Start TimescaleDB service
echo "Starting TimescaleDB service..."
brew services start timescaledb

# Install Python dependencies
echo "Installing Python dependencies..."
pip install -r requirements.txt

echo "Setup complete!"
echo "TimescaleDB is now running and ready to use with psycopg2-binary"