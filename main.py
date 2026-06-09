import os
import hashlib
import json
import threading
import tempfile
import unicodedata
import subprocess
import shutil
import time
import re
from flask import Flask, render_template, request, send_file, jsonify, Response
from gtts import gTTS
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Cấu hình thư mục
DATA_FOLDER = 'data'
CACHE_FOLDER = 'cache'

# Tạo folder nếu chưa có
os.makedirs(DATA_FOLDER, exist_ok=True)
os.makedirs(CACHE_FOLDER, exist_ok=True)

# Detect ffmpeg availability once at startup and choose cache extension accordingly
FFMPEG_AVAILABLE = shutil.which('ffmpeg') is not None


def get_audio_filename(text):
    """Tạo tên file dựa trên nội dung text (MD5 hash).
    If ffmpeg is available we use WAV cache, otherwise fall back to MP3.
    """
    ext = '.wav' if FFMPEG_AVAILABLE else '.mp3'
    hash_object = hashlib.md5(text.encode())
    return os.path.join(CACHE_FOLDER, hash_object.hexdigest() + ext)

# In-memory cache for preloaded text contents (key: file_path -> list of lines)
PRELOAD_CACHE = {}
PRELOAD_LOCK = threading.Lock()


def prune_cache(max_files: int = 200):
    """Chỉ lưu tồn max_files tập tin âm thanh tân cận nhất trong thư mục đệm.
    Phàm các tập tin định dạng .wav cùng .mp3, nếu vượt hạn, tất bị loại trừ khi cần.
    Quá trình xóa nếu phát sinh lỗi nơi từng tập tin riêng lẻ thì nhất loạt vô thị, bởi khả năng tập tin đã bị dời bỏ đồng thời bởi tiến trình khác.
    """
    try:
        # Bao hàm cả .wav lẫn .mp3 nhằm bảo toàn chu toàn, phòng khi sơ suất
        files = [os.path.join(CACHE_FOLDER, f) for f in os.listdir(CACHE_FOLDER)
                 if f.lower().endswith('.wav') or f.lower().endswith('.mp3')]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in files[max_files:]:
            try:
                os.remove(old)
                print(f"[prune_cache] removed old cache: {old}")
            except FileNotFoundError:
                # Tập tin khả năng dĩ bị tha tuyến trình hoặc tiến trình khác trừ khứ; nhất luật vô thị, bất tất xử lý.
                continue
            except Exception as e:
                print(f"[prune_cache] failed to remove {old}: {e}")
    except Exception as e:
        print(f"[prune_cache] error while pruning cache: {e}")

# Các phần mở rộng tệp được phép tải lên
ALLOWED_EXTENSIONS = {'txt'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _extract_first_number(s):
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def _numeric_sort_key(name):
    # return tuple for sorting: (has_number, number_or_large, original_lower)
    num = _extract_first_number(name)
    if num is None:
        return (1, name.lower())
    return (0, num, name.lower())


def _is_meaningful_line(s: str) -> bool:
    """Trả về True nếu dòng có ít nhất một ký tự chữ hoặc số.
    Điều này dùng để lọc bỏ các dòng chỉ gồm dấu câu như "...." hoặc các ký hiệu tương tự, vì chúng có thể làm TTS bị lỗi."""
    if not s:
        return False
    # Có chứa ít nhất một ký tự chữ, chữ số hoặc dấu gạch dưới (_) theo chuẩn Unicode.
    return bool(re.search(r"\w", s))


def sanitize_text(text: str) -> str:
    """Chuẩn hóa và làm sạch văn bản để tránh các ký tự đôi khi gây lỗi cho gTTS.
    Thao tác này sẽ thay thế các dấu ngoặc kép cong, dấu gạch nối, dấu ba chấm thường gặp và loại bỏ các ký tự điều khiển.
    """
    if not text:
        return text
    t = unicodedata.normalize('NFKC', text)
    replacements = {
        '\u2018': "'", '\u2019': "'",
        '\u201c': '"', '\u201d': '"',
        '\u2013': '-', '\u2014': '-', '\u2026': '...'
    }
    for k, v in replacements.items():
        t = t.replace(k, v)
    # remove C0 control chars except newline/tab
    t = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F]', '', t)
    # collapse whitespace
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def sanitize_folder_name(folder_name: str) -> str:
    """Sanitize folder name while preserving Vietnamese characters and spaces.
    Remove only filesystem-dangerous characters: < > : " / \ | ? *
    """
    if not folder_name:
        return 'unnamed'
    # Remove only dangerous filesystem characters
    folder_name = re.sub(r'[<>:"/\\|?*]', '', folder_name)
    # Remove leading/trailing whitespace and dots
    folder_name = folder_name.strip('. ')
    # Collapse multiple spaces
    folder_name = re.sub(r'\s+', ' ', folder_name)
    # Return or fallback to 'unnamed' if result is empty
    return folder_name if folder_name else 'unnamed'


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/taoscript')
def taoscript():
    return render_template('taoscript.html')

@app.route('/upload_split')
def upload_split_page():
    return render_template('upload_split.html')

@app.route('/indext.css')
def css():
    return render_template('index.css')


@app.route('/api/list_files')
def list_files():
    """Liệt kê các folder và file txt (top-level folders, sorted)."""
    structure = {}
    try:
        for folder_name in sorted(os.listdir(DATA_FOLDER)):
            folder_path = os.path.join(DATA_FOLDER, folder_name)
            if not os.path.isdir(folder_path):
                continue
            txt_files = [f for f in os.listdir(folder_path) if f.endswith('.txt')]
            # numeric-aware sort
            txt_files = sorted(txt_files, key=_numeric_sort_key)
            if txt_files:
                structure[folder_name] = txt_files
    except FileNotFoundError:
        pass
    return jsonify(structure)


@app.route('/api/get_text')
def get_text():
    """Đọc file txt và chia thành mảng các đoạn text."""
    folder = request.args.get('folder')
    filename = request.args.get('file')
    # optional: index of current line the client has played (zero-based)
    # if provided and >= len(lines) the server will return the next file automatically
    line_index = request.args.get('line_index', None)
    try:
        line_index = int(line_index) if line_index is not None else None
    except ValueError:
        line_index = None

    # optional: playback speed (echoed back so client keeps it)
    playback_speed = request.args.get('playback_speed', None)
    file_path = os.path.join(DATA_FOLDER, folder, filename)

    if not os.path.exists(file_path):
        # if file missing, try to auto-advance to next file
        nf, nf_file = get_next_file_in_structure(folder, filename)
        if not nf:
            return jsonify({"error": "File not found and no next file"}), 404
        next_path = os.path.join(DATA_FOLDER, nf, nf_file)
        next_lines = []
        with open(next_path, 'r', encoding='utf-8') as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                if not _is_meaningful_line(s):
                    continue
                next_lines.append(s)
        return jsonify({"lines": next_lines, "folder": nf, "file": nf_file, "next_folder": None, "next_file": None, "playback_speed": playback_speed, "padding_ms": 50})

    # Nếu đã preload, trả về cache
    with PRELOAD_LOCK:
        if file_path in PRELOAD_CACHE:
            lines = PRELOAD_CACHE[file_path]
        else:
            lines = []
            with open(file_path, 'r', encoding='utf-8') as f:
                # Đọc file, tách dòng, bỏ dòng trống và bỏ các đoạn vô nghĩa
                for line in f:
                    s = line.strip().replace("“", " ").replace("”", " ").replace("‘", " ").replace("’", " ")
                    if not s:
                        continue
                    if not _is_meaningful_line(s):
                        continue
                    lines.append(s)

    # If client says it's already past the end of this file, return next file automatically
    if line_index is not None and line_index >= len(lines):
        nf, nf_file = get_next_file_in_structure(folder, filename)
        if not nf:
            # nothing next -> return empty with metadata
            return jsonify({"lines": [], "folder": folder, "file": filename, "next_folder": None, "next_file": None, "playback_speed": playback_speed, "padding_ms": 50})
        next_path = os.path.join(DATA_FOLDER, nf, nf_file)
        next_lines = []
        with open(next_path, 'r', encoding='utf-8') as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                if not _is_meaningful_line(s):
                    continue
                next_lines.append(s)
        # also return info about further-next file
        nnf, nnf_file = get_next_file_in_structure(nf, nf_file)
        return jsonify({"lines": next_lines, "folder": nf, "file": nf_file, "next_folder": nnf, "next_file": nnf_file, "playback_speed": playback_speed, "padding_ms": 50})

    # normal return: include metadata (next file hint, playback speed, padding recommendation)
    nf, nf_file = get_next_file_in_structure(folder, filename)
    return jsonify({"lines": lines, "folder": folder, "file": filename, "next_folder": nf, "next_file": nf_file, "playback_speed": playback_speed, "padding_ms": 50})


def get_next_file_in_structure(folder, filename):
    """Return (next_folder, next_filename) or (None, None) if none."""
    folders = []
    try:
        for folder_name in sorted(os.listdir(DATA_FOLDER)):
            folder_path = os.path.join(DATA_FOLDER, folder_name)
            if not os.path.isdir(folder_path):
                continue
            txt_files = [f for f in os.listdir(folder_path) if f.endswith('.txt')]
            txt_files = sorted(txt_files, key=_numeric_sort_key)
            if txt_files:
                folders.append((folder_name, txt_files))
    except FileNotFoundError:
        return (None, None)

    # Find current folder index
    for fi, (f_name, f_list) in enumerate(folders):
        if f_name == folder:
            # find file in list
            try:
                idx = f_list.index(filename)
            except ValueError:
                return (None, None)
            # next file in same folder
            if idx + 1 < len(f_list):
                return (f_name, f_list[idx+1])
            # otherwise move to first file of next folder
            if fi + 1 < len(folders):
                return (folders[fi+1][0], folders[fi+1][1][0])
            # no next
            return (None, None)
    return (None, None)


@app.route('/api/next_file')
def next_file():
    """Return the next text file after the given folder/file.

    Query params: folder, file
    Response: {"next_folder": str or null, "next_file": str or null}
    """
    folder = request.args.get('folder')
    filename = request.args.get('file')
    if not folder or not filename:
        return jsonify({"error": "missing folder or file parameter"}), 400

    nf, nf_file = get_next_file_in_structure(folder, filename)
    return jsonify({"next_folder": nf, "next_file": nf_file})


@app.route('/api/preload')
def preload():
    """Preload a text file and stream progress via Server-Sent Events (SSE)."""
    folder = request.args.get('folder')
    filename = request.args.get('file')
    file_path = os.path.join(DATA_FOLDER, folder, filename)

    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404

    def generate():
        try:
            # Count total meaningful lines first
            with open(file_path, 'r', encoding='utf-8') as fcount:
                total = 0
                for l in fcount:
                    s = l.strip()
                    if not s:
                        continue
                    if not _is_meaningful_line(s):
                        continue
                    total += 1

            # Read and cache progressively
            loaded = 0
            lines_acc = []
            chunk = 0
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    if not _is_meaningful_line(s):
                        # skip punctuation-only or meaningless lines
                        continue
                    lines_acc.append(s)
                    loaded += 1
                    chunk += 1

                    # Send update every 10 lines or on completion
                    if chunk >= 10 or loaded == total:
                        chunk = 0
                        progress = int((loaded / total) * 100) if total else 100
                        data = {"progress": progress, "loaded": loaded, "total": total}
                        # also print to server log for debugging
                        print(f"[preload] {filename}: {loaded}/{total} ({progress}%)")
                        yield f"data: {json.dumps(data)}\n\n"
                        # small pause so client can render progress visibly for large files
                        time.sleep(0.01)

            # Store into preload cache
            with PRELOAD_LOCK:
                PRELOAD_CACHE[file_path] = lines_acc
            print(f"[preload] completed cache for {filename}: {len(lines_acc)} lines")

            # Notify completion
            yield f"data: {json.dumps({'progress': 100, 'status': 'done', 'loaded': loaded, 'total': total})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'progress': 0, 'status': 'error', 'message': str(e)})}\n\n"

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/tts')
def tts():
    """Chuyển text thành audio hoặc lấy từ cache."""
    text = request.args.get('text')
    if not text:
        return "No text", 400
    # sanitize input to avoid problematic characters
    text = sanitize_text(text)
    if not text:
        return "No text after sanitization", 400

    # Kiểm tra cache
    output_file = get_audio_filename(text)

    if not os.path.exists(output_file):
        print(f"Generating new audio for: {text[:40]}...")
        success = False
        last_err = None
        # Try a few times because transient network/remote errors sometimes return invalid content
        for attempt in range(1, 4):
            tmp_mp3 = None
            try:
                tts_obj = gTTS(text=text, lang='vi')
                # create a temp mp3 file and let gTTS write into it
                fd, tmp_mp3 = tempfile.mkstemp(suffix='.mp3')
                os.close(fd)
                try:
                    tts_obj.save(tmp_mp3)
                except Exception:
                    if tmp_mp3 and os.path.exists(tmp_mp3):
                        try: os.remove(tmp_mp3)
                        except Exception: pass
                    raise

                # convert mp3 -> wav using ffmpeg if available, otherwise keep mp3
                if FFMPEG_AVAILABLE:
                    try:
                        # remove any existing output before convert
                        if os.path.exists(output_file):
                            try: os.remove(output_file)
                            except Exception: pass
                        cmd = [
                            'ffmpeg', '-y', '-loglevel', 'error',
                            '-i', tmp_mp3,
                            # set common params for compatibility
                            '-ar', '22050', '-ac', '1', output_file
                        ]
                        subprocess.run(cmd, check=True)
                    except Exception as conv_e:
                        print(f"ffmpeg conversion failed: {conv_e}")
                        # cleanup tmp
                        if os.path.exists(tmp_mp3):
                            try: os.remove(tmp_mp3)
                            except Exception: pass
                        raise
                    finally:
                        try:
                            if os.path.exists(tmp_mp3):
                                os.remove(tmp_mp3)
                        except Exception:
                            pass
                else:
                    # No ffmpeg: move the produced MP3 into the cache path
                    try:
                        # ensure target folder exists
                        os.makedirs(os.path.dirname(output_file), exist_ok=True)
                        os.replace(tmp_mp3, output_file)
                    except Exception:
                        # fallback: try shutil.move
                        try:
                            shutil.move(tmp_mp3, output_file)
                        except Exception as move_e:
                            print(f"Failed to move tmp MP3 into cache: {move_e}")
                            if os.path.exists(tmp_mp3):
                                try: os.remove(tmp_mp3)
                                except Exception: pass
                            raise

                success = True
                # prune cache to keep disk bounded
                try:
                    prune_cache(200)
                except Exception:
                    pass
                break
            except Exception as e:
                last_err = e
                print(f"Error generating TTS (attempt {attempt}): {e}")
                # cleanup tmp if exists
                try:
                    if tmp_mp3 and os.path.exists(tmp_mp3):
                        os.remove(tmp_mp3)
                except Exception:
                    pass
                time.sleep(0.3)
        if not success:
            print(f"TTS failed after retries: {last_err}")
            return ("TTS generation failed", 503)
    else:
        print(f"Serving from cache: {text[:40]}...")

    # Always send full file body (disable conditional 304 responses)
    # Choose proper mimetype based on extension
    mimetype = 'audio/wav' if output_file.lower().endswith('.wav') else 'audio/mpeg'
    resp = send_file(output_file, mimetype=mimetype, conditional=False)
    # Allow client caching but avoid conditional checks that may return 304 without body
    resp.headers['Cache-Control'] = 'public, max-age=31536000'
    return resp


@app.route('/api/tts_file')
def tts_file():
    """Concatenate entire file content into one WAV file."""
    folder = request.args.get('folder', '1')
    filename = request.args.get('file')
    
    if not filename:
        return jsonify({"error": "Missing file parameter"}), 400
    
    # Get file path
    file_path = os.path.join(DATA_FOLDER, folder, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404
    
    # Read lines
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        lines = [line.strip() for line in content.split('\n') if line.strip()]
    except Exception as e:
        print(f"[tts_file] Error reading file: {e}")
        return jsonify({"error": "Failed to read file"}), 500
    
    if not lines:
        return jsonify({"error": "File is empty"}), 400
    
    # Create content hash for caching
    content_hash = hashlib.md5(content.encode()).hexdigest()
    output_file = os.path.join(CACHE_FOLDER, f"{content_hash}.wav")
    metadata_file = os.path.join(CACHE_FOLDER, f"{content_hash}.json")
    
    # Try loading from cache
    offsets = None
    if os.path.exists(output_file) and os.path.exists(metadata_file):
        try:
            with open(metadata_file, 'r') as f:
                metadata = json.load(f)
            offsets = metadata.get('offsets')
            print(f"[tts_file] Cache hit: {filename} (hash: {content_hash})")
        except Exception as e:
            print(f"[tts_file] Metadata error: {e}, regenerating")
            offsets = None
    
    # Generate if not cached
    if offsets is None:
        print(f"[tts_file] Generating audio: {filename} ({len(lines)} lines)")
        
        temp_files = []
        durations = []
        
        for i, line in enumerate(lines):
            text = sanitize_text(line)
            if not text:
                durations.append(0.0)
                continue
            
            try:
                tts_obj = gTTS(text=text, lang='vi')
                fd, tmp_mp3 = tempfile.mkstemp(suffix='.mp3')
                os.close(fd)
                tts_obj.save(tmp_mp3)
                
                if FFMPEG_AVAILABLE:
                    fd2, tmp_wav = tempfile.mkstemp(suffix='.wav')
                    os.close(fd2)
                    subprocess.run(
                        ['ffmpeg', '-y', '-i', tmp_mp3, '-ar', '22050', '-ac', '1', tmp_wav],
                        check=True, capture_output=True
                    )
                    os.remove(tmp_mp3)
                    temp_files.append(tmp_wav)
                    
                    # Get duration
                    result = subprocess.run(
                        ['ffprobe', '-i', tmp_wav, '-show_entries', 'format=duration', '-v', 'quiet', '-of', 'csv=p=0'],
                        capture_output=True, text=True
                    )
                    try:
                        duration = float(result.stdout.strip())
                    except:
                        duration = 1.0
                    durations.append(duration)
                else:
                    temp_files.append(tmp_mp3)
                    durations.append(1.0)
                
                if (i + 1) % 10 == 0 or i == len(lines) - 1:
                    pct = int((i + 1) / len(lines) * 100)
                    print(f"[tts_file] Processed {i+1}/{len(lines)} ({pct}%)")
            except Exception as e:
                print(f"[tts_file] Error on line {i}: {e}")
                durations.append(0.0)
                continue
        
        if not temp_files:
            return jsonify({"error": "Failed to create audio segments"}), 500
        
        # Concatenate
        concat_file = tempfile.mktemp(suffix='.txt')
        with open(concat_file, 'w') as f:
            for tf in temp_files:
                f.write(f"file '{tf}'\n")
        
        try:
            print(f"[tts_file] Concatenating {len(temp_files)} segments")
            subprocess.run(
                ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_file, '-c', 'copy', output_file],
                check=True, capture_output=True
            )
            print(f"[tts_file] Concatenation complete")
        except Exception as e:
            print(f"[tts_file] Concat error: {e}")
            return jsonify({"error": "Concatenation failed"}), 500
        finally:
            for tf in temp_files:
                try:
                    os.remove(tf)
                except:
                    pass
            try:
                os.remove(concat_file)
            except:
                pass
        
        # Save metadata - offsets[i] = start time of line i (even if skipped)
        offsets = []
        cumulative = 0.0
        for d in durations:
            offsets.append(cumulative)
            cumulative += d
        
        # Ensure offsets match line count
        while len(offsets) < len(lines):
            offsets.append(cumulative)
        offsets = offsets[:len(lines)]
        
        metadata = {'offsets': offsets}
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f)
        
        try:
            prune_cache(200)
        except:
            pass
    
    # Return audio with metadata
    try:
        resp = send_file(output_file, mimetype='audio/wav', conditional=False)
        resp.headers['Cache-Control'] = 'public, max-age=31536000'
        resp.headers['X-Offsets'] = json.dumps(offsets)
        return resp
    except Exception as e:
        print(f"[tts_file] Send error: {e}")
        return jsonify({"error": "Failed to send file"}), 500


@app.route('/api/tts_file_chunk')
def tts_file_chunk():
    """Concatenate a chunk (20%) of file content into one WAV file."""
    folder = request.args.get('folder', '1')
    filename = request.args.get('file')
    chunk_num = int(request.args.get('chunk', '0'))
    
    if not filename:
        return jsonify({"error": "Missing file parameter"}), 400
    
    # Get file path
    file_path = os.path.join(DATA_FOLDER, folder, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404
    
    # Read lines
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        lines = [line.strip() for line in content.split('\n') if line.strip()]
    except Exception as e:
        print(f"[tts_file_chunk] Error reading file: {e}")
        return jsonify({"error": "Failed to read file"}), 500
    
    if not lines:
        return jsonify({"error": "File is empty"}), 400
    
    # Calculate chunk boundaries (20% = 1 chunk)
    total_lines = len(lines)
    chunk_size = max(1, total_lines // 5)  # 5 chunks = 20% each
    start_idx = chunk_num * chunk_size
    end_idx = start_idx + chunk_size if chunk_num < 4 else total_lines
    chunk_lines = lines[start_idx:end_idx]
    
    if not chunk_lines:
        return jsonify({"error": "Chunk out of range"}), 400
    
    # Create cache key for this chunk
    content_hash = hashlib.md5(content.encode()).hexdigest()
    chunk_key = f"{content_hash}_chunk{chunk_num}"
    output_file = os.path.join(CACHE_FOLDER, f"{chunk_key}.wav")
    metadata_file = os.path.join(CACHE_FOLDER, f"{chunk_key}.json")
    
    # Try loading from cache
    offsets = None
    if os.path.exists(output_file) and os.path.exists(metadata_file):
        try:
            with open(metadata_file, 'r') as f:
                metadata = json.load(f)
            offsets = metadata.get('offsets')
            print(f"[tts_file_chunk] Cache hit: {filename} chunk {chunk_num} (hash: {chunk_key})")
        except Exception as e:
            print(f"[tts_file_chunk] Metadata error: {e}, regenerating")
            offsets = None
    
    # Generate if not cached
    if offsets is None:
        pct = int((chunk_num / 5) * 100)
        print(f"[tts_file_chunk] Generating audio: {filename} chunk {chunk_num} ({pct}%) - {len(chunk_lines)} lines")
        
        temp_files = []
        durations = []
        
        for i, line in enumerate(chunk_lines):
            text = sanitize_text(line)
            if not text:
                durations.append(0.0)
                continue
            
            try:
                tts_obj = gTTS(text=text, lang='vi')
                fd, tmp_mp3 = tempfile.mkstemp(suffix='.mp3')
                os.close(fd)
                tts_obj.save(tmp_mp3)
                
                if FFMPEG_AVAILABLE:
                    fd2, tmp_wav = tempfile.mkstemp(suffix='.wav')
                    os.close(fd2)
                    subprocess.run(
                        ['ffmpeg', '-y', '-i', tmp_mp3, '-ar', '22050', '-ac', '1', tmp_wav],
                        check=True, capture_output=True
                    )
                    os.remove(tmp_mp3)
                    temp_files.append(tmp_wav)
                    
                    # Get duration
                    result = subprocess.run(
                        ['ffprobe', '-i', tmp_wav, '-show_entries', 'format=duration', '-v', 'quiet', '-of', 'csv=p=0'],
                        capture_output=True, text=True
                    )
                    try:
                        duration = float(result.stdout.strip())
                    except:
                        duration = 1.0
                    durations.append(duration)
                else:
                    temp_files.append(tmp_mp3)
                    durations.append(1.0)
                
                if (i + 1) % 5 == 0 or i == len(chunk_lines) - 1:
                    pct_chunk = int((i + 1) / len(chunk_lines) * 100)
                    print(f"[tts_file_chunk] Chunk {chunk_num}: {i+1}/{len(chunk_lines)} ({pct_chunk}%)")
            except Exception as e:
                print(f"[tts_file_chunk] Error on line {i}: {e}")
                durations.append(0.0)
                continue
        
        if not temp_files:
            return jsonify({"error": "Failed to create audio segments"}), 500
        
        # Concatenate
        concat_file = tempfile.mktemp(suffix='.txt')
        with open(concat_file, 'w') as f:
            for tf in temp_files:
                f.write(f"file '{tf}'\n")
        
        try:
            print(f"[tts_file_chunk] Concatenating chunk {chunk_num}")
            subprocess.run(
                ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_file, '-c', 'copy', output_file],
                check=True, capture_output=True
            )
            print(f"[tts_file_chunk] Concatenation complete for chunk {chunk_num}")
        except Exception as e:
            print(f"[tts_file_chunk] Concat error: {e}")
            return jsonify({"error": "Concatenation failed"}), 500
        finally:
            for tf in temp_files:
                try:
                    os.remove(tf)
                except:
                    pass
            try:
                os.remove(concat_file)
            except:
                pass
        
        # Save metadata - offsets[i] = start time of line i (even if skipped)
        offsets = []
        cumulative = 0.0
        for d in durations:
            offsets.append(cumulative)
            cumulative += d
        
        # Ensure offsets match chunk line count
        while len(offsets) < len(chunk_lines):
            offsets.append(cumulative)
        offsets = offsets[:len(chunk_lines)]
        
        metadata = {'offsets': offsets, 'chunk': chunk_num, 'start_idx': start_idx, 'end_idx': end_idx}
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f)
        
        try:
            prune_cache(200)
        except:
            pass
    
    # Return audio with metadata
    try:
        resp = send_file(output_file, mimetype='audio/wav', conditional=False)
        resp.headers['Cache-Control'] = 'public, max-age=31536000'
        resp.headers['X-Offsets'] = json.dumps(offsets)
        resp.headers['X-Chunk'] = str(chunk_num)
        resp.headers['X-Total-Chunks'] = '5'
        resp.headers['X-Start-Index'] = str(start_idx)
        resp.headers['X-End-Index'] = str(end_idx)
        return resp
    except Exception as e:
        print(f"[tts_file_chunk] Send error: {e}")
        return jsonify({"error": "Failed to send file"}), 500


@app.route('/api/upload', methods=['POST'])
def api_upload():
    """Upload a .txt file and optionally create/select a folder under DATA_FOLDER."""
    # Choose target folder: new_folder takes precedence
    new_folder = request.form.get('new_folder', '').strip()
    folder = request.form.get('folder', '').strip()
    target_folder = new_folder or folder
    if not target_folder:
        return jsonify({'error': 'No target folder specified'}), 400

    # ensure safe folder name
    target_folder = secure_filename(target_folder)
    target_path = os.path.join(DATA_FOLDER, target_folder)
    os.makedirs(target_path, exist_ok=True)

    files = request.files.getlist('file')
    if not files:
        return jsonify({'error': 'No files uploaded'}), 400

    saved = []
    for file in files:
        if not file or file.filename == '':
            continue
        filename = secure_filename(file.filename)
        if not allowed_file(filename):
            # force .txt if missing
            if '.' not in filename:
                filename = filename + '.txt'
            else:
                # skip non-txt
                continue

        save_path = os.path.join(target_path, filename)
        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(save_path):
            filename = f"{base}-{counter}{ext}"
            save_path = os.path.join(target_path, filename)
            counter += 1

        try:
            file.save(save_path)
            saved.append(filename)
        except Exception as e:
            # continue saving others
            print('Upload save error', e)

    if not saved:
        return jsonify({'error': 'No files were saved'}), 400
    return jsonify({'ok': True, 'folder': target_folder, 'files': saved})


# =====================================================================
# Inline logic từ themcach.py và tach.py
# =====================================================================

_DASH_LINE = "----------------------------------------"


def _themcach_has_delimiter(text: str) -> bool:
    """Kiểm tra xem file đã có dòng phân cách '---...' chưa.
    Nếu đã có → không cần chạy themcach, nhảy thẳng sang tach."""
    return _DASH_LINE in text


def _themcach_process(text: str) -> str:
    """Thay thế dòng trống bằng dòng phân cách (logic từ themcach.py)."""
    lines = text.splitlines()
    output_lines = []
    for line in lines:
        if line.strip() == "":
            output_lines.append(_DASH_LINE)
        else:
            output_lines.append(line)
    return "\n".join(output_lines) + ("\n" if text.endswith("\n") else "")


def _tach_split(text: str) -> list:
    """Tách nội dung theo dòng phân cách, trả về list[{name, lines}] (logic từ tach.py)."""
    sections = text.split(_DASH_LINE)
    results = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        lines = section.split('\n')
        name = lines[0].strip()
        if not name:
            continue
        content_lines = [l for l in lines[1:] if l.strip()]
        if not content_lines:
            continue
        # Tạo tên file an toàn (giữ ký tự Unicode, loại ký tự cấm Windows/Linux)
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name).strip()
        if not safe_name:
            safe_name = f"chuong_{len(results) + 1}"
        results.append({'name': safe_name + '.txt', 'lines': content_lines})
    return results


@app.route('/api/upload_split', methods=['POST'])
def api_upload_split():
    """Upload 1 file .txt lớn → chạy themcach (nếu cần) + tach → lưu các file con vào data/<folder>/.

    Form fields:
        file   : file .txt
        folder : tên thư mục đích (đã tồn tại hoặc mới)
    """
    folder_name = request.form.get('folder', '').strip()
    if not folder_name:
        return jsonify({'error': 'Thiếu tên thư mục'}), 400

    folder_name = sanitize_folder_name(folder_name)
    if not folder_name:
        return jsonify({'error': 'Tên thư mục không hợp lệ'}), 400

    file = request.files.get('file')
    if not file or file.filename == '':
        return jsonify({'error': 'Không có file được gửi lên'}), 400

    filename = secure_filename(file.filename)
    if not filename.lower().endswith('.txt'):
        return jsonify({'error': 'Chỉ chấp nhận file .txt'}), 400

    # Đọc nội dung
    try:
        raw_bytes = file.read()
        text = raw_bytes.decode('utf-8')
    except UnicodeDecodeError:
        try:
            text = raw_bytes.decode('utf-8-sig')
        except Exception:
            return jsonify({'error': 'File không đúng định dạng UTF-8'}), 400

    steps = []  # log các bước để trả về client

    # ── Bước 1: kiểm tra xem đã có dòng phân cách chưa
    if _themcach_has_delimiter(text):
        steps.append({'msg': 'File đã có dòng phân cách — bỏ qua bước themcach.', 'ok': True})
    else:
        steps.append({'msg': 'Chưa có dòng phân cách — đang thêm vào dòng trống...', 'info': True})
        text = _themcach_process(text)
        steps.append({'msg': 'Đã thêm dòng phân cách xong.', 'ok': True})

    # ── Bước 2: tách thành các file con
    chunks = _tach_split(text)
    if not chunks:
        return jsonify({'error': 'Không tách được nội dung (không tìm thấy chương nào)'}), 400

    steps.append({'msg': f'Tách được {len(chunks)} chương/đoạn.', 'ok': True})

    # ── Bước 3: tạo folder và lưu file con
    target_dir = os.path.join(DATA_FOLDER, folder_name)
    os.makedirs(target_dir, exist_ok=True)
    steps.append({'msg': f'Thư mục đích: data/{folder_name}', 'info': True})

    saved_files = []
    for chunk in chunks:
        name = chunk['name']
        content = '\n'.join(chunk['lines'])
        save_path = os.path.join(target_dir, name)

        # Tránh ghi đè — đặt tên lại nếu trùng
        base, ext = os.path.splitext(name)
        counter = 1
        while os.path.exists(save_path):
            name = f"{base}-{counter}{ext}"
            save_path = os.path.join(target_dir, name)
            counter += 1

        try:
            with open(save_path, 'w', encoding='utf-8') as f:
                f.write(content)
            saved_files.append(name)
        except Exception as e:
            steps.append({'msg': f'Lỗi lưu {name}: {e}', 'err': True})

    steps.append({'msg': f'Đã lưu {len(saved_files)}/{len(chunks)} file vào data/{folder_name}/', 'ok': True})

    return jsonify({
        'ok': True,
        'folder': folder_name,
        'file_count': len(saved_files),
        'files': saved_files,
        'steps': steps
    })

if __name__ == '__main__':
    # Chạy server ở port 80
    # Disable debug reloader and enable threading for stable background run
    # prune cache at startup (keep recent 200 files)
    try:
        prune_cache(200)
    except Exception:
        pass
    app.run(host='0.0.0.0', port=80, debug=False, use_reloader=False, threaded=True)
