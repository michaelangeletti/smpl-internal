import os
import csv
import hashlib
import subprocess
import datetime
import glob
import shutil
import sys

# --- USER VARIABLES (Modified for Ubuntu) ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__)) 
SOURCE_PARENT = os.path.join(BASE_DIR, "Source")

# On Ubuntu, external drives are usually mounted in /media/[user]/[drive_name] or /mnt
# Update this path to match your specific mount point
MKV_OUT_DIR = "/media/smpl-5220r/B/mkv" 

DOCS_DIR = os.path.join(BASE_DIR, "Documents")
MC_DIR = os.path.join(BASE_DIR, "MediaConch")
MC_POLICY = os.path.join(MC_DIR, "DPX_SMPTE-CORE.xml")

LOG_FILE_PATH = ""

def write_log(message, print_to_screen=True):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    if print_to_screen:
        print(log_entry)
    if LOG_FILE_PATH:
        with open(LOG_FILE_PATH, "a") as f:
            f.write(log_entry + "\n")

def is_step_complete(step_marker):
    """Checks the log file to see if a specific step was already successful."""
    if not os.path.exists(LOG_FILE_PATH):
        return False
    search_string = f">>> SUCCESS: {step_marker} completed successfully."
    try:
        with open(LOG_FILE_PATH, "r") as f:
            log_content = f.read()
        return search_string in log_content
    except Exception:
        return False

def mark_step_complete(step_num):
    """Explicitly marks a step as complete in the log."""
    marker = f"Step {step_num}"
    write_log(f"\n>>> SUCCESS: {marker} completed successfully.")

def write_banner(title):
    divider = "=" * 60
    write_log(f"\n{divider}")
    write_log(f" STEP: {title}")
    write_log(f"{divider}\n")

def init_log_header():
    if not os.path.exists(LOG_FILE_PATH):
        header = (
            "============================================================\n"
            "Stanford Media Preservation Lab\n"
            "RAWcooked automation script, v1.2, 2026\n"
            "DPX --> FFv1/mkv\n"
            "============================================================\n\n"
        )
        with open(LOG_FILE_PATH, "w") as f:
            f.write(header)
    else:
        # Log is resuming from a previous run
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        resume_note = (
            "\n\n"
            "============================================================\n"
            f"SCRIPT RESUMED: {timestamp}\n"
            "Continuing from previous attempt after error/interruption\n"
            "============================================================\n\n"
        )
        with open(LOG_FILE_PATH, "a") as f:
            f.write(resume_note)

def verify_md5(file_path, md5_path):
    file_name = os.path.basename(file_path)
    try:
        with open(md5_path, 'r') as f:
            expected = f.read().split()[0].lower()
        sha = hashlib.md5()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha.update(chunk)
        actual = sha.hexdigest()
        
        if expected == actual:
            write_log(f"  [PASS] {file_name}")
            return True
        else:
            write_log(f"  [FAIL] !!! {file_name} (Mismatch!)")
            return False
    except Exception as e:
        write_log(f"  [ERROR] {file_name}: {str(e)}")
        return False

def run_step_verbose(step_num, step_name, command, parse_rawcooked=False):
    """Run a command with verbose output, checking if already complete first."""
    marker = f"Step {step_num}"
    if is_step_complete(marker):
        write_log(f"Step {step_num} ({step_name}) already completed. Skipping...")
        return True

    write_banner(f"{step_num}. {step_name}")
    try:
        # Using executable='/bin/bash' ensures compatibility on Ubuntu
        process = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, executable='/bin/bash'
        )
        output_lines = []
        
        # Track RAWcooked phases if requested
        current_phase = None
        phase_markers = {
            'Reversibility data': '5a',
            'Encoding': '5b', 
            'Reversibility check': '5c'
        }
        
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            
            # Check if we're entering a new RAWcooked phase
            if parse_rawcooked:
                for keyword, phase in phase_markers.items():
                    if keyword in line and current_phase != phase:
                        current_phase = phase
                        phase_name = {
                            '5a': 'DPX FILE ANALYSIS',
                            '5b': 'RAWCOOKED LOSSLESS ENCODING',
                            '5c': 'RAWCOOKED REVERSIBILITY CHECK'
                        }[phase]
                        divider = f"\n--- {phase_name} ---\n"
                        output_lines.append(divider)
                        sys.stdout.write(divider)
                        sys.stdout.flush()
                        break
            
            output_lines.append(line)
        
        process.wait()
        
        if LOG_FILE_PATH:
            with open(LOG_FILE_PATH, "a") as f:
                f.writelines(output_lines)
        
        if process.returncode == 0:
            mark_step_complete(step_num)
            return True
        else:
            write_log(f"!!! ERROR: Step {step_num} failed with return code {process.returncode}")
            return False
    except Exception as e:
        write_log(f"!!! EXCEPTION in Step {step_num}: {e}")
        return False

def process_sequence(source_folder_path):
    global LOG_FILE_PATH
    
    source_folder_name = os.path.basename(source_folder_path.rstrip(os.sep))
    LOG_FILE_PATH = os.path.join(DOCS_DIR, f"{source_folder_name}_process.log")
    
    init_log_header()
    write_log(f"PROCESSING FOLDER: {source_folder_name}")

    # Define output paths upfront
    csv_path = os.path.join(DOCS_DIR, f"{source_folder_name}_inventory.csv")
    long_md5_path = os.path.join(DOCS_DIR, f"{source_folder_name}.md5")
    rc_log_path = os.path.join(DOCS_DIR, f"{source_folder_name}.log")
    custom_xml = os.path.join(DOCS_DIR, f"{source_folder_name}.xml")
    mkv_path = os.path.join(MKV_OUT_DIR, f"{source_folder_name}.mkv")
    mp4_out = os.path.join(MKV_OUT_DIR, f"{source_folder_name}_rawcooked_review.mp4")

    # 1. Inventory
    if is_step_complete("Step 1"):
        write_log("Step 1 (Inventory) already completed. Skipping...")
    else:
        write_banner("1. INVENTORY GENERATION")
        dpx_count = 0
        total_bytes = 0
        try:
            with open(csv_path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['path', 'file name', 'extension', 'file size (MB)', 'file size (bytes)'])
                for root, dirs, files in os.walk(source_folder_path):
                    for file in sorted(files):
                        if not file.startswith('.'):
                            f_full_path = os.path.join(root, file)
                            ext = os.path.splitext(file)[1].lower()
                            if ext == '.dpx': dpx_count += 1
                            size_bytes = os.path.getsize(f_full_path)
                            total_bytes += size_bytes
                            size_mb = round(size_bytes / 1048576, 2)
                            
                            # For .md5 files, show bytes instead of MB (which would be 0.00)
                            if ext == '.md5':
                                writer.writerow([f_full_path, file, ext, '', size_bytes])
                            else:
                                writer.writerow([f_full_path, file, ext, size_mb, ''])
            total_mb = round(total_bytes / 1048576, 2)
            write_log(f"Inventory complete. Found {dpx_count} DPX files. Total: {total_mb} MB")
            mark_step_complete(1)
        except Exception as e:
            write_log(f"!!! ERROR in Step 1: {e}")
            return "Inventory Generation Error"

    # 2. Source Integrity
    if is_step_complete("Step 2"):
        write_log("Step 2 (Checksums) already completed. Skipping...")
    else:
        write_banner("2. CHECKSUM VERIFICATION")
        try:
            md5_files_to_delete = []
            for root, dirs, files in os.walk(source_folder_path):
                for file in sorted(files):
                    if not file.endswith('.md5') and not file.startswith('.'):
                        f_path = os.path.join(root, file)
                        m_path = f_path + ".md5"
                        if not os.path.exists(m_path):
                            write_log(f"CRITICAL ERROR: Missing MD5 for {file}")
                            return "Missing MD5"
                        if not verify_md5(f_path, m_path):
                            return "Checksum Mismatch"
                        md5_files_to_delete.append(m_path)
            write_log("-" * 30)
            write_log("Deleting sidecar .md5 files...")
            for m_file in md5_files_to_delete:
                os.remove(m_file)
            mark_step_complete(2)
        except Exception as e:
            write_log(f"!!! ERROR in Step 2: {e}")
            return "Checksum Verification Error"

    # 3. MediaConch
    if is_step_complete("Step 3"):
        write_log("Step 3 (MediaConch) already completed. Skipping...")
    else:
        write_banner("3. MEDIACONCH POLICY VALIDATION")
        try:
            dpx_files = sorted(glob.glob(os.path.join(source_folder_path, "**/*.dpx"), recursive=True))
            for dpx in dpx_files:
                f_name = os.path.basename(dpx)
                mc_cmd = f"mediaconch -p '{MC_POLICY}' '{dpx}'"
                res = subprocess.run(mc_cmd, shell=True, capture_output=True, text=True)
                if res.returncode != 0:
                    write_log(f"  [INVALID] {f_name}")
                    return "MediaConch Policy Failure"
                write_log(f"  [VALID] {f_name}")
            mark_step_complete(3)
        except Exception as e:
            write_log(f"!!! ERROR in Step 3: {e}")
            return "MediaConch Validation Error"

    # 4. Long MD5 List
    if is_step_complete("Step 4"):
        write_log("Step 4 (Manifest) already completed. Skipping...")
    else:
        write_banner("4. MANIFEST GENERATION (.md5)")
        try:
            with open(long_md5_path, 'w') as lf:
                for root, dirs, files in os.walk(source_folder_path):
                    for file in sorted(files):
                        if not file.startswith('.') and not file.endswith('.md5'):
                            sha = hashlib.md5()
                            with open(os.path.join(root, file), 'rb') as f:
                                for chunk in iter(lambda: f.read(8192), b""): sha.update(chunk)
                            lf.write(f"{sha.hexdigest()}  {file}\n")
            mark_step_complete(4)
        except Exception as e:
            write_log(f"!!! ERROR in Step 4: {e}")
            return "Manifest Generation Error"

    # 5. RAWcooked
    rc_cmd = f"rawcooked -y --all --log-name '{rc_log_path}' '{source_folder_path}' -o '{mkv_path}'"
    
    if not run_step_verbose(5, "RAWCOOKED TRANSCODE", rc_cmd, parse_rawcooked=True): 
        return "RAWcooked Encoding Error"
    
    # Check RAWcooked log for issues (only if step just completed)
    if os.path.exists(rc_log_path):
        with open(rc_log_path, 'r') as rcl:
            for line in rcl:
                if "?" in line or "reversibility check failed" in line.lower():
                    write_log(f"!!! NOTE: RAWcooked bypassed a prompt: {line.strip()}")

    # 6. Metadata Tags
    if not os.path.exists(custom_xml):
        write_log(f"CRITICAL: Missing '{source_folder_name}.xml'")
        return "Missing Required XML Metadata"
    
    if not run_step_verbose(6, "EMBED METADATA TAGS", f"mkvpropedit '{mkv_path}' --tags all:'{custom_xml}'"):
        return "mkvpropedit Tagging Error"
    
    # 7. Attachments
    if not run_step_verbose(7, "EMBED ATTACHMENTS", f"mkvpropedit '{mkv_path}' --add-attachment '{rc_log_path}' --add-attachment '{long_md5_path}'"):
        return "Attachment Error"
    
    # 8. FFmpeg Review Copy
    if not run_step_verbose(8, "GENERATE REVIEW DERIVATIVE", f"ffmpeg -i '{mkv_path}' -crf 18 -vf 'scale=-2:720' -pix_fmt yuv420p '{mp4_out}'"):
        return "FFmpeg Derivative Error"
    
    # 9. Final MKV MD5
    if is_step_complete("Step 9"):
        write_log("Step 9 (Final Hash) already completed. Skipping...")
    else:
        write_banner("9. FINAL DELIVERABLE HASH")
        try:
            sha = hashlib.md5()
            with open(mkv_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b""): sha.update(chunk)
            with open(mkv_path + ".md5", 'w') as f:
                f.write(f"{sha.hexdigest()}  {os.path.basename(mkv_path)}\n")
            mark_step_complete(9)
        except Exception as e:
            write_log(f"!!! ERROR in Step 9: {e}")
            return "Final Hash Generation Error"

    write_log(f"\n{'='*60}")
    write_log(f"✓ COMPLETED PROCESS: {source_folder_name}")
    write_log(f"{'='*60}\n")
    return True

def main():
    if not os.path.exists(DOCS_DIR): os.makedirs(DOCS_DIR)
    if not os.path.exists(MKV_OUT_DIR): os.makedirs(MKV_OUT_DIR, exist_ok=True)
    if not shutil.which("mkvpropedit"):
        print("FATAL ERROR: mkvpropedit not installed. Please run: sudo apt install mkvtoolnix")
        return

    all_sequences = sorted([os.path.join(SOURCE_PARENT, d) for d in os.listdir(SOURCE_PARENT) 
                           if os.path.isdir(os.path.join(SOURCE_PARENT, d)) and not d.startswith('.')])
    
    if not all_sequences:
        print(f"No folders found in {SOURCE_PARENT}")
        return

    print("="*60)
    print("Stanford Media Preservation Lab - RAWcooked Workflow (Ubuntu)")
    print("Resumable Mode: Any failed step can be resumed")
    print("="*60)
    
    success_list, error_list = [], []

    for seq in all_sequences:
        folder_name = os.path.basename(seq.rstrip(os.sep))
        print(f"\n>>> Processing: {folder_name}")
        result = process_sequence(seq)
        if result is True:
            success_list.append(folder_name)
        else:
            error_list.append((folder_name, result))
            print(f"\n!!! Processing halted at: {folder_name}")
            print(f"!!! Reason: {result}")
            print(f"!!! To resume: Simply run this script again")
            break

    print("\n" + "="*60)
    print("BATCH PROCESSING SUMMARY")
    print("="*60)
    print(f"Total Sequences: {len(all_sequences)}")
    print(f"Successfully Completed: {len(success_list)}")
    
    if success_list:
        print("\nCompleted:")
        for s in success_list:
            print(f"  ✓ {s}")
    
    if error_list:
        print(f"\nErrors/Halted: {len(error_list)}")
        for name, err in error_list:
            print(f"  ✗ {name}: {err}")
        print("\nTo resume: Fix the issue and run the script again.")
        print("Already-completed steps will be automatically skipped.")
    
    print("="*60 + "\n")

if __name__ == "__main__":
    main()
