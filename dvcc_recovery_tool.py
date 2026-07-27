#!/usr/bin/env python3

import os
import glob
import subprocess
import shutil
import sys
import datetime
import logging

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def setup_logger(log_enabled):
    logger = logging.getLogger("DaVinciRecover")
    logger.setLevel(logging.DEBUG)
    logger.handlers = []

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    logger.addHandler(ch)

    if log_enabled:
        log_timestamp = datetime.datetime.now().strftime("%m%d%Y-%H%M%S")
        fh = logging.FileHandler(f"DaVinciRecover_{log_timestamp}.log")
        fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger

def detect_codec(file_path):
    if os.path.getsize(file_path) <= 8192:
        return 'empty'

    with open(file_path, 'rb') as f:
        f.seek(8192)
        header = f.read(8)

    if header[4:8] == b'icpf':
        return 'prores'
    elif header.startswith(b'\x00\x00\x02\x80'):
        # Note: FFmpeg handles DNxHR using the 'dnxhd' codec
        return 'dnxhd'
    elif header == b'\x00\x00\x00\x00\x00\x00\x00\x00':
        return 'blank'
    else:
        return 'unknown'

def scan_for_sessions(base_dir, recursive):
    sessions = {}
    if recursive:
        for root, dirs, files in os.walk(base_dir):
            count = sum(1 for f in files if f.endswith('.dvcc'))
            if count > 0:
                sessions[root] = count
    else:
        try:
            count = sum(1 for f in os.listdir(base_dir) if f.endswith('.dvcc'))
            if count > 0:
                sessions[base_dir] = count
        except FileNotFoundError:
            pass
    return sessions

def process_session(session_dir, output_dir, format_choice, framerate, force_raw, raw_res, raw_fmt, logger):
    session_name = os.path.basename(os.path.abspath(session_dir))
    if not session_name:
        session_name = "Root_Session"

    # Fix Race Condition: Generate timestamp per session
    timestamp = datetime.datetime.now().strftime("%m%d%Y-%H%M%S")

    if format_choice == 'dpx':
        output_file = os.path.join(output_dir, f"Recovered_{session_name}_{timestamp}_%06d.dpx")
        display_file = os.path.join(output_dir, f"Recovered_{session_name}_{timestamp}_[SEQUENCE].dpx")
    else:
        output_file = os.path.join(output_dir, f"Recovered_{session_name}_{timestamp}.{format_choice}")
        display_file = output_file

    logger.info(f"\n{'='*50}")
    logger.info(f"PROCESSING SESSION: {session_name}")
    logger.info(f"{'='*50}")

    search_pattern = os.path.join(session_dir, '*.dvcc')
    files = sorted(glob.glob(search_pattern))

    codec = 'unknown'
    for f in files[:10]:
        detected = detect_codec(f)
        if detected in ['prores', 'dnxhd']:
            codec = detected
            break

    if codec == 'unknown':
        mb_size = os.path.getsize(files[0]) / (1024 * 1024)
        logger.warning(f"File size: ~{mb_size:.2f} MB per frame. Lacks standard ProRes/DNxHR headers.")

        if not force_raw:
            logger.warning("-> SKIPPING FOLDER (Force Raw is OFF).")
            return False
        else:
            logger.warning(f"-> FORCE RAW ENABLED: Forcing extraction at {raw_res} using '{raw_fmt}'.")
            codec = 'rawvideo'
    else:
        logger.info(f"Source Codec detected: {codec.upper()}")

    temp_dir = os.path.join(output_dir, "_temp_dvcc_working")
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir, exist_ok=True)

    logger.info(f"Extracting {len(files)} frames to temp directory...")

    try:
        for i, file_path in enumerate(files):
            temp_filename = f"frame_{i:06d}.raw"
            temp_filepath = os.path.join(temp_dir, temp_filename)

            if detect_codec(file_path) == 'empty':
                continue

            with open(file_path, 'rb') as fin, open(temp_filepath, 'wb') as fout:
                fin.seek(8192)
                if codec == 'prores':
                    size_bytes = fin.read(4)
                    fin.seek(8192)
                    frame_size = int.from_bytes(size_bytes, byteorder='big')
                    fout.write(fin.read(frame_size))
                else:
                    fout.write(fin.read())

            if (i + 1) % 1000 == 0:
                logger.info(f"  Extracted {i + 1} / {len(files)} frames...")
    except Exception as e:
        logger.error(f"Error during extraction: {e}")
        shutil.rmtree(temp_dir)
        return False

    logger.info(f"\nStitching video with FFmpeg...")
    sequence_pattern = os.path.join(temp_dir, "frame_%06d.raw")

    cmd = [
        'ffmpeg', '-y',
        '-framerate', str(framerate)
    ]

    # CODEC MAPPING
    if codec == 'rawvideo':
        fmt_lower = raw_fmt.lower()
        # Check if the user inputted a known video codec instead of a raw pixel format
        if fmt_lower in ['v210', 'r210', 'cfhd']:
            cmd.extend([
                '-f', 'image2',
                '-vcodec', fmt_lower,
                '-s', raw_res,
                '-i', sequence_pattern
            ])
        else:
            cmd.extend([
                '-f', 'image2',
                '-vcodec', 'rawvideo',
                '-s', raw_res,
                '-pix_fmt', fmt_lower,
                '-i', sequence_pattern
            ])
    else:
        cmd.extend([
            '-f', 'image2',
            '-vcodec', codec,
            '-i', sequence_pattern
        ])

    # OUTPUT ENCODING
    if format_choice == 'mp4':
        cmd.extend(['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '16'])
    elif format_choice == 'prores':
        cmd.extend(['-c:v', 'prores_ks', '-profile:v', '3', '-pix_fmt', 'yuv422p10le'])
    elif format_choice == 'dpx':
        cmd.extend(['-c:v', 'dpx'])

    cmd.append(output_file)
    logger.debug(f"FFmpeg command: {' '.join(cmd)}")

    success = False
    try:
        if logger.handlers and any(isinstance(h, logging.FileHandler) for h in logger.handlers):
            process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            logger.debug(process.stdout)
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, cmd)
        else:
            subprocess.run(cmd, check=True)

        logger.info(f"Success! Saved to: {display_file}")
        success = True
    except subprocess.CalledProcessError:
        logger.error("Error: FFmpeg encountered an issue during encoding.")
    except FileNotFoundError:
        logger.error("Error: FFmpeg not found. Ensure it is installed and in your system PATH.")
    finally:
        logger.info("Cleaning up session temporary files...")
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

    return success

def main():
    input_dir = "."
    output_dir = "."
    recursive_mode = True
    format_choice = "mp4"
    formats = ['mp4', 'prores', 'dpx']
    framerate = "24"
    log_enabled = True

    force_raw = False
    raw_res = "3840x2160"
    raw_fmt = "r210"

    while True:
        clear_screen()

        sessions = scan_for_sessions(input_dir, recursive_mode)
        total_sessions = len(sessions)
        total_frames = sum(sessions.values())

        print(".oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.\n")
        print("    DAVINCI DVCC RECOVERY TOOL v1.0 by SKETCHY.DOG LLC   \n")
        print(".oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.\n\n")
        print(f" 1. Input Dir      : {os.path.abspath(input_dir)}")
        print(f"                     Found: {total_sessions} sessions ({total_frames} frames)")
        print(f" 2. Output Dir     : {os.path.abspath(output_dir)}")
        print(f" 3. Recursive Scan : {'[ON] ' if recursive_mode else '[OFF]'}")
        print(f" 4. Output Format  : {format_choice.upper()}")
        print(f" 5. Framerate      : {framerate} fps")
        print(f" 6. Log File       : {'[ON] ' if log_enabled else '[OFF]'}")
        #print("-----------------------------------------------------")
        print(f" 7. Unknown Files  : {'[FORCE RAW]' if force_raw else '[SKIP]'}")
        if force_raw:
            print(f" 8. Raw Resolution : {raw_res}")
            print(f" 9. Raw Fmt/Codec  : {raw_fmt}")
            print(f"    (Uncomp 10b YUV = v210, Uncomp 10b RGB = r210)")
            print(f"    (Uncomp 16b Float = gbrpf32le, Cineform = cfhd)")
        print("\n<<>><<>><<>><<>><<>><<>><<>><<>><<>><<>><<>><<>>\n")
        #print("\n.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.\n")
        print(" R. [R]ecover All Found Sessions")
        print(" Q. [Q]uit")
        print("\n<<>><<>><<>><<>><<>><<>><<>><<>><<>><<>><<>><<>>\n")
        #print("\n.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.\n")

        choice = input("Select an option: ").strip().lower()

        if choice == '1':
            new_dir = input("Enter path to Cache/CacheClip folder (or Enter for current): ").strip()
            if new_dir and os.path.isdir(new_dir):
                input_dir = new_dir
        elif choice == '2':
            new_dir = input("Enter output folder path (or Enter for current): ").strip()
            if new_dir:
                os.makedirs(new_dir, exist_ok=True)
                output_dir = new_dir
        elif choice == '3':
            recursive_mode = not recursive_mode
        elif choice == '4':
            current_idx = formats.index(format_choice)
            format_choice = formats[(current_idx + 1) % len(formats)]
        elif choice == '5':
            new_fps = input(f"Enter framerate (current: {framerate}): ").strip()
            if new_fps.isdigit() or new_fps.replace('.', '', 1).isdigit():
                framerate = new_fps
        elif choice == '6':
            log_enabled = not log_enabled
        elif choice == '7':
            force_raw = not force_raw
        elif choice == '8' and force_raw:
            new_res = input(f"Enter Raw Resolution (Typically 1920x1080, 3840x2160, 4096x2160): ").strip()
            if new_res and 'x' in new_res:
                raw_res = new_res
        elif choice == '9' and force_raw:
            new_fmt = input(f"Enter Raw Format/Codec (Can be: r210, v210, cfhd, bgra, or others): ").strip()
            if new_fmt:
                raw_fmt = new_fmt
        elif choice == 'r':
            if total_sessions == 0:
                input("No .dvcc sessions found to recover! Press Enter to continue...")
                continue

            logger = setup_logger(log_enabled)
            clear_screen()
            print(".oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.")
            print("                   PROCESSING RECOVERY                   ")
            print(".oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.oOo.\n")

            os.makedirs(output_dir, exist_ok=True)

            success_count = 0
            for session_dir in sessions.keys():
                if process_session(session_dir, output_dir, format_choice, framerate, force_raw, raw_res, raw_fmt, logger):
                    success_count += 1

            logger.info(f"\n{'='*50}")
            logger.info(f"BATCH COMPLETE: Successfully recovered {success_count} / {total_sessions} sessions.")
            logger.info(f"{'='*50}")
            input("\nPress Enter to return to menu...")

        elif choice == 'q':
            clear_screen()
            sys.exit(0)

if __name__ == "__main__":
    main()
