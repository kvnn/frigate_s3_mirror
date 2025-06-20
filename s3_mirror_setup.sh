#!/bin/bash
# Quick setup script for Frigate S3 Mirror

set -e

echo "=== Frigate S3 Mirror Setup ==="

# Check if required files exist
REQUIRED_FILES=(
    "frigate/s3.py"
    "frigate/s3_snapshot_mirror.py"
    "s3_mirror_patcher.py"
    "s3_mirror_entrypoint.sh"
)

echo "Checking required files..."
for file in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "$file" ]]; then
        echo "ERROR: Missing required file: $file"
        echo "Please ensure all S3 mirror files are in the current directory"
        exit 1
    fi
done

# Make entrypoint executable
chmod +x s3_mirror_entrypoint.sh
chmod +x s3_mirror_monitor.sh 2>/dev/null || true

echo "✓ All required files found"

# Check docker-compose.yml
if [[ ! -f "docker-compose.yml" ]]; then
    echo "ERROR: docker-compose.yml not found"
    exit 1
fi

# Check if S3 configuration is set
if ! grep -q "S3_BUCKET=" docker-compose.yml; then
    echo ""
    echo "WARNING: S3 configuration not found in docker-compose.yml"
    echo "Please update the following environment variables:"
    echo "  - S3_BUCKET=your-bucket-name"
    echo "  - S3_ACCESS_KEY=your-access-key"
    echo "  - S3_SECRET_KEY=your-secret-key"
    echo ""
    read -p "Have you configured S3 settings? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Please configure S3 settings in docker-compose.yml first"
        exit 1
    fi
fi

# Create necessary directories
echo "Creating cache directory..."
mkdir -p ./config

# Backup existing Frigate data if running
if docker ps --format '{{.Names}}' | grep -q "^frigate$"; then
    echo "Backing up existing S3 state files..."
    docker exec frigate cp /media/frigate/clips/cache/.s3_uploaded_events /config/.s3_uploaded_events.backup 2>/dev/null || true
    docker exec frigate cp /media/frigate/clips/cache/.s3_failed_events /config/.s3_failed_events.backup 2>/dev/null || true
fi

echo ""
echo "Setup complete! To start Frigate with S3 mirroring:"
echo "  docker-compose down"
echo "  docker-compose up -d"
echo ""
echo "To monitor S3 uploads:"
echo "  ./s3_monitor.sh        # One-time check"
echo "  ./s3_monitor.sh watch  # Continuous monitoring"
echo ""
echo "To view logs:"
echo "  docker logs frigate | grep S3"
echo ""