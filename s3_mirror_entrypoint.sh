#!/bin/bash
set -euo pipefail

# Enhanced entrypoint for Frigate with S3 support

echo "[S3 Mirror] Starting Frigate with S3 snapshot mirroring support..."

# Function to check if S3 is configured
is_s3_configured() {
    [[ -n "${S3_BUCKET:-}" && -n "${S3_ACCESS_KEY:-}" && -n "${S3_SECRET_KEY:-}" ]]
}

# Function to install Python packages reliably
install_packages() {
    local max_attempts=3
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        echo "[S3 Mirror] Installing required packages (attempt $attempt/$max_attempts)..."
        
        if python -m pip install --no-cache-dir --upgrade pip && \
           python -m pip install --no-cache-dir boto3>=1.26.0 botocore>=1.29.0; then
            echo "[S3 Mirror] Successfully installed required packages"
            return 0
        else
            echo "[S3 Mirror] Failed to install packages, attempt $attempt"
            attempt=$((attempt + 1))
            sleep 2
        fi
    done
    
    echo "[S3 Mirror] WARNING: Failed to install boto3 after $max_attempts attempts"
    return 1
}

# Function to verify S3 modules
verify_s3_modules() {
    local required_files=("/app/s3.py" "/app/s3_snapshot_mirror.py" "/app/s3_mirror_patcher.py")
    
    for file in "${required_files[@]}"; do
        if [[ ! -f "$file" ]]; then
            echo "[S3 Mirror] ERROR: Required file $file not found"
            return 1
        fi
    done
    
    echo "[S3 Mirror] All required S3 modules found"
    return 0
}

# Main logic
if is_s3_configured; then
    echo "[S3 Mirror] S3 configuration detected:"
    echo "[S3 Mirror]   Bucket: ${S3_BUCKET}"
    echo "[S3 Mirror]   Region: ${S3_REGION:-us-east-1}"
    echo "[S3 Mirror]   Path Prefix: ${S3_PATH_PREFIX:-none}"
    echo "[S3 Mirror]   Upload Threads: ${S3_UPLOAD_THREADS:-8}"
    echo "[S3 Mirror]   Storage Class: ${S3_STORAGE_CLASS:-STANDARD_IA}"
    
    # Verify S3 modules exist
    if ! verify_s3_modules; then
        echo "[S3 Mirror] ERROR: S3 modules missing, running Frigate without S3 support"
        exec python -m frigate "$@"
    fi
    
    # Install boto3 if needed
    if ! python -c "import boto3" 2>/dev/null; then
        if ! install_packages; then
            echo "[S3 Mirror] WARNING: Could not install boto3, S3 mirroring may not work"
            # Continue anyway - the patcher will handle the missing module
        fi
    else
        echo "[S3 Mirror] boto3 already installed"
    fi
    
    # Make patcher executable
    chmod +x /app/frigate_s3_patcher.py
    
    # Run Frigate with S3 patcher
    echo "[S3 Mirror] Starting Frigate with S3 patch..."
    exec python /app/frigate_s3_patcher.py "$@"
else
    echo "[S3 Mirror] S3 not configured, starting standard Frigate..."
    exec python -m frigate "$@"
fi