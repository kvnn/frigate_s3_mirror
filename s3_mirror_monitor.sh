#!/bin/bash
# S3 Mirror Health Monitoring Script

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if Frigate container is running
check_container() {
    if docker ps --format '{{.Names}}' | grep -q "^frigate$"; then
        echo -e "${GREEN}✓${NC} Frigate container is running"
        return 0
    else
        echo -e "${RED}✗${NC} Frigate container is not running"
        return 1
    fi
}

# Check S3 mirror status in logs
check_s3_mirror() {
    local status=$(docker logs frigate 2>&1 | grep -E "S3 snapshot mirror" | tail -5)
    
    if echo "$status" | grep -q "started successfully"; then
        echo -e "${GREEN}✓${NC} S3 mirror started successfully"
        
        # Check for recent uploads
        local recent_uploads=$(docker logs frigate --since 5m 2>&1 | grep -c "Queued .* new snapshots for upload")
        echo -e "  Recent upload batches (last 5 min): $recent_uploads"
        
        return 0
    else
        echo -e "${RED}✗${NC} S3 mirror may not be running"
        return 1
    fi
}

# Check S3 upload statistics
check_upload_stats() {
    local stats=$(docker logs frigate --since 1h 2>&1 | grep "S3 Upload Stats" | tail -1)
    
    if [[ -n "$stats" ]]; then
        echo -e "${GREEN}Upload Statistics:${NC}"
        echo "$stats" | sed 's/.*S3 Upload Stats - /  /'
    else
        echo -e "${YELLOW}⚠${NC} No recent upload statistics found"
    fi
}

# Check for errors
check_errors() {
    local errors=$(docker logs frigate --since 1h 2>&1 | grep -E "(ERROR|Failed to upload)" | tail -10)
    
    if [[ -n "$errors" ]]; then
        echo -e "${RED}Recent Errors:${NC}"
        echo "$errors" | sed 's/^/  /'
    else
        echo -e "${GREEN}✓${NC} No recent errors found"
    fi
}

# Check S3 state files
check_state_files() {
    local uploaded_count=$(docker exec frigate sh -c 'wc -l < /media/frigate/clips/cache/.s3_uploaded_events 2>/dev/null || echo 0')
    local failed_count=$(docker exec frigate sh -c 'test -f /media/frigate/clips/cache/.s3_failed_events && jq length /media/frigate/clips/cache/.s3_failed_events || echo 0' 2>/dev/null)
    
    echo -e "${GREEN}State Information:${NC}"
    echo "  Uploaded events tracked: $uploaded_count"
    echo "  Failed events tracked: $failed_count"
}

# Check S3 connectivity
check_s3_connection() {
    local bucket=$(docker exec frigate printenv S3_BUCKET 2>/dev/null)
    
    if [[ -n "$bucket" ]]; then
        echo -e "${GREEN}S3 Configuration:${NC}"
        echo "  Bucket: $bucket"
        
        # Check if we can list the bucket (requires aws cli in container)
        if docker exec frigate which aws >/dev/null 2>&1; then
            if docker exec frigate aws s3 ls "s3://$bucket" --max-items 1 >/dev/null 2>&1; then
                echo -e "  ${GREEN}✓${NC} S3 connection verified"
            else
                echo -e "  ${RED}✗${NC} Cannot connect to S3 bucket"
            fi
        fi
    else
        echo -e "${RED}✗${NC} S3 not configured"
    fi
}

# Main monitoring function
main() {
    echo "=== Frigate S3 Mirror Health Check ==="
    echo "Time: $(date)"
    echo
    
    # Run all checks
    check_container || exit 1
    echo
    
    check_s3_mirror
    echo
    
    check_upload_stats
    echo
    
    check_state_files
    echo
    
    check_s3_connection
    echo
    
    check_errors
    echo
    
    echo "=== End Health Check ==="
}

# Run with watch if requested
if [[ "$1" == "watch" ]]; then
    watch -n 30 "$0"
else
    main
fi