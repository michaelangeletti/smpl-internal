#!/usr/bin/env python3
"""
Secure file replication with integrity verification.
Copies files from remote server using rsync and validates checksums.
"""

import subprocess
import logging
import sys
import time
import select
import re
import hashlib
from pathlib import Path
from typing import Optional, Tuple, Set, List, Dict
from dataclasses import dataclass

# --- ANSI Color Codes ---
class Colors:
    """ANSI color codes for terminal output."""
    RESET = '\033[0m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    
    @staticmethod
    def disable():
        """Disable colors (for non-TTY output)."""
        Colors.RESET = ''
        Colors.RED = ''
        Colors.GREEN = ''
        Colors.YELLOW = ''
        Colors.BLUE = ''
        Colors.MAGENTA = ''
        Colors.CYAN = ''
        Colors.BOLD = ''

# --- Configuration ---
REMOTE_HOST_ALIAS = "sul-smpl.stanford.edu"
REMOTE_DIR = "/pool0/smpl2/digreq-2664"
LOCAL_DIR = "/Users/mangelet/Desktop/copy"
LOG_FILE = "/Users/mangelet/Desktop/digreq-2664_service_service_replication_log.txt"

# --- Constants ---
SSH_TIMEOUT = 30  # seconds
RSYNC_TIMEOUT = 3600  # 1 hour max for rsync
MD5_CHUNK_SIZE = 8192
PROGRESS_DOT_INTERVAL = 2.0  # seconds

# Disable colors if output is not a TTY
if not sys.stdout.isatty():
    Colors.disable()


class ColoredFormatter(logging.Formatter):
    """Custom formatter that adds colors based on log level."""
    
    FORMATS = {
        logging.DEBUG: Colors.CYAN + '%(asctime)s - DEBUG: %(message)s' + Colors.RESET,
        logging.INFO: '%(asctime)s - %(message)s',
        logging.WARNING: Colors.YELLOW + '%(asctime)s - WARNING: %(message)s' + Colors.RESET,
        logging.ERROR: Colors.RED + '%(asctime)s - ERROR: %(message)s' + Colors.RESET,
        logging.CRITICAL: Colors.RED + Colors.BOLD + '%(asctime)s - CRITICAL: %(message)s' + Colors.RESET,
    }
    
    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno, self.FORMATS[logging.INFO])
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)

@dataclass
class TransferStats:
    """Statistics for the file transfer operation."""
    remote_file_count: int
    remote_size: str
    local_file_count: int
    local_size: str
    duration: float
    remote_orphans: List[str]
    missing_files: Set[str]
    checksum_mismatches: List[str]

# --- Setup Logging ---
# File handler without colors
file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8', errors='surrogateescape')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s: %(message)s'))

# Console handler with colors
console_handler = logging.StreamHandler()
console_handler.setFormatter(ColoredFormatter())

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)
logger = logging.getLogger(__name__)


def run_ssh_command(command: str, timeout: int = SSH_TIMEOUT) -> Optional[str]:
    """
    Execute a command on the remote host via SSH.
    
    Args:
        command: The command to execute on remote host
        timeout: Maximum time to wait for command completion
        
    Returns:
        Command output as string, or None if command failed
    """
    ssh_cmd = ['ssh', REMOTE_HOST_ALIAS, command]
    
    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            errors='surrogateescape',
            timeout=timeout,
            check=False
        )
        
        if result.returncode != 0:
            logger.error(f"SSH command failed (exit {result.returncode}): {command}")
            if result.stderr:
                logger.error(f"Error output: {result.stderr.strip()}")
            return None
            
        return result.stdout.strip()
        
    except subprocess.TimeoutExpired:
        logger.error(f"SSH command timed out after {timeout}s: {command}")
        return None
    except Exception as e:
        logger.error(f"SSH command exception: {e}")
        return None


def verify_ssh_connection() -> bool:
    """Verify SSH connection is working."""
    logger.info(f"{Colors.CYAN}Verifying SSH connection...{Colors.RESET}")
    result = run_ssh_command("echo 'SSH_OK'", timeout=10)
    
    if result and 'SSH_OK' in result:
        logger.info(f"{Colors.GREEN}✓ SSH connection verified successfully{Colors.RESET}")
        return True
    
    logger.error(f"SSH connection failed to {REMOTE_HOST_ALIAS}")
    logger.error("Please ensure SSH key authentication is configured")
    return False


def get_remote_stats(remote_path: str) -> Tuple[Set[str], str, List[str]]:
    """
    Get remote directory statistics in a single SSH session.
    
    Returns:
        Tuple of (file manifest, total size, orphan files without .md5)
    """
    logger.info(f"{Colors.CYAN}Gathering remote directory statistics...{Colors.RESET}")
    
    # Single SSH command that does everything in one session
    combined_cmd = f"""
    cd {remote_path} 2>/dev/null || exit 1
    
    # List all files (excluding hidden) and calculate total size
    find . -type f ! -path '*/.*' -print0 | 
    while IFS= read -r -d '' file; do
        echo "FILE:$file"
    done
    
    # Calculate total size using actual file sizes (not disk usage)
    total_bytes=$(find . -type f ! -path '*/.*' -exec stat -f%z {{}} + 2>/dev/null | awk '{{sum+=$1}} END {{print sum}}')
    if [ -z "$total_bytes" ]; then
        # Fallback for Linux (stat has different syntax)
        total_bytes=$(find . -type f ! -path '*/.*' -exec stat -c%s {{}} + 2>/dev/null | awk '{{sum+=$1}} END {{print sum}}')
    fi
    echo "SIZE_BYTES:$total_bytes"
    """
    
    output = run_ssh_command(combined_cmd, timeout=SSH_TIMEOUT * 2)
    
    if not output:
        logger.error("Failed to get remote statistics")
        return set(), "0B", []
    
    manifest = set()
    remote_files = set()
    orphans = []
    total_bytes = 0
    
    for line in output.splitlines():
        if line.startswith("FILE:"):
            # Remove "FILE:" prefix and leading "./"
            file_path = line[5:].lstrip('./')
            if file_path:  # Skip empty paths
                manifest.add(file_path)
                remote_files.add(file_path)
        elif line.startswith("SIZE_BYTES:"):
            try:
                total_bytes = int(line[11:].strip())
            except (ValueError, AttributeError):
                total_bytes = 0
    
    # Check for orphan files (data files without .md5 sidecars)
    for file_path in sorted(remote_files):
        if not file_path.endswith('.md5'):
            md5_path = f"{file_path}.md5"
            if md5_path not in remote_files:
                orphans.append(file_path)
    
    size_str = format_bytes(total_bytes)
    logger.info(f"{Colors.GREEN}✓ Remote: {len(manifest)} files, {size_str}{Colors.RESET}")
    
    return manifest, size_str, orphans


def extract_hash_from_sidecar(sidecar_path: Path) -> Optional[str]:
    """Extract MD5 hash from sidecar file."""
    try:
        content = sidecar_path.read_text(errors='ignore')
        match = re.search(r'([a-fA-F0-9]{32})', content)
        return match.group(1).lower() if match else None
    except Exception as e:
        logger.error(f"Error reading sidecar {sidecar_path}: {e}")
        return None


def calculate_md5(file_path: Path) -> Optional[str]:
    """Calculate MD5 hash of a file."""
    try:
        md5_hash = hashlib.md5()
        with file_path.open('rb') as f:
            for chunk in iter(lambda: f.read(MD5_CHUNK_SIZE), b''):
                md5_hash.update(chunk)
        return md5_hash.hexdigest()
    except Exception as e:
        logger.error(f"Error calculating MD5 for {file_path}: {e}")
        return None


def format_bytes(num_bytes: int) -> str:
    """Convert bytes to human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f}PB"


def verify_integrity(
    local_path: Path,
    remote_manifest: Set[str],
    remote_size: str,
    duration: float,
    remote_orphans: List[str]
) -> TransferStats:
    """
    Verify integrity of transferred files by checking MD5 checksums.
    
    Returns:
        TransferStats object with verification results
    """
    logger.info(f"\n{Colors.CYAN}--- Starting Integrity Verification ---{Colors.RESET}")
    
    local_files_map: Dict[str, Path] = {}
    total_local_size = 0
    source_folder = Path(REMOTE_DIR).name
    
    # Build local file inventory
    for item in local_path.rglob('*'):
        if item.is_file() and not item.name.startswith('.'):
            # Skip hidden files/folders
            if any(part.startswith('.') for part in item.parts):
                continue
                
            total_local_size += item.stat().st_size
            
            # Get relative path
            try:
                rel_path = item.relative_to(local_path)
            except ValueError:
                continue
            
            # Normalize path (remove source folder prefix if present)
            parts = rel_path.parts
            if parts and parts[0] == source_folder:
                normalized = Path(*parts[1:]) if len(parts) > 1 else Path('')
            else:
                normalized = rel_path
            
            if str(normalized):  # Skip empty paths
                local_files_map[str(normalized)] = item
    
    local_manifest = set(local_files_map.keys())
    missing_from_local = remote_manifest - local_manifest
    checksum_mismatches = []
    
    # Verify checksums for data files
    data_files = [f for f in sorted(local_manifest) if not f.endswith('.md5')]
    
    logger.info(f"{Colors.CYAN}Verifying checksums for {len(data_files)} files...{Colors.RESET}")
    verified_count = 0
    
    for rel_path in data_files:
        full_path = local_files_map[rel_path]
        sidecar_path = Path(str(full_path) + '.md5')
        
        if sidecar_path.exists():
            expected_md5 = extract_hash_from_sidecar(sidecar_path)
            
            if not expected_md5:
                logger.error(f"  Invalid sidecar format: {rel_path}.md5")
                continue
            
            actual_md5 = calculate_md5(full_path)
            
            if not actual_md5:
                logger.error(f"  Could not calculate MD5: {rel_path}")
                continue
            
            if actual_md5 == expected_md5:
                verified_count += 1
                if verified_count % 10 == 0:  # Log every 10th file to reduce noise
                    logger.info(f"{Colors.GREEN}  ✓ Verified {verified_count}/{len(data_files)} files...{Colors.RESET}")
            else:
                logger.error(f"  MD5 MISMATCH: {rel_path}")
                logger.error(f"    Expected: {expected_md5}")
                logger.error(f"    Actual:   {actual_md5}")
                checksum_mismatches.append(rel_path)
    
    logger.info(f"{Colors.GREEN}  ✓ Verified {verified_count}/{len(data_files)} files{Colors.RESET}")
    
    # Create stats object
    stats = TransferStats(
        remote_file_count=len(remote_manifest),
        remote_size=remote_size,
        local_file_count=len(local_manifest),
        local_size=format_bytes(total_local_size),
        duration=duration,
        remote_orphans=remote_orphans,
        missing_files=missing_from_local,
        checksum_mismatches=checksum_mismatches
    )
    
    return stats


def print_final_report(stats: TransferStats) -> bool:
    """
    Print final integrity report.
    
    Returns:
        True if verification passed, False otherwise
    """
    logger.info("\n" + Colors.BOLD + "=" * 60)
    logger.info("FINAL INTEGRITY REPORT")
    logger.info("=" * 60 + Colors.RESET)
    logger.info(f"Remote: {stats.remote_file_count} files ({stats.remote_size})")
    logger.info(f"Local:  {stats.local_file_count} files ({stats.local_size})")
    logger.info(f"Duration: {stats.duration:.2f} seconds ({stats.duration/60:.1f} minutes)")
    logger.info("-" * 60)
    
    success = True
    
    # Check for missing sidecars on source (WARNING, not a failure)
    if stats.remote_orphans:
        logger.warning(f"⚠ {len(stats.remote_orphans)} files missing .md5 sidecars on SERVER:")
        for orphan in stats.remote_orphans[:10]:  # Show first 10
            logger.warning(f"  → {orphan}")
        if len(stats.remote_orphans) > 10:
            logger.warning(f"  ... and {len(stats.remote_orphans) - 10} more")
        logger.warning("Note: These files were still copied, but cannot be verified")
    else:
        logger.info(f"{Colors.GREEN}✓ SOURCE VERIFIED: All server files have .md5 sidecars{Colors.RESET}")
    
    # Check file counts
    if stats.remote_file_count == stats.local_file_count:
        logger.info(f"{Colors.GREEN}✓ FILE COUNT: Match confirmed{Colors.RESET}")
    else:
        success = False
        logger.error(f"✗ FILE COUNT MISMATCH: Remote={stats.remote_file_count}, Local={stats.local_file_count}")
    
    # Check for missing files
    if stats.missing_files:
        success = False
        logger.error(f"✗ MISSING FILES: {len(stats.missing_files)} files not copied:")
        for f in sorted(list(stats.missing_files))[:10]:
            logger.error(f"  → {f}")
        if len(stats.missing_files) > 10:
            logger.error(f"  ... and {len(stats.missing_files) - 10} more")
    
    # Check for checksum mismatches
    if stats.checksum_mismatches:
        success = False
        logger.error(f"✗ CHECKSUM FAILURES: {len(stats.checksum_mismatches)} files failed MD5 verification:")
        for f in stats.checksum_mismatches:
            logger.error(f"  → {f}")
    
    logger.info("=" * 60)
    
    if success:
        logger.info(f"{Colors.GREEN}{Colors.BOLD}✓ SUCCESS: All files replicated and verified{Colors.RESET}")
    else:
        logger.error(f"{Colors.RED}{Colors.BOLD}✗ VERIFICATION FAILED: Issues detected (see above){Colors.RESET}")
    
    logger.info(Colors.BOLD + "=" * 60 + Colors.RESET)
    
    return success


def run_rsync_transfer(src: str, dst: Path) -> Tuple[int, float]:
    """
    Execute rsync transfer with progress monitoring.
    
    Returns:
        Tuple of (return_code, duration)
    """
    start_time = time.time()
    
    rsync_cmd = [
        'rsync',
        '-avz8',
        '--protect-args',
        '--iconv=UTF-8-MAC,UTF-8',
        '--exclude=.*',
        '--info=progress2',  # Better progress info
        src,
        str(dst)
    ]
    
    logger.info(f"{Colors.CYAN}Starting rsync transfer...{Colors.RESET}")
    logger.info(f"Command: {' '.join(rsync_cmd)}")
    
    try:
        process = subprocess.Popen(
            rsync_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors='surrogateescape',
            bufsize=1  # Line buffered
        )
        
        logger.info(f"{Colors.CYAN}Transfer in progress (live updates below)...{Colors.RESET}")
        last_activity_time = time.time()
        last_log_time = time.time()
        progress_line_active = False
        LOG_INTERVAL = 30  # Log progress to file every 30 seconds
        last_logged_line = ""
        
        while True:
            # Check for output with timeout
            reads = [process.stdout, process.stderr]
            ret = select.select(reads, [], [], 0.5)
            
            if process.stdout in ret[0]:
                line = process.stdout.readline()
                if line:
                    line = line.strip()
                    if line:
                        last_activity_time = time.time()
                        
                        # Handle progress lines (contain transfer stats)
                        if any(keyword in line for keyword in ['to-chk', 'xfr#', '%', '/s']):
                            current_time = time.time()
                            
                            # Always update terminal display
                            if sys.stdout.isatty():
                                # Clear line and write new progress
                                sys.stdout.write(f"\r{Colors.CYAN}[rsync] {line:<80}{Colors.RESET}")
                                sys.stdout.flush()
                                progress_line_active = True
                            
                            # Only log to file every LOG_INTERVAL seconds
                            if current_time - last_log_time >= LOG_INTERVAL and line != last_logged_line:
                                # Write directly to file handler to avoid formatter
                                timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
                                file_handler.stream.write(f"{timestamp} - INFO: [rsync] {line}\n")
                                file_handler.stream.flush()
                                last_log_time = current_time
                                last_logged_line = line
                        
                        # Log summary lines immediately
                        elif any(keyword in line.lower() for keyword in ['speedup', 'total size', 'sent', 'received']):
                            if progress_line_active:
                                sys.stdout.write("\n")
                                progress_line_active = False
                            logger.info(f"{Colors.CYAN}[rsync] {line}{Colors.RESET}")
            
            if process.stderr in ret[0]:
                line = process.stderr.readline()
                if line:
                    last_activity_time = time.time()
                    if progress_line_active:
                        sys.stdout.write("\n")
                        progress_line_active = False
                    logger.warning(f"[rsync stderr] {line.strip()}")
            
            # Show heartbeat dot if no activity for a while
            current_time = time.time()
            if current_time - last_activity_time > PROGRESS_DOT_INTERVAL * 2:  # Double interval for heartbeat
                if progress_line_active:
                    # Just add a dot after the progress line
                    sys.stdout.write(f" {Colors.YELLOW}•{Colors.RESET}")
                else:
                    sys.stdout.write(f"{Colors.YELLOW}•{Colors.RESET}")
                sys.stdout.flush()
                last_activity_time = current_time
            
            # Check if process finished
            if process.poll() is not None:
                break
        
        # Get remaining output
        stdout, stderr = process.communicate()
        if stdout:
            for line in stdout.splitlines():
                line = line.strip()
                if line and any(keyword in line.lower() for keyword in ['total size', 'speedup', 'sent', 'received']):
                    if progress_line_active:
                        sys.stdout.write("\n")
                        progress_line_active = False
                    logger.info(f"{Colors.CYAN}[rsync] {line}{Colors.RESET}")
        
        if progress_line_active:
            sys.stdout.write("\n")
        
        duration = time.time() - start_time
        
        if process.returncode != 0:
            logger.warning(f"Rsync completed with exit code {process.returncode}")
            if stderr:
                logger.warning(f"Errors: {stderr.strip()}")
        else:
            logger.info(f"{Colors.GREEN}✓ Rsync completed successfully in {duration:.2f} seconds{Colors.RESET}")
        
        return process.returncode, duration
        
    except Exception as e:
        logger.error(f"Rsync execution error: {e}")
        return 1, time.time() - start_time


def replicate() -> bool:
    """
    Main replication function.
    
    Returns:
        True if replication and verification succeeded, False otherwise
    """
    logger.info(Colors.BOLD + "="*60)
    logger.info(f"Starting replication from {REMOTE_HOST_ALIAS}")
    logger.info(f"Remote: {REMOTE_DIR}")
    logger.info(f"Local:  {LOCAL_DIR}")
    logger.info("="*60 + Colors.RESET)
    
    # Verify SSH connection
    if not verify_ssh_connection():
        logger.error("Cannot proceed without SSH connection")
        return False
    
    # Get remote statistics
    remote_manifest, remote_size, remote_orphans = get_remote_stats(REMOTE_DIR)
    
    if not remote_manifest:
        logger.error("No files found on remote server or error occurred")
        return False
    
    # Prepare local directory
    local_path = Path(LOCAL_DIR)
    local_path.mkdir(parents=True, exist_ok=True)
    
    # Execute rsync
    src = f"{REMOTE_HOST_ALIAS}:{REMOTE_DIR}"
    return_code, duration = run_rsync_transfer(src, local_path)
    
    # Verify integrity
    stats = verify_integrity(local_path, remote_manifest, remote_size, duration, remote_orphans)
    
    # Print report
    success = print_final_report(stats)
    
    return success and return_code == 0


if __name__ == "__main__":
    try:
        success = replicate()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        logger.warning("\nOperation cancelled by user")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)