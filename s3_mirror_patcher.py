#!/usr/bin/env python3
"""
Enhanced runtime patcher for Frigate to add S3 snapshot mirroring.
This script patches Frigate's app.py at runtime with maximum reliability.
"""

import sys
import os
import time
import traceback
import importlib.util
import signal

# Ensure we handle signals properly for clean shutdown
signal.signal(signal.SIGTERM, lambda sig, frame: sys.exit(0))
signal.signal(signal.SIGINT, lambda sig, frame: sys.exit(0))

def wait_for_frigate_modules():
    """Wait for Frigate modules to be available."""
    max_attempts = 30
    for attempt in range(max_attempts):
        try:
            import frigate
            import frigate.app
            return True
        except ImportError:
            if attempt < max_attempts - 1:
                print(f"Waiting for Frigate modules... attempt {attempt + 1}/{max_attempts}")
                time.sleep(1)
            else:
                print("ERROR: Frigate modules not found after 30 seconds")
                return False
    return False

def patch_frigate():
    """Dynamically patch Frigate to add S3 functionality with enhanced error handling."""
    try:
        import shutil
        
        frigate_dir = "/opt/frigate/frigate"
        
        # Ensure frigate directory exists
        if not os.path.exists(frigate_dir):
            print(f"ERROR: Frigate directory not found at {frigate_dir}")
            return False
        
        # Copy S3 modules with verification
        files_to_copy = [
            ("s3.py", f"{frigate_dir}/s3.py"),
            ("s3_snapshot_mirror.py", f"{frigate_dir}/s3_snapshot_mirror.py")
        ]
        
        for src, dst in files_to_copy:
            src_path = f"/app/{src}"
            if os.path.exists(src_path):
                try:
                    shutil.copy2(src_path, dst)
                    print(f"Successfully copied {src} to {dst}")
                    
                    # Verify the file was copied correctly
                    if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src_path):
                        print(f"ERROR: Failed to verify copy of {src}")
                        return False
                except Exception as e:
                    print(f"ERROR: Failed to copy {src}: {e}")
                    return False
            else:
                print(f"ERROR: Source file {src_path} not found")
                return False
        
        # Import Frigate modules
        import frigate.app
        from frigate.app import FrigateApp, logger
        
        # Store original methods
        original_start_event_processor = FrigateApp.start_event_processor
        original_stop = FrigateApp.stop
        
        # Define new methods with enhanced error handling
        def start_s3_snapshot_mirror(self):
            """Start S3 snapshot mirror service with error handling."""
            try:
                # Only start if S3 is configured
                if not all([os.environ.get(k) for k in ['S3_BUCKET', 'S3_ACCESS_KEY', 'S3_SECRET_KEY']]):
                    logger.info("S3 not configured, skipping snapshot mirror")
                    return
                
                # Import here to ensure module is available
                from frigate.s3_snapshot_mirror import S3SnapshotMirror
                
                self.s3_snapshot_mirror = S3SnapshotMirror(self.config, self.stop_event)
                self.s3_snapshot_mirror.daemon = True  # Ensure thread doesn't block shutdown
                self.s3_snapshot_mirror.start()
                
                # Add to processes dict for monitoring
                if hasattr(self, 'processes'):
                    self.processes['s3_mirror'] = 's3_mirror_thread'
                
                logger.info("S3 snapshot mirror started successfully")
                
            except ImportError as e:
                logger.error(f"Failed to import S3 modules: {e}")
                logger.error("S3 snapshot mirroring will be disabled")
            except Exception as e:
                logger.error(f"Failed to start S3 snapshot mirror: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                # Don't crash Frigate if S3 mirror fails
        
        def patched_start_event_processor(self):
            """Start event processor and then S3 mirror."""
            # Call original method
            original_start_event_processor(self)
            
            # Start S3 mirror after event processor
            try:
                start_s3_snapshot_mirror(self)
            except Exception as e:
                logger.error(f"Error starting S3 mirror: {e}")
                # Continue even if S3 mirror fails
        
        def patched_stop(self):
            """Enhanced stop method with S3 mirror cleanup."""
            try:
                # Stop S3 mirror first if it exists
                if hasattr(self, 's3_snapshot_mirror'):
                    logger.info("Stopping S3 snapshot mirror...")
                    try:
                        # Set running flag to false if it exists
                        if hasattr(self.s3_snapshot_mirror, '_running'):
                            self.s3_snapshot_mirror._running = False
                        
                        # Give it time to finish current operations
                        self.s3_snapshot_mirror.join(timeout=10)
                        
                        if self.s3_snapshot_mirror.is_alive():
                            logger.warning("S3 mirror thread did not stop gracefully")
                    except Exception as e:
                        logger.error(f"Error stopping S3 mirror: {e}")
            except Exception as e:
                logger.error(f"Error in S3 shutdown: {e}")
            
            # Call original stop method
            original_stop(self)
        
        # Apply patches
        FrigateApp.start_event_processor = patched_start_event_processor
        FrigateApp.stop = patched_stop
        
        print("Successfully patched Frigate with S3 snapshot mirroring")
        return True
        
    except Exception as e:
        print(f"ERROR: Failed to patch Frigate: {e}")
        print(f"Traceback: {traceback.format_exc()}")
        return False

def main():
    """Main entry point with enhanced error handling."""
    # Wait for Frigate modules
    if not wait_for_frigate_modules():
        print("ERROR: Could not load Frigate modules, running without S3 support")
        # Continue anyway - Frigate will run without S3
    else:
        # Only patch if S3 is configured
        if all([os.environ.get(k) for k in ['S3_BUCKET', 'S3_ACCESS_KEY', 'S3_SECRET_KEY']]):
            print("S3 configuration detected, patching Frigate...")
            
            # Install boto3 if needed
            try:
                import boto3
            except ImportError:
                print("Installing boto3...")
                import subprocess
                subprocess.run([
                    sys.executable, "-m", "pip", "install", "--no-cache-dir", 
                    "boto3>=1.26.0", "botocore>=1.29.0"
                ], check=True)
            
            # Apply patch
            if patch_frigate():
                print("Frigate patched successfully")
            else:
                print("WARNING: Failed to patch Frigate, continuing without S3 support")
        else:
            print("S3 not configured, skipping patch")
    
    # Run Frigate
    print("Starting Frigate...")
    os.execvp(sys.executable, [sys.executable, "-m", "frigate"] + sys.argv[1:])

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        print(f"Traceback: {traceback.format_exc()}")
        sys.exit(1)