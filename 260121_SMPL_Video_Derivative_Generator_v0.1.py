#!/usr/bin/env python3
"""
Video Transcoding for Wowza streaming
Batch processes video files with deinterlacing, scaling, and H.264 encoding.

Stanford Media Preservation Lab
Video Derivative Generator - v0.1
January 2026
"""

import os
import sys
import subprocess
import json
import math
import csv
import shutil
import re
import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass, asdict
from enum import Enum
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor, as_completed

# ==============================================================================
# CONSTANTS
# ==============================================================================

class ProcessStatus(Enum):
    SUCCESS = "Success"
    ERROR = "Error"
    SKIPPED = "Skipped"
    INCOMPLETE = "Incomplete"

VIDEO_EXTENSIONS = (
    '.mp4', '.mov', '.mkv', '.mxf', '.avi', '.mpg', '.mpeg', '.m2v', '.ts',
    '.vob', '.wmv', '.asf', '.flv', '.f4v', '.rm', '.rmvb', '.dv', '.dif',
    '.webm', '.ogg', '.ogv', '.3gp', '.3g2', '.m4v'
)

# Bitrate configurations (height -> (bitrate, maxrate, bufsize))
BITRATE_CONFIG = {
    'sd': {'bitrate': '1000k', 'maxrate': '1200k', 'bufsize': '2000k'},
    'hd': {'bitrate': '2800k', 'maxrate': '2900k', 'bufsize': '5800k'}
}

# Thumbnail generation positions (as percentage of duration)
THUMBNAIL_POSITIONS = [0.10, 0.40, 0.60, 0.90]

# GOP multiplier
GOP_MULTIPLIER = 2

# FFmpeg validation timeout
VALIDATION_TIMEOUT = 300

# ==============================================================================
# DATA CLASSES
# ==============================================================================

@dataclass
class VideoInfo:
    """Stores video stream information"""
    width: int
    height: int
    duration: float
    fps: float
    dar: str
    has_audio: bool
    total_frames: int
    codec: str

@dataclass
class ProcessingResult:
    """Result of processing a single video file"""
    source_file: str
    status: ProcessStatus
    audio_status: str
    message: str
    timestamp: str

@dataclass
class ProcessingStats:
    """Overall processing statistics"""
    total: int = 0
    success: int = 0
    error: int = 0
    skipped: int = 0
    failed_files: List[str] = None

    def __post_init__(self):
        if self.failed_files is None:
            self.failed_files = []

# ==============================================================================
# CONFIGURATION
# ==============================================================================

class Config:
    """Configuration management"""
    
    def __init__(self, args: argparse.Namespace):
        self.source_dir = Path(args.source_dir)
        self.output_dir = Path(args.output_dir)
        self.finished_dir = self.source_dir / "finished_sources"
        self.log_dir = self.output_dir / "process_logs"
        self.csv_log = self.output_dir / "transcode_summary.csv"
        
        self.cleanup_only = args.cleanup_only
        self.move_finished = args.move_finished
        self.dry_run = args.dry_run
        self.workers = args.workers
        self.skip_validation = args.skip_validation
        
        # Validate paths
        if not self.source_dir.exists():
            raise FileNotFoundError(f"Source directory does not exist: {self.source_dir}")
        
        # Create output directories (even in dry-run for logging)
        for directory in [self.output_dir, self.log_dir, self.finished_dir]:
            directory.mkdir(parents=True, exist_ok=True)

# ==============================================================================
# LOGGING SETUP
# ==============================================================================

def setup_logging(log_dir: Path, dry_run: bool) -> logging.Logger:
    """Configure logging with both file and console handlers"""
    logger = logging.getLogger('video_transcoder')
    logger.setLevel(logging.DEBUG)
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter('%(levelname)s: %(message)s')
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # File handler
    if not dry_run:
        log_file = log_dir / f"transcode_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)
        
        # Write banner to log file
        with open(log_file, 'a') as f:
            f.write("=" * 70 + "\n")
            f.write("Stanford Media Preservation Lab\n")
            f.write("Video Derivative Generator - v0.1\n")
            f.write("January 2026\n")
            f.write("=" * 70 + "\n\n")
    
    return logger

# ==============================================================================
# UTILITY FUNCTIONS
# ==============================================================================

def check_dependencies() -> bool:
    """Verify that required external tools are available"""
    logger = logging.getLogger('video_transcoder')
    required_tools = ['ffmpeg', 'ffprobe']
    
    for tool in required_tools:
        if shutil.which(tool) is None:
            logger.error(f"Required tool '{tool}' not found in PATH")
            return False
    
    logger.info("All required dependencies found")
    return True

def check_disk_space(output_dir: Path, required_gb: float = 10.0) -> bool:
    """Check if sufficient disk space is available"""
    logger = logging.getLogger('video_transcoder')
    stat = shutil.disk_usage(output_dir)
    available_gb = stat.free / (1024 ** 3)
    
    if available_gb < required_gb:
        logger.warning(f"Low disk space: {available_gb:.2f} GB available (recommended: {required_gb} GB)")
        return False
    
    logger.info(f"Disk space OK: {available_gb:.2f} GB available")
    return True

def get_completed_files(csv_path: Path) -> Set[str]:
    """Read CSV log and return set of successfully processed files"""
    completed = set()
    if not csv_path.exists():
        return completed
    
    try:
        with open(csv_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('Status') == ProcessStatus.SUCCESS.value:
                    completed.add(row.get('Source File'))
    except Exception as e:
        logging.getLogger('video_transcoder').warning(f"Error reading CSV log: {e}")
    
    return completed

def log_to_csv(csv_path: Path, result: ProcessingResult, dry_run: bool):
    """Append processing result to CSV log"""
    if dry_run:
        return
    
    file_exists = csv_path.exists()
    
    try:
        with open(csv_path, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(['Timestamp', 'Source File', 'Status', 'Audio', 'Details'])
            writer.writerow([
                result.timestamp,
                result.source_file,
                result.status.value,
                result.audio_status,
                result.message
            ])
    except Exception as e:
        logging.getLogger('video_transcoder').error(f"Error writing to CSV: {e}")

# ==============================================================================
# VIDEO PROCESSING FUNCTIONS
# ==============================================================================

def get_video_info(file_path: Path) -> VideoInfo:
    """Extract video information using ffprobe"""
    logger = logging.getLogger('video_transcoder')
    
    cmd = [
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_streams', '-show_format', str(file_path)
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise Exception("ffprobe timeout - file may be corrupt")
    except json.JSONDecodeError:
        raise Exception("Failed to parse ffprobe output")
    except Exception as e:
        raise Exception(f"ffprobe failed: {e}")
    
    # Find video and audio streams
    v_stream = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
    a_stream = next((s for s in data['streams'] if s['codec_type'] == 'audio'), None)
    
    if not v_stream:
        raise Exception("No video stream found")
    
    duration = float(data['format'].get('duration', 0))
    if duration == 0:
        raise Exception("Invalid or zero duration")
    
    # Calculate FPS
    fps_str = v_stream.get('avg_frame_rate', '30/1')
    try:
        fps = eval(fps_str)
    except:
        fps = 30.0
    
    # Get total frames
    total_frames = int(v_stream.get('nb_frames', 0))
    if total_frames == 0:
        total_frames = int(duration * fps)
    
    return VideoInfo(
        width=int(v_stream['width']),
        height=int(v_stream['height']),
        duration=duration,
        fps=fps,
        dar=v_stream.get('display_aspect_ratio', '4:3'),
        has_audio=a_stream is not None,
        total_frames=total_frames,
        codec=v_stream.get('codec_name', 'unknown')
    )

def calculate_scaling_params(width: int, height: int, dar: str) -> str:
    """Determine appropriate scaling parameters based on input dimensions"""
    # SD PAL (720x576)
    if height == 576 and width == 720:
        return "854:480" if dar == "16:9" else "640:480"
    
    # SD NTSC (720x480/486)
    elif height in [480, 486] and width == 720:
        return "854:480" if dar == "16:9" else "640:480"
    
    # HD content
    elif width >= 1280 and height >= 720:
        # Already 720p or 1080p - scale to 720p
        if (width == 1920 and height == 1080) or (width == 1280 and height == 720):
            return "1280:720"
        # Larger than 1080p
        elif width > 1920 or height > 1080:
            return "1280:720" if dar == "16:9" else "-2:720"
    
    # Default: ensure even dimensions
    return "trunc(iw/2)*2:trunc(ih/2)*2"

def get_bitrate_config(height: int) -> Dict[str, str]:
    """Get bitrate configuration based on video height"""
    return BITRATE_CONFIG['sd'] if height <= 576 else BITRATE_CONFIG['hd']

def calculate_output_fps(fps: float) -> float:
    """Calculate appropriate output frame rate"""
    is_pal = (abs(fps - 50) < 1 or abs(fps - 25) < 1)
    threshold = 25 if is_pal else 30
    
    if fps > threshold:
        return fps / 2
    return fps

def validate_output(file_path: Path, timeout: int = VALIDATION_TIMEOUT) -> Tuple[bool, str]:
    """Validate output file integrity using ffmpeg"""
    cmd = ['ffmpeg', '-v', 'error', '-i', str(file_path), '-f', 'null', '-']
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        is_valid = result.returncode == 0 and len(result.stderr) == 0
        return is_valid, result.stderr
    except subprocess.TimeoutExpired:
        return False, "Validation timeout"
    except Exception as e:
        return False, str(e)

def run_ffmpeg_with_progress(
    cmd: List[str],
    total_frames: int,
    description: str,
    log_file: Optional[Path] = None
) -> Tuple[bool, str]:
    """Execute ffmpeg command with progress bar"""
    if total_frames <= 0:
        total_frames = 1
    
    pbar = tqdm(
        total=total_frames,
        desc=description,
        unit="fr",
        leave=False,
        dynamic_ncols=True,
        colour='cyan'
    )
    
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1
    )
    
    frame_pattern = re.compile(r'frame=\s*(\d+)')
    full_output = []
    last_frame = 0
    
    try:
        for line in process.stdout:
            full_output.append(line)
            match = frame_pattern.search(line)
            if match:
                current_frame = int(match.group(1))
                diff = current_frame - last_frame
                if diff > 0:
                    pbar.update(diff)
                    last_frame = current_frame
        
        process.wait()
        
    except Exception as e:
        process.kill()
        raise e
    finally:
        pbar.close()
    
    output_text = "".join(full_output)
    
    # Write to log file if provided
    if log_file:
        try:
            with open(log_file, 'a') as f:
                f.write(output_text)
        except Exception as e:
            logging.getLogger('video_transcoder').warning(f"Failed to write log: {e}")
    
    return (process.returncode == 0), output_text

def generate_thumbnail(
    source_path: Path,
    output_path: Path,
    timestamp: float,
    scale_string: str,
    index: int,
    total: int,
    log_file: Optional[Path] = None
) -> bool:
    """Generate a single thumbnail at specified timestamp"""
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(timestamp),
        "-i", str(source_path),
        "-vf", f"bwdif=mode=0:parity=-1:deint=all,scale={scale_string},format=rgb24",
        "-frames:v", "1",
        "-update", "1",
        "-c:v", "jpeg2000",
        "-pix_fmt", "rgb24",
        str(output_path)
    ]
    
    desc = f"Thumbnail {index}/{total}"
    success, output = run_ffmpeg_with_progress(cmd, 1, desc, log_file)
    
    return success

def cleanup_temp_files(prefix: Path):
    """Remove temporary ffmpeg pass files"""
    for ext in ["-0.log", "-0.log.mbtree"]:
        temp_file = Path(str(prefix) + ext)
        if temp_file.exists():
            try:
                temp_file.unlink()
            except Exception as e:
                logging.getLogger('video_transcoder').warning(f"Failed to delete {temp_file}: {e}")

def verify_derivatives_exist(output_path: Path, thumbnail_prefix: str, num_thumbs: int = 4) -> bool:
    """Check if all expected output files exist"""
    if not output_path.exists():
        return False
    
    for i in range(1, num_thumbs + 1):
        thumb_path = output_path.parent / f"{thumbnail_prefix}_thumb_{i}.jp2"
        if not thumb_path.exists():
            return False
    
    return True

def process_single_video(
    source_path: Path,
    config: Config,
    completed_set: Set[str]
) -> ProcessingResult:
    """Process a single video file"""
    logger = logging.getLogger('video_transcoder')
    base_name = source_path.name
    root_name = source_path.stem
    
    # Generate output paths
    new_name = root_name[:-3] + "_sl.mp4"
    output_path = config.output_dir / new_name
    thumbnail_prefix = root_name[:-3]
    
    # Check if already processed
    derivatives_exist = verify_derivatives_exist(output_path, thumbnail_prefix)
    
    if base_name in completed_set and derivatives_exist:
        return ProcessingResult(
            source_file=base_name,
            status=ProcessStatus.SKIPPED,
            audio_status="N/A",
            message="Already processed",
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
    
    # Setup logging
    process_log = config.log_dir / f"{root_name}_process.log"
    stats_log_prefix = config.log_dir / f"stats_{root_name}"
    
    audio_status = "Unknown"
    
    try:
        # Get video information
        info = get_video_info(source_path)
        audio_status = "Stereo" if info.has_audio else "No Audio"
        
        # Calculate encoding parameters
        out_fps = calculate_output_fps(info.fps)
        gop = math.ceil(out_fps * GOP_MULTIPLIER)
        scale_string = calculate_scaling_params(info.width, info.height, info.dar)
        bitrate_cfg = get_bitrate_config(info.height)
        
        # Build filter chain
        vf_chain = f"bwdif=mode=0:parity=-1:deint=all,scale={scale_string},format=yuv420p"
        
        # Build ffmpeg command components
        video_mapping = ["-map", "0:v:0"]
        audio_mapping = ["-map", "0:a:0"] if info.has_audio else []
        
        video_codec_args = [
            "-c:v", "libx264",
            "-preset", "medium",
            "-profile:v", "main",
            "-fps_mode", "cfr",
            "-r", f"{out_fps:.3f}",
            "-g", str(gop),
            "-sc_threshold", "0",
            "-vf", vf_chain,
            "-b:v", bitrate_cfg['bitrate'],
            "-maxrate", bitrate_cfg['maxrate'],
            "-bufsize", bitrate_cfg['bufsize']
        ]
        
        audio_codec_args = [
            "-c:a", "aac_at",
            "-ac", "2",
            "-ar", "48000",
            "-b:a", "128k"
        ] if info.has_audio else ["-an"]
        
        # Create process log file
        with open(process_log, 'w') as log_f:
            log_f.write("=" * 70 + "\n")
            log_f.write("Stanford Media Preservation Lab\n")
            log_f.write("Video Derivative Generator - v0.1\n")
            log_f.write("January 2026\n")
            log_f.write("=" * 70 + "\n\n")
            log_f.write(f"Processing: {base_name}\n")
            log_f.write(f"Source: {info.width}x{info.height} @ {info.fps:.2f}fps\n")
            log_f.write(f"Output: {scale_string} @ {out_fps:.2f}fps\n\n")
        
        # PASS 1
        pass1_cmd = [
            "ffmpeg", "-y", "-i", str(source_path)
        ] + video_mapping + video_codec_args + [
            "-pass", "1",
            "-passlogfile", str(stats_log_prefix),
            "-an",
            "-f", "mp4",
            os.devnull
        ]
        
        success, _ = run_ffmpeg_with_progress(
            pass1_cmd,
            info.total_frames,
            f"Pass 1: {base_name[:20]}",
            process_log
        )
        
        if not success:
            raise Exception("Pass 1 encoding failed")
        
        # PASS 2
        pass2_cmd = [
            "ffmpeg", "-y", "-i", str(source_path)
        ] + video_mapping + audio_mapping + video_codec_args + [
            "-pass", "2",
            "-passlogfile", str(stats_log_prefix),
            "-tune", "film"
        ] + audio_codec_args + [
            "-movflags", "faststart",
            str(output_path)
        ]
        
        success, _ = run_ffmpeg_with_progress(
            pass2_cmd,
            info.total_frames,
            f"Pass 2: {base_name[:20]}",
            process_log
        )
        
        if not success:
            raise Exception("Pass 2 encoding failed")
        
        # Validate output
        if not config.skip_validation:
            is_valid, err_msg = validate_output(output_path)
            if not is_valid:
                raise Exception(f"Output validation failed: {err_msg}")
        
        # Generate thumbnails
        time_points = [info.duration * pos for pos in THUMBNAIL_POSITIONS]
        for idx, timestamp in enumerate(time_points, 1):
            thumb_path = config.output_dir / f"{thumbnail_prefix}_thumb_{idx}.jp2"
            success = generate_thumbnail(
                source_path,
                thumb_path,
                timestamp,
                scale_string,
                idx,
                len(time_points),
                process_log
            )
            if not success:
                raise Exception(f"Thumbnail {idx} generation failed")
        
        # Move source file if configured
        if config.move_finished and not config.dry_run:
            dest_path = config.finished_dir / base_name
            shutil.move(str(source_path), str(dest_path))
        
        return ProcessingResult(
            source_file=base_name,
            status=ProcessStatus.SUCCESS,
            audio_status=audio_status,
            message="Completed successfully",
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
    
    except Exception as e:
        logger.error(f"Error processing {base_name}: {e}")
        
        # Cleanup failed outputs
        if output_path.exists():
            try:
                output_path.unlink()
            except Exception:
                pass
        
        for i in range(1, 5):
            thumb_path = config.output_dir / f"{thumbnail_prefix}_thumb_{i}.jp2"
            if thumb_path.exists():
                try:
                    thumb_path.unlink()
                except Exception:
                    pass
        
        return ProcessingResult(
            source_file=base_name,
            status=ProcessStatus.ERROR,
            audio_status=audio_status,
            message=str(e),
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
    
    finally:
        # Cleanup temporary pass files
        cleanup_temp_files(stats_log_prefix)

# ==============================================================================
# MAIN PROCESSING LOGIC
# ==============================================================================

def collect_video_files(source_dir: Path, finished_dir: Path) -> List[Path]:
    """Recursively collect all video files from source directory"""
    files = []
    
    for root, _, filenames in os.walk(source_dir):
        root_path = Path(root)
        
        # Skip the finished directory
        if root_path.resolve() == finished_dir.resolve():
            continue
        
        for filename in filenames:
            if filename.lower().endswith(VIDEO_EXTENSIONS):
                files.append(root_path / filename)
    
    return sorted(files)

def print_file_list(files: List[Path], file_index_map: Dict[str, int] = None):
    """Display pre-flight list of files to be processed"""
    print("\n" + "=" * 70)
    print(f"FILES FOUND ({len(files)} total):")
    print("-" * 70)
    
    for i, file_path in enumerate(files, 1):
        status = ""
        if file_index_map:
            # Used during processing to show status
            filename = file_path.name
            if filename in file_index_map:
                status = " ✓"
        print(f"{i:4}. {file_path.name}{status}")
    
    print("=" * 70 + "\n")

def update_file_status(files: List[Path], completed_files: Set[str]):
    """Update and display file list with completion status"""
    # Clear screen and redisplay with status
    os.system('clear' if os.name != 'nt' else 'cls')
    
    print("\n" + "=" * 70)
    print(f"FILES PROCESSING ({len(completed_files)}/{len(files)} completed):")
    print("-" * 70)
    
    for i, file_path in enumerate(files, 1):
        filename = file_path.name
        if filename in completed_files:
            print(f"{i:4}. ✓ {filename}")
        else:
            print(f"{i:4}.   {filename}")
    
    print("=" * 70 + "\n")

def print_summary(stats: ProcessingStats, start_time: datetime):
    """Print final processing summary"""
    duration = datetime.now() - start_time
    
    print("\n" + "=" * 70)
    print("PROCESSING SUMMARY")
    print("=" * 70)
    print(f"Total files:      {stats.total}")
    print(f"Successful:       {stats.success}")
    print(f"Skipped:          {stats.skipped}")
    print(f"Errors:           {stats.error}")
    print(f"Processing time:  {duration}")
    print("=" * 70)
    
    if stats.failed_files:
        print("\nFAILED FILES:")
        for failed in stats.failed_files:
            print(f"  - {failed}")
        print()

def print_live_status(files: List[Path], session_completed: Set[str], stats: ProcessingStats):
    """Print live status update showing which files are completed"""
    status_lines = []
    status_lines.append("\n" + "=" * 70)
    status_lines.append(f"STATUS UPDATE ({len(session_completed)}/{len(files)} completed)")
    status_lines.append("-" * 70)
    
    # Show all files with their status
    for i, file_path in enumerate(files):
        filename = file_path.name
        status_icon = "✓" if filename in session_completed else "⋯"
        status_lines.append(f"{i+1:4}. {status_icon} {filename}")
    
    status_lines.append("-" * 70)
    status_lines.append(f"Success: {stats.success} | Skipped: {stats.skipped} | Errors: {stats.error}")
    status_lines.append("=" * 70 + "\n")
    
    # Use tqdm.write to print without interfering with progress bars
    for line in status_lines:
        tqdm.write(line)

def process_batch(config: Config) -> ProcessingStats:
    """Main batch processing function"""
    logger = logging.getLogger('video_transcoder')
    stats = ProcessingStats()
    start_time = datetime.now()
    
    # Collect files
    files = collect_video_files(config.source_dir, config.finished_dir)
    stats.total = len(files)
    
    if not files:
        logger.warning("No video files found to process")
        return stats
    
    # Display file list
    print_file_list(files)
    
    # Get completed files
    completed_set = get_completed_files(config.csv_log)
    logger.info(f"Found {len(completed_set)} previously completed files")
    
    # Track newly completed files in this session
    session_completed = set()
    
    # Process files
    mode = "CLEANUP" if config.cleanup_only else "PROCESSING"
    print(f"--- Starting {mode} MODE ---\n")
    
    if config.workers > 1 and not config.cleanup_only:
        # Parallel processing
        with ProcessPoolExecutor(max_workers=config.workers) as executor:
            futures = {
                executor.submit(process_single_video, f, config, completed_set): f
                for f in files
            }
            
            with tqdm(total=len(files), unit="file", desc="Total Progress", position=0) as pbar:
                for future in as_completed(futures):
                    result = future.result()
                    log_to_csv(config.csv_log, result, config.dry_run)
                    
                    if result.status == ProcessStatus.SUCCESS:
                        stats.success += 1
                        session_completed.add(result.source_file)
                    elif result.status == ProcessStatus.SKIPPED:
                        stats.skipped += 1
                        session_completed.add(result.source_file)
                    else:
                        stats.error += 1
                        stats.failed_files.append(f"{result.source_file}: {result.message}")
                    
                    # Update status display
                    pbar.set_postfix_str(f"✓ {len(session_completed)}/{len(files)} | ✗ {stats.error}")
                    pbar.update(1)
                    
                    # Print status update every file
                    if len(session_completed) % 1 == 0 or result.status == ProcessStatus.ERROR:
                        print_live_status(files, session_completed, stats)
    else:
        # Sequential processing
        with tqdm(total=len(files), unit="file", desc="Total Progress", position=0) as pbar:
            for file_path in files:
                result = process_single_video(file_path, config, completed_set)
                log_to_csv(config.csv_log, result, config.dry_run)
                
                if result.status == ProcessStatus.SUCCESS:
                    stats.success += 1
                    session_completed.add(result.source_file)
                elif result.status == ProcessStatus.SKIPPED:
                    stats.skipped += 1
                    session_completed.add(result.source_file)
                else:
                    stats.error += 1
                    stats.failed_files.append(f"{result.source_file}: {result.message}")
                
                # Update status display with completed count
                pbar.set_postfix_str(f"✓ {len(session_completed)}/{len(files)} | ✗ {stats.error}")
                pbar.update(1)
                
                # Print status update after each file in sequential mode
                print_live_status(files, session_completed, stats)
    
    # Print summary
    print_summary(stats, start_time)
    
    return stats

# ==============================================================================
# COMMAND LINE INTERFACE
# ==============================================================================

def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description='Video Transcoding and Archival Pipeline',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    parser.add_argument(
        '--source-dir',
        type=str,
        default='/Volumes/Bert/00_deriv/1',
        help='Source directory containing video files'
    )
    
    parser.add_argument(
        '--output-dir',
        type=str,
        default='/Volumes/Bert/00_deriv/1',
        help='Output directory for processed files'
    )
    
    parser.add_argument(
        '--workers',
        type=int,
        default=1,
        help='Number of parallel workers (1 = sequential)'
    )
    
    parser.add_argument(
        '--cleanup-only',
        action='store_true',
        help='Only cleanup temporary files, do not process'
    )
    
    parser.add_argument(
        '--move-finished',
        action='store_true',
        default=True,
        help='Move source files to finished_sources after processing'
    )
    
    parser.add_argument(
        '--no-move-finished',
        dest='move_finished',
        action='store_false',
        help='Do not move source files after processing'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Simulate processing without making changes'
    )
    
    parser.add_argument(
        '--skip-validation',
        action='store_true',
        help='Skip output file validation (faster but less safe)'
    )
    
    return parser.parse_args()

# ==============================================================================
# ENTRY POINT
# ==============================================================================

def main():
    """Main entry point"""
    args = parse_arguments()
    
    try:
        # Create configuration
        config = Config(args)
        
        # Setup logging
        logger = setup_logging(config.log_dir, config.dry_run)
        
        # Check dependencies
        if not check_dependencies():
            logger.error("Missing required dependencies. Exiting.")
            sys.exit(1)
        
        # Check disk space
        if not config.dry_run:
            check_disk_space(config.output_dir)
        
        # Run processing
        logger.info("Starting video transcoding pipeline")
        stats = process_batch(config)
        
        # Exit with appropriate code
        if stats.error > 0:
            sys.exit(1)
        
    except KeyboardInterrupt:
        print("\n\nProcessing interrupted by user")
        sys.exit(130)
    except Exception as e:
        logging.getLogger('video_transcoder').critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
