import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectionError, EndpointConnectionError

logger = logging.getLogger(__name__)


class S3Api:
    def __init__(self) -> None:
        self.bucket_name = None
        self.region = "us-east-1"
        self.access_key = None
        self.secret_key = None
        self.endpoint_url = None
        self.path_prefix = ""
        self._initialized = False
        self._last_health_check = 0
        self._health_check_interval = 300  # 5 minutes
        self._is_healthy = True
        self._upload_stats = {"success": 0, "failed": 0}
        self._retry_queue = []
        self._executor = None
        
        # Load S3 configuration from environment or secrets
        if "S3_BUCKET" in os.environ:
            self.bucket_name = os.environ.get("S3_BUCKET")
        
        if "S3_REGION" in os.environ:
            self.region = os.environ.get("S3_REGION")
            
        if "S3_ACCESS_KEY" in os.environ:
            self.access_key = os.environ.get("S3_ACCESS_KEY")
            
        if "S3_SECRET_KEY" in os.environ:
            self.secret_key = os.environ.get("S3_SECRET_KEY")
            
        if "S3_ENDPOINT_URL" in os.environ:
            self.endpoint_url = os.environ.get("S3_ENDPOINT_URL")
            
        if "S3_PATH_PREFIX" in os.environ:
            self.path_prefix = os.environ.get("S3_PATH_PREFIX", "").strip("/")
            
        # Check for secrets directory (for Docker secrets)
        elif os.path.isdir("/run/secrets") and os.access("/run/secrets", os.R_OK):
            secrets_dir = Path("/run/secrets")
            
            if (secrets_dir / "s3_bucket").exists():
                self.bucket_name = (secrets_dir / "s3_bucket").read_text().strip()
                
            if (secrets_dir / "s3_access_key").exists():
                self.access_key = (secrets_dir / "s3_access_key").read_text().strip()
                
            if (secrets_dir / "s3_secret_key").exists():
                self.secret_key = (secrets_dir / "s3_secret_key").read_text().strip()
                
        # Check for add-on options file
        elif os.path.isfile("/data/options.json"):
            with open("/data/options.json") as f:
                options = json.loads(f.read())
            self.bucket_name = options.get("s3_bucket")
            self.access_key = options.get("s3_access_key")
            self.secret_key = options.get("s3_secret_key")
            self.region = options.get("s3_region", self.region)
            self.endpoint_url = options.get("s3_endpoint_url")
            self.path_prefix = options.get("s3_path_prefix", "").strip("/")
            
        self._is_active = bool(
            self.bucket_name and 
            self.access_key and 
            self.secret_key
        )
        
        if self._is_active:
            self._initialize_client()
            
    def _initialize_client(self) -> None:
        """Initialize S3 client with robust configuration."""
        try:
            # Aggressive retry configuration for maximum reliability
            config = Config(
                region_name=self.region,
                retries={
                    'max_attempts': 10,
                    'mode': 'adaptive',
                    'adaptive_retry_wait_time': 2.0
                },
                max_pool_connections=100,
                connect_timeout=10,
                read_timeout=30,
                tcp_keepalive=True
            )
            
            self._s3_client = boto3.client(
                's3',
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                config=config,
                endpoint_url=self.endpoint_url,
                use_ssl=True if not self.endpoint_url or self.endpoint_url.startswith('https') else False
            )
            
            # Thread-local storage for S3 clients
            self._local = threading.local()
            
            # Test connection
            self._s3_client.head_bucket(Bucket=self.bucket_name)
            logger.info(f"S3 connection established to bucket: {self.bucket_name}")
            self._initialized = True
            self._is_healthy = True
            
            # Initialize thread pool for parallel uploads
            max_workers = int(os.environ.get("S3_UPLOAD_THREADS", "8"))
            self._executor = ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="s3_upload"
            )
            
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == '404':
                logger.error(f"S3 bucket '{self.bucket_name}' does not exist")
            else:
                logger.error(f"Failed to connect to S3: {e}")
            self._is_active = False
            self._is_healthy = False
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            self._is_active = False
            self._is_healthy = False
    
    def _get_client(self):
        """Get thread-local S3 client with connection reuse."""
        if not hasattr(self._local, 's3_client') or not self._is_healthy:
            config = Config(
                region_name=self.region,
                retries={
                    'max_attempts': 10,
                    'mode': 'adaptive',
                    'adaptive_retry_wait_time': 2.0
                },
                max_pool_connections=100,
                connect_timeout=10,
                read_timeout=30,
                tcp_keepalive=True
            )
            
            self._local.s3_client = boto3.client(
                's3',
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                config=config,
                endpoint_url=self.endpoint_url,
                use_ssl=True if not self.endpoint_url or self.endpoint_url.startswith('https') else False
            )
        
        return self._local.s3_client
    
    def _health_check(self) -> bool:
        """Periodic health check of S3 connection."""
        current_time = time.time()
        if current_time - self._last_health_check < self._health_check_interval:
            return self._is_healthy
            
        try:
            self._get_client().head_bucket(Bucket=self.bucket_name)
            self._is_healthy = True
            self._last_health_check = current_time
            
            # Process retry queue if healthy
            if self._retry_queue:
                self._process_retry_queue()
                
        except Exception as e:
            logger.warning(f"S3 health check failed: {e}")
            self._is_healthy = False
            
        return self._is_healthy
    
    def _process_retry_queue(self) -> None:
        """Process failed uploads from retry queue."""
        retry_count = len(self._retry_queue)
        if retry_count > 0:
            logger.info(f"Processing {retry_count} uploads from retry queue")
            
        while self._retry_queue and self._is_healthy:
            item = self._retry_queue.pop(0)
            self._executor.submit(self._upload_with_retry, item)
    
    def _upload_with_retry(self, upload_data: Dict[str, Any]) -> bool:
        """Upload with exponential backoff retry."""
        max_retries = 5
        base_delay = 1
        
        for attempt in range(max_retries):
            try:
                if "file_path" in upload_data:
                    with open(upload_data["file_path"], 'rb') as f:
                        self._get_client().upload_fileobj(
                            f,
                            self.bucket_name,
                            upload_data["s3_key"],
                            ExtraArgs=upload_data["extra_args"]
                        )
                else:
                    self._get_client().put_object(
                        Bucket=self.bucket_name,
                        Key=upload_data["s3_key"],
                        Body=upload_data["body"],
                        **upload_data["extra_args"]
                    )
                
                self._upload_stats["success"] += 1
                return True
                
            except (ConnectionError, EndpointConnectionError) as e:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Connection error on attempt {attempt + 1}, retrying in {delay}s: {e}")
                time.sleep(delay)
                
            except ClientError as e:
                error_code = e.response['Error']['Code']
                if error_code in ['RequestTimeout', 'ServiceUnavailable', 'SlowDown']:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"S3 error {error_code} on attempt {attempt + 1}, retrying in {delay}s")
                    time.sleep(delay)
                else:
                    logger.error(f"Non-retryable S3 error: {e}")
                    self._upload_stats["failed"] += 1
                    return False
                    
            except Exception as e:
                logger.error(f"Unexpected error during upload: {e}")
                self._upload_stats["failed"] += 1
                return False
        
        # Max retries exceeded, add to retry queue
        self._retry_queue.append(upload_data)
        self._upload_stats["failed"] += 1
        return False
    
    def is_active(self) -> bool:
        """Check if S3 is configured and healthy."""
        if not self._is_active:
            return False
        
        # Periodic health check
        if self._initialized:
            self._health_check()
            
        return self._is_active and self._is_healthy
    
    def get_stats(self) -> Dict[str, Any]:
        """Get upload statistics."""
        return {
            "success": self._upload_stats["success"],
            "failed": self._upload_stats["failed"],
            "retry_queue_size": len(self._retry_queue),
            "is_healthy": self._is_healthy
        }
    
    def upload_snapshot(
        self, 
        file_path: str, 
        event_id: str, 
        timestamp: float,
        camera: str,
        label: str,
        file_extension: str = "jpg",
        async_upload: bool = True
    ) -> bool:
        """Upload a snapshot to S3 with metadata."""
        if not self.is_active():
            return False
            
        # Format timestamp
        dt = datetime.fromtimestamp(timestamp)
        formatted_time = dt.strftime("%Y%m%d_%H%M%S")
        
        # Construct S3 key
        key_parts = []
        if self.path_prefix:
            key_parts.append(self.path_prefix)
        key_parts.extend([
            camera,
            dt.strftime("%Y-%m-%d"),
            f"{event_id}-{formatted_time}.{file_extension}"
        ])
        s3_key = "/".join(key_parts)
        
        # Prepare upload data
        upload_data = {
            "file_path": file_path,
            "s3_key": s3_key,
            "extra_args": {
                'Metadata': {
                    'event_id': event_id,
                    'camera': camera,
                    'label': label,
                    'timestamp': str(timestamp),
                    'upload_time': str(datetime.now().timestamp())
                },
                'ContentType': f'image/{file_extension}',
                'StorageClass': os.environ.get('S3_STORAGE_CLASS', 'STANDARD_IA')
            }
        }
        
        if async_upload and self._executor:
            # Async upload
            future = self._executor.submit(self._upload_with_retry, upload_data)
            return True  # Return immediately, upload happens in background
        else:
            # Sync upload
            return self._upload_with_retry(upload_data)
    
    def upload_snapshot_bytes(
        self,
        image_bytes: bytes,
        event_id: str,
        timestamp: float,
        camera: str,
        label: str,
        file_extension: str = "jpg",
        async_upload: bool = True
    ) -> bool:
        """Upload snapshot bytes directly to S3."""
        if not self.is_active():
            return False
            
        # Format timestamp
        dt = datetime.fromtimestamp(timestamp)
        formatted_time = dt.strftime("%Y%m%d_%H%M%S")
        
        # Construct S3 key
        key_parts = []
        if self.path_prefix:
            key_parts.append(self.path_prefix)
        key_parts.extend([
            camera,
            dt.strftime("%Y-%m-%d"),
            f"{event_id}-{formatted_time}.{file_extension}"
        ])
        s3_key = "/".join(key_parts)
        
        # Prepare upload data
        upload_data = {
            "body": image_bytes,
            "s3_key": s3_key,
            "extra_args": {
                'Metadata': {
                    'event_id': event_id,
                    'camera': camera,
                    'label': label,
                    'timestamp': str(timestamp),
                    'upload_time': str(datetime.now().timestamp())
                },
                'ContentType': f'image/{file_extension}',
                'StorageClass': os.environ.get('S3_STORAGE_CLASS', 'STANDARD_IA')
            }
        }
        
        if async_upload and self._executor:
            # Async upload
            future = self._executor.submit(self._upload_with_retry, upload_data)
            return True  # Return immediately, upload happens in background
        else:
            # Sync upload
            return self._upload_with_retry(upload_data)
    
    def shutdown(self) -> None:
        """Gracefully shutdown S3 client."""
        if self._executor:
            self._executor.shutdown(wait=True, cancel_futures=False)
            
        # Save retry queue to disk for next startup
        if self._retry_queue:
            retry_file = Path("/tmp/.s3_retry_queue.json")
            try:
                with open(retry_file, 'w') as f:
                    json.dump(self._retry_queue, f)
                logger.info(f"Saved {len(self._retry_queue)} uploads to retry queue")
            except Exception as e:
                logger.error(f"Failed to save retry queue: {e}")