import json
import logging
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional, Set, Dict, Any

from peewee import DoesNotExist, OperationalError, InterfaceError

from frigate.config import FrigateConfig
from frigate.const import CLIPS_DIR
from frigate.models import Event
from frigate.s3 import S3Api

logger = logging.getLogger(__name__)


class S3SnapshotMirror(threading.Thread):
    """Mirror Frigate snapshots to S3 storage with maximum reliability."""
    
    def __init__(
        self, 
        config: FrigateConfig, 
        stop_event: threading.Event
    ) -> None:
        super().__init__(name="s3_snapshot_mirror")
        self.config = config
        self.stop_event = stop_event
        self.s3_api = S3Api()
        self.uploaded_events: Set[str] = set()
        self.failed_events: Dict[str, int] = {}  # event_id: retry_count
        self.last_check_time = 0
        self.check_interval = 5  # Check for new snapshots every 5 seconds
        self.state_file = Path(f"{CLIPS_DIR}/cache/.s3_uploaded_events")
        self.failed_file = Path(f"{CLIPS_DIR}/cache/.s3_failed_events")
        self._db_reconnect_attempts = 0
        self._max_db_reconnect_attempts = 10
        self._last_stats_log = 0
        self._stats_log_interval = 300  # Log stats every 5 minutes
        self._event_cache = deque(maxlen=1000)  # Cache recent events
        self._running = True
        
        # Performance optimization
        self.batch_size = int(os.environ.get("S3_BATCH_SIZE", "50"))
        self.max_failed_retries = int(os.environ.get("S3_MAX_FAILED_RETRIES", "3"))
        
        # Load state from previous run
        self._load_state()
        
        # Load retry queue from S3Api if it exists
        retry_file = Path("/tmp/.s3_retry_queue.json")
        if retry_file.exists():
            try:
                with open(retry_file, 'r') as f:
                    retry_queue = json.load(f)
                logger.info(f"Loaded {len(retry_queue)} items from S3 retry queue")
                retry_file.unlink()
            except Exception as e:
                logger.error(f"Failed to load S3 retry queue: {e}")
    
    def _load_state(self) -> None:
        """Load previously uploaded and failed event IDs from state files."""
        # Load uploaded events
        try:
            if self.state_file.exists():
                with open(self.state_file, 'r') as f:
                    for line in f:
                        event_id = line.strip()
                        if event_id:
                            self.uploaded_events.add(event_id)
                logger.info(f"Loaded {len(self.uploaded_events)} previously uploaded events")
        except Exception as e:
            logger.error(f"Failed to load S3 upload state: {e}")
        
        # Load failed events
        try:
            if self.failed_file.exists():
                with open(self.failed_file, 'r') as f:
                    self.failed_events = json.load(f)
                logger.info(f"Loaded {len(self.failed_events)} failed events for retry")
        except Exception as e:
            logger.error(f"Failed to load failed events state: {e}")
    
    def _save_state(self) -> None:
        """Save uploaded and failed event IDs to state files."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Save uploaded events
            with open(self.state_file, 'w') as f:
                for event_id in sorted(self.uploaded_events):
                    f.write(f"{event_id}\n")
            
            # Save failed events
            if self.failed_events:
                with open(self.failed_file, 'w') as f:
                    json.dump(self.failed_events, f)
            elif self.failed_file.exists():
                self.failed_file.unlink()
                
        except Exception as e:
            logger.error(f"Failed to save S3 upload state: {e}")
    
    def _ensure_db_connection(self) -> bool:
        """Ensure database connection is alive, reconnect if needed."""
        try:
            # Simple query to test connection
            Event.select().limit(1).execute()
            self._db_reconnect_attempts = 0
            return True
        except (OperationalError, InterfaceError) as e:
            logger.warning(f"Database connection error: {e}")
            if self._db_reconnect_attempts < self._max_db_reconnect_attempts:
                self._db_reconnect_attempts += 1
                try:
                    # Attempt to reconnect
                    from frigate.models import Event
                    Event._meta.database.close()
                    Event._meta.database.connect()
                    logger.info("Successfully reconnected to database")
                    return True
                except Exception as reconnect_error:
                    logger.error(f"Failed to reconnect to database: {reconnect_error}")
                    time.sleep(5)  # Wait before next attempt
            else:
                logger.error("Max database reconnection attempts reached")
        return False
    
    def _process_snapshot(self, event: Event) -> bool:
        """Process a single snapshot for upload."""
        try:
            # Check if already uploaded or in failed state
            if event.id in self.uploaded_events:
                return True
                
            if event.id in self.failed_events:
                if self.failed_events[event.id] >= self.max_failed_retries:
                    return False
            
            # Find snapshot file
            snapshot_paths = [
                os.path.join(CLIPS_DIR, f"{event.camera}-{event.id}-snapshot.jpg"),
                os.path.join(CLIPS_DIR, "snapshots", event.camera, f"{event.id}.jpg"),
                os.path.join(CLIPS_DIR, f"{event.camera}-{event.id}.jpg"),
            ]
            
            snapshot_path = None
            for path in snapshot_paths:
                if os.path.exists(path):
                    snapshot_path = path
                    break
            
            if not snapshot_path:
                # No snapshot file found, mark as processed to avoid repeated checks
                self.uploaded_events.add(event.id)
                logger.debug(f"No snapshot file found for event {event.id}")
                return False
            
            # Verify file is complete (not being written)
            try:
                file_size = os.path.getsize(snapshot_path)
                if file_size < 1000:  # Minimum expected size
                    logger.debug(f"Snapshot file too small for event {event.id}, skipping")
                    return False
                
                # Check if file is still being written
                time.sleep(0.1)
                new_size = os.path.getsize(snapshot_path)
                if new_size != file_size:
                    logger.debug(f"Snapshot file still being written for event {event.id}")
                    return False
                    
            except OSError:
                return False
            
            # Upload to S3
            success = self.s3_api.upload_snapshot(
                file_path=snapshot_path,
                event_id=event.id,
                timestamp=event.start_time,
                camera=event.camera,
                label=event.label,
                async_upload=True  # Always async for reliability
            )
            
            if success:
                self.uploaded_events.add(event.id)
                if event.id in self.failed_events:
                    del self.failed_events[event.id]
                logger.debug(f"Queued snapshot upload for event {event.id}")
                return True
            else:
                # Track failed upload
                self.failed_events[event.id] = self.failed_events.get(event.id, 0) + 1
                logger.warning(f"Failed to queue snapshot upload for event {event.id}")
                return False
                
        except Exception as e:
            logger.error(f"Error processing snapshot for event {event.id}: {e}")
            self.failed_events[event.id] = self.failed_events.get(event.id, 0) + 1
            return False
    
    def _check_for_new_snapshots(self) -> None:
        """Check for new snapshots to upload."""
        if not self._ensure_db_connection():
            logger.error("Database connection lost, skipping snapshot check")
            return
            
        try:
            current_time = time.time()
            
            # First, retry any failed events
            if self.failed_events:
                failed_ids = list(self.failed_events.keys())[:10]  # Process 10 at a time
                for event_id in failed_ids:
                    if self.failed_events.get(event_id, 0) >= self.max_failed_retries:
                        continue
                    try:
                        event = Event.get_by_id(event_id)
                        self._process_snapshot(event)
                    except DoesNotExist:
                        del self.failed_events[event_id]
            
            # Query for new events with snapshots
            query = (
                Event.select()
                .where(
                    Event.has_snapshot == True,
                    Event.end_time != None,
                    Event.end_time > self.last_check_time
                )
                .order_by(Event.start_time.desc())
                .limit(self.batch_size)
            )
            
            events_processed = 0
            for event in query:
                if event.id in self.uploaded_events:
                    continue
                
                # Skip disabled cameras
                camera_config = self.config.cameras.get(event.camera)
                if not camera_config or not camera_config.enabled:
                    self.uploaded_events.add(event.id)
                    continue
                
                # Add to cache for potential future reference
                self._event_cache.append({
                    'id': event.id,
                    'camera': event.camera,
                    'timestamp': event.start_time
                })
                
                if self._process_snapshot(event):
                    events_processed += 1
            
            if events_processed > 0:
                logger.info(f"Queued {events_processed} new snapshots for upload")
            
            self.last_check_time = current_time
            
        except Exception as e:
            logger.error(f"Error checking for new snapshots: {e}")
    
    def _cleanup_old_events(self) -> None:
        """Remove old events from tracking to prevent memory growth."""
        try:
            # Keep only events from the last 7 days
            cutoff_time = time.time() - (7 * 24 * 60 * 60)
            initial_size = len(self.uploaded_events)
            
            # Get current event IDs from last 7 days
            recent_events = set()
            if self._ensure_db_connection():
                query = (
                    Event.select(Event.id)
                    .where(Event.start_time > cutoff_time)
                    .tuples()
                )
                recent_events = {event[0] for event in query}
            
            # Keep only recent events in uploaded set
            self.uploaded_events = self.uploaded_events.intersection(recent_events)
            
            # Also cleanup failed events
            failed_to_remove = []
            for event_id in list(self.failed_events.keys()):
                if event_id not in recent_events:
                    failed_to_remove.append(event_id)
            
            for event_id in failed_to_remove:
                del self.failed_events[event_id]
            
            removed_count = initial_size - len(self.uploaded_events)
            if removed_count > 0:
                logger.info(f"Cleaned up {removed_count} old events from tracking")
            
        except Exception as e:
            logger.error(f"Error cleaning up old events: {e}")
    
    def _log_stats(self) -> None:
        """Log upload statistics."""
        current_time = time.time()
        if current_time - self._last_stats_log < self._stats_log_interval:
            return
            
        self._last_stats_log = current_time
        
        stats = self.s3_api.get_stats()
        logger.info(
            f"S3 Upload Stats - Success: {stats['success']}, "
            f"Failed: {stats['failed']}, "
            f"Retry Queue: {stats['retry_queue_size']}, "
            f"Tracked Events: {len(self.uploaded_events)}, "
            f"Failed Events: {len(self.failed_events)}, "
            f"Health: {'OK' if stats['is_healthy'] else 'DEGRADED'}"
        )
    
    def run(self) -> None:
        """Main thread loop with enhanced reliability."""
        if not self.s3_api.is_active():
            logger.warning("S3 snapshot mirroring is not configured or unhealthy, exiting")
            return
        
        logger.info("S3 snapshot mirror started with enhanced reliability")
        
        # Process any existing snapshots on startup
        try:
            self._check_for_new_snapshots()
        except Exception as e:
            logger.error(f"Error during initial snapshot check: {e}")
        
        last_cleanup = time.time()
        cleanup_interval = 3600  # Cleanup every hour
        last_state_save = time.time()
        state_save_interval = 60  # Save state every minute
        health_check_failures = 0
        max_health_failures = 5
        
        while self._running and not self.stop_event.wait(self.check_interval):
            try:
                # Check S3 health
                if not self.s3_api.is_active():
                    health_check_failures += 1
                    if health_check_failures >= max_health_failures:
                        logger.error("S3 service unhealthy for too long, pausing operations")
                        time.sleep(60)  # Wait longer before retry
                        health_check_failures = 0
                    continue
                else:
                    health_check_failures = 0
                
                # Check for new snapshots
                self._check_for_new_snapshots()
                
                # Log statistics
                self._log_stats()
                
                current_time = time.time()
                
                # Periodic cleanup
                if current_time - last_cleanup > cleanup_interval:
                    self._cleanup_old_events()
                    last_cleanup = current_time
                
                # Frequent state saves for reliability
                if current_time - last_state_save > state_save_interval:
                    self._save_state()
                    last_state_save = current_time
                    
            except Exception as e:
                logger.error(f"Unexpected error in S3 mirror main loop: {e}")
                time.sleep(10)  # Wait before continuing
        
        # Cleanup on exit
        logger.info("S3 snapshot mirror stopping...")
        self._running = False
        
        # Final state save
        self._save_state()
        
        # Shutdown S3 API (waits for pending uploads)
        self.s3_api.shutdown()
        
        # Final stats
        self._log_stats()
        
        logger.info("S3 snapshot mirror stopped")