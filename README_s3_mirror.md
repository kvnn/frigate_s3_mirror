# Frigate S3 Snapshot Mirror - Runtime Patch

This implementation provides maximum reliability for S3 snapshot mirroring by patching Frigate at runtime.

## Features

- **Maximum Reliability**: Aggressive retry logic, connection pooling, and graceful degradation
- **Async Uploads**: Non-blocking parallel uploads with configurable thread pool
- **State Persistence**: Survives container restarts without re-uploading
- **Health Monitoring**: Built-in health checks and automatic recovery
- **Error Recovery**: Failed uploads are retried with exponential backoff
- **Resource Efficient**: Minimal overhead with batch processing
- **S3 Compatible**: Works with AWS S3, MinIO, Wasabi, and other S3-compatible services

## Quick Start

1. **Download all files** to your Frigate directory:
   - `s3.py` - S3 API wrapper with reliability features
   - `s3_snapshot_mirror.py` - Main mirror service
   - `frigate_s3_patcher.py` - Runtime patcher
   - `entrypoint.sh` - Custom entrypoint
   - `s3_monitor.sh` - Health monitoring script
   - `setup_s3_mirror.sh` - Setup helper

2. **Run the setup script**:
   ```bash
   chmod +x setup_s3_mirror.sh
   ./setup_s3_mirror.sh
   ```

3. **Configure S3** in your `docker-compose.yml`:
   ```yaml
   environment:
     - S3_BUCKET=your-bucket-name
     - S3_ACCESS_KEY=your-access-key-id
     - S3_SECRET_KEY=your-secret-access-key
     - S3_REGION=us-west-2  # Optional
   ```

4. **Start Frigate**:
   ```bash
   docker-compose down
   docker-compose up -d
   ```

5. **Monitor uploads**:
   ```bash
   ./s3_monitor.sh        # One-time health check
   ./s3_monitor.sh watch  # Continuous monitoring
   ```

## Configuration

### Required Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `S3_BUCKET` | S3 bucket name | `my-frigate-snapshots` |
| `S3_ACCESS_KEY` | AWS Access Key ID | `AKIAIOSFODNN7EXAMPLE` |
| `S3_SECRET_KEY` | AWS Secret Access Key | `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY` |

### Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `S3_REGION` | `us-east-1` | AWS region |
| `S3_PATH_PREFIX` | (none) | S3 key prefix for organization |
| `S3_ENDPOINT_URL` | (none) | Custom endpoint for S3-compatible services |
| `S3_UPLOAD_THREADS` | `8` | Number of parallel upload threads |
| `S3_BATCH_SIZE` | `50` | Events to process per batch |
| `S3_STORAGE_CLASS` | `STANDARD_IA` | S3 storage class |
| `S3_MAX_FAILED_RETRIES` | `3` | Max retries before marking upload as failed |

## S3 Path Structure

Snapshots are organized with this structure:
```
{prefix}/{camera}/{date}/{event-id}-{timestamp}.jpg

Example:
frigate/snapshots/front_door/2024-01-15/abc123-20240115_143022.jpg
```

## Reliability Features

### Connection Management
- **Connection Pooling**: Reuses connections for efficiency
- **TCP Keep-Alive**: Prevents connection timeout on long uploads
- **Adaptive Retries**: Automatic retry with exponential backoff
- **Health Checks**: Periodic S3 connectivity verification

### Error Recovery
- **Retry Queue**: Failed uploads are queued for retry
- **Persistent State**: Upload progress survives restarts
- **Graceful Degradation**: Continues operating even if S3 is temporarily unavailable
- **Database Reconnection**: Automatically reconnects if Frigate DB connection is lost

### Performance Optimization
- **Batch Processing**: Processes multiple events efficiently
- **Async Uploads**: Non-blocking parallel uploads
- **Memory Management**: Old events are cleaned up automatically
- **Thread Pool**: Configurable number of upload workers

## Monitoring

### View Upload Statistics
```bash
docker logs frigate | grep "S3 Upload Stats"
```

Example output:
```
S3 Upload Stats - Success: 1523, Failed: 2, Retry Queue: 0, Tracked Events: 1523, Failed Events: 2, Health: OK
```

### Check S3 Mirror Status
```bash
docker logs frigate | grep "S3 snapshot mirror"
```

### Monitor Specific Camera
```bash
docker logs frigate | grep "S3" | grep "front_door"
```

### View Failed Uploads
```bash
docker exec frigate cat /media/frigate/clips/cache/.s3_failed_events
```

## Troubleshooting

### S3 Mirror Not Starting

1. Check S3 credentials:
   ```bash
   docker exec frigate printenv | grep S3_
   ```

2. Check for import errors:
   ```bash
   docker logs frigate | grep "Failed to import S3 modules"
   ```

3. Verify boto3 installation:
   ```bash
   docker exec frigate python -c "import boto3; print('boto3 OK')"
   ```

### Uploads Failing

1. Check S3 connectivity:
   ```bash
   docker exec frigate python -c "
   import boto3, os
   s3 = boto3.client('s3',
       aws_access_key_id=os.environ['S3_ACCESS_KEY'],
       aws_secret_access_key=os.environ['S3_SECRET_KEY'])
   s3.head_bucket(Bucket=os.environ['S3_BUCKET'])
   print('S3 connection OK')
   "
   ```

2. Check IAM permissions (minimum required):
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": [
           "s3:PutObject",
           "s3:PutObjectAcl"
         ],
         "Resource": "arn:aws:s3:::your-bucket/*"
       },
       {
         "Effect": "Allow",
         "Action": "s3:ListBucket",
         "Resource": "arn:aws:s3:::your-bucket"
       }
     ]
   }
   ```

### High Memory Usage

Adjust batch size and cleanup interval:
```yaml
environment:
  - S3_BATCH_SIZE=25  # Reduce batch size
```

### Database Connection Errors

The mirror automatically reconnects to the database. Check logs for:
```bash
docker logs frigate | grep "Successfully reconnected to database"
```

## Advanced Usage

### Using with S3-Compatible Services

For MinIO:
```yaml
environment:
  - S3_ENDPOINT_URL=https://minio.example.com:9000
  - S3_BUCKET=frigate-snapshots
  - S3_ACCESS_KEY=minioadmin
  - S3_SECRET_KEY=minioadmin
```

For Wasabi:
```yaml
environment:
  - S3_ENDPOINT_URL=https://s3.wasabisys.com
  - S3_REGION=us-east-1
  - S3_BUCKET=frigate-snapshots
```

### Lifecycle Policies

To automatically delete old snapshots, configure S3 lifecycle rules:

```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket your-bucket \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "Delete old snapshots",
      "Status": "Enabled",
      "Prefix": "frigate/snapshots/",
      "Expiration": {
        "Days": 30
      }
    }]
  }'
```

### Backup State Files

State files are stored in:
- `/media/frigate/clips/cache/.s3_uploaded_events` - Successfully uploaded events
- `/media/frigate/clips/cache/.s3_failed_events` - Failed upload tracking

Back these up to preserve upload state across major updates.

## Performance Tuning

For high-volume installations:

```yaml
environment:
  - S3_UPLOAD_THREADS=16        # Increase parallel uploads
  - S3_BATCH_SIZE=100           # Larger batches
  - S3_STORAGE_CLASS=STANDARD   # Faster storage class
```

For low-bandwidth connections:

```yaml
environment:
  - S3_UPLOAD_THREADS=2         # Reduce parallel uploads
  - S3_BATCH_SIZE=10            # Smaller batches
```

## Notes

- The runtime patch approach modifies Frigate's behavior at startup
- All S3 operations are designed to fail gracefully without affecting Frigate
- Uploads happen asynchronously to avoid blocking Frigate's main operations
- The system automatically handles Frigate restarts and recovers state
- Failed uploads are retried automatically with exponential backoff
- Old events are cleaned up automatically after 7 days