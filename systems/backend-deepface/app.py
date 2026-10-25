import os
import time
import base64
import sqlite3
import threading
from datetime import datetime
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename
import cv2
import numpy as np
from deepface import DeepFace
from io import BytesIO
from PIL import Image

app = Flask(__name__)
CORS(app)

# Configuration
UPLOAD_FOLDER = 'uploads'
DATABASE_FOLDER = 'database_images'

# Location of this service's local processing database.
#
# It was previously a bare relative filename, which in a container resolves to
# /app/face_labels.db - inside the image's writable layer, mounted by nothing, so
# it grew unbounded while running and vanished on restart. The Dockerfile even
# declared a VOLUME for /app/data that nothing ever wrote to. Making it
# configurable lets the container point it at that mounted volume while a host
# run keeps the old working-directory default.
FACE_LABELS_DB = os.environ.get('FACE_LABELS_DB', 'face_labels.db')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DATABASE_FOLDER, exist_ok=True)
_db_dir = os.path.dirname(FACE_LABELS_DB)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['DATABASE_FOLDER'] = DATABASE_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Initialize SQLite database
def _db():
    """
    Open the local processing database.

    There are now several gunicorn worker processes, all writing here. WAL lets
    a reader proceed while another process writes, and the busy timeout turns
    the remaining contention into a short wait instead of an immediate
    "database is locked". Both matter from the very first call, because every
    worker runs init_db() at import - simultaneously.
    """
    conn = sqlite3.connect(FACE_LABELS_DB, timeout=60)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
    except Exception:
        pass  # a concurrent worker is mid-switch; the timeout above covers us
    return conn


def init_db():
    conn = _db()
    cursor = conn.cursor()
    
    # Create tables
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS face_detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            x INTEGER NOT NULL,
            y INTEGER NOT NULL,
            w INTEGER NOT NULL,
            h INTEGER NOT NULL,
            label_id INTEGER,
            confidence REAL,
            FOREIGN KEY (image_id) REFERENCES images (id),
            FOREIGN KEY (label_id) REFERENCES labels (id)
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()


def sweep_stale_uploads(max_age_hours: float = 6.0) -> int:
    """
    Delete leftovers in the uploads folder older than max_age_hours.

    Every file written there is transient - an image held only long enough to run
    inference, then removed. Anything still present hours later is a leak from a
    crash or an early return. Without a sweep these accumulate silently: this
    folder had reached 9,376 files and 8.1 GB, on a mounted volume, before anyone
    noticed.

    Runs once at startup. Set DEEPFACE_UPLOAD_SWEEP_HOURS=0 to disable.
    """
    try:
        max_age_hours = float(os.environ.get('DEEPFACE_UPLOAD_SWEEP_HOURS',
                                             max_age_hours))
    except (TypeError, ValueError):
        pass
    if max_age_hours <= 0:
        return 0

    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    freed = 0
    try:
        entries = os.listdir(UPLOAD_FOLDER)
    except OSError:
        return 0

    for name in entries:
        path = os.path.join(UPLOAD_FOLDER, name)
        try:
            if not os.path.isfile(path) or os.path.getmtime(path) >= cutoff:
                continue
            freed += os.path.getsize(path)
            os.remove(path)
            removed += 1
        except OSError:
            continue

    if removed:
        print(f"Swept {removed} stale upload(s) older than {max_age_hours}h "
              f"({freed / 1024 / 1024:.1f} MB freed)")
    return removed


sweep_stale_uploads()

# ── Warm up the YuNet detector at startup ─────────────────────────────────────
# YuNet is an OpenCV-native detector — no TensorFlow, ~1 MB model file.
# RetinaFace required TensorFlow (~500 MB RAM) and was OOM-killing the worker.
# YuNet runs entirely in OpenCV's DNN module, uses ~50 MB, and is fast.
# Report where weights are being read from BEFORE touching the model. If this
# directory is wrong the library silently re-downloads, and under gunicorn that
# download happens inside a forked worker - which macOS aborts outright
# ("+[NSCharacterSet initialize] may have been in progress ... Crashing instead"),
# producing a boot loop whose only visible symptom is a misleading
# "Worker was sent SIGKILL! Perhaps out of memory?".
#
# Everything below runs at import time. Under gunicorn that means once per
# worker, AFTER it has been forked - which is the only safe place for it. Do
# not "optimise" this by adding --preload to share the weights across workers:
# the thread pools these calls start do not survive fork(), and the workers all
# die with SIGSEGV instead.
_deepface_home = os.environ.get('DEEPFACE_HOME', os.path.expanduser('~'))
_weights_dir = os.path.join(_deepface_home, '.deepface', 'weights')
_yunet = os.path.join(_weights_dir, 'face_detection_yunet_2023mar.onnx')

# ── Which embedding model ─────────────────────────────────────────────────────
#
# DEFINED HERE, above the start-up block, because that block warms these models
# at import time. It previously sat further down next to the endpoints that use
# it, which reads better and crashes every worker:
#
#   File "/app/app.py", line 305, in <module>
#     for _model in WARMUP_MODELS:
#   NameError: name 'WARMUP_MODELS' is not defined
#
# Module-level code runs top to bottom, and nothing in the test suite imports
# this file, so it failed on the first real start instead.
#
# Was the literal 'VGG-Face' in five places here. It is now a default that a
# request can override, because a catalog chooses its own model
# (documentation/specs/face-recognition-migration-spec.md) and one service has
# to serve catalogs part-way through a migration.
#
# The default stays VGG-Face so that a deployment which upgrades this service
# and nothing else behaves exactly as it did.
DEFAULT_EMBEDDING_MODEL = os.environ.get('DEEPFACE_MODEL', 'VGG-Face')

# Models this worker will pre-load. During a migration both are in use, and
# paying the load cost on a request is what the warm-up exists to avoid.
WARMUP_MODELS = [m.strip() for m in os.environ.get(
    'DEEPFACE_WARMUP_MODELS', DEFAULT_EMBEDDING_MODEL).split(',') if m.strip()]

# Known output dimensions, used only to report them. DeepFace owns the real
# list; these are the two airlock uses.
MODEL_DIMS = {'VGG-Face': 4096, 'SFace': 128}

# The file each model needs in the bind-mounted weights cache, so /api/health
# can say whether a model is actually usable rather than merely configured.
MODEL_WEIGHT_FILES = {
    'VGG-Face': 'vgg_face_weights.h5',                  # 560 MB
    'SFace': 'face_recognition_sface_2021dec.onnx',     # ~37 MB
}

print(f"DeepFace weights dir: {_weights_dir}")
if not os.path.exists(_yunet):
    print(f"WARNING: YuNet model not found at {_yunet} - it will be downloaded.")
    print("         On macOS under gunicorn, set DEEPFACE_HOME to a directory that")
    print("         already holds .deepface/weights, or the download can abort the")
    print("         worker. See start-host.sh.")

# ── Face detection ────────────────────────────────────────────────────────────
#
# DeepFace's YuNet wrapper hard-codes a downsample that destroys group photos:
#
#   deepface/models/face_detection/YuNet.py
#   if height > 640 or width > 640:
#       r = 640.0 / max(height, width)
#       img = cv2.resize(img, (int(width * r), int(height * r)))
#
# Every image, whatever its size, is detected at 640 px on its longest side. In
# a 6000x4000 group shot a face is then 12-15 px across, which scores below
# YuNet's 0.9 confidence threshold and is dropped. Measured over an 18-image
# sample of this catalog:
#
#   detection size   faces found   median detect
#   640 (DeepFace)            43            5 ms
#   1024                      70           11 ms
#   1600                      85           23 ms
#   2048                      86           39 ms
#
# The 640 cap was losing half the faces in the library. 2048 finds one more
# face than 1600 for 16 ms more, so 1600 is where the curve flattens.
#
# We call cv2.FaceDetectorYN ourselves instead. This endpoint only ever used
# facial_area and confidence from extract_faces - never its aligned crops - so
# nothing is lost by not going through DeepFace here. /api/embed and
# /api/compare-multiple still use DeepFace as before.
DETECTION_INPUT_MAX_DIM = int(os.environ.get('DETECTION_INPUT_MAX_DIM', '1600'))
YUNET_SCORE_THRESHOLD = float(os.environ.get('YUNET_SCORE_THRESHOLD', '0.9'))

_yunet_detector = None
_yunet_lock = threading.Lock()


def get_yunet():
    """Build the detector once per process. Returns None if weights are absent."""
    global _yunet_detector
    if _yunet_detector is None:
        if not os.path.exists(_yunet):
            return None
        _yunet_detector = cv2.FaceDetectorYN_create(_yunet, "", (0, 0))
    return _yunet_detector


def detect_faces_yunet(bgr):
    """
    Detect faces in a BGR ndarray.

    Returns a list of {x, y, w, h, confidence} in the coordinate space of the
    image passed in, and the size actually used for inference.

    FaceDetectorYN carries mutable per-call state (setInputSize,
    setScoreThreshold), so the lock matters if this is ever run with
    --threads > 1. Workers are processes today, which makes it free.
    """
    model = get_yunet()
    if model is None:
        raise RuntimeError(f'YuNet weights not found at {_yunet}')

    height, width = bgr.shape[:2]
    scale = 1.0
    img = bgr
    if DETECTION_INPUT_MAX_DIM > 0 and max(height, width) > DETECTION_INPUT_MAX_DIM:
        scale = DETECTION_INPUT_MAX_DIM / float(max(height, width))
        img = cv2.resize(bgr, (max(1, int(width * scale)), max(1, int(height * scale))),
                         interpolation=cv2.INTER_AREA)

    ih, iw = img.shape[:2]
    with _yunet_lock:
        model.setInputSize((iw, ih))
        model.setScoreThreshold(YUNET_SCORE_THRESHOLD)
        _, raw = model.detect(img)

    faces = []
    if raw is None:
        return faces, (iw, ih)

    inv = 1.0 / scale if scale != 1.0 else 1.0
    for row in raw:
        x, y, w, h = (int(round(v * inv)) for v in row[:4])
        # YuNet returns negative coordinates when it believes part of a face
        # falls outside the frame.
        x, y = max(x, 0), max(y, 0)
        w = min(w, width - x)
        h = min(h, height - y)
        if w <= 0 or h <= 0:
            continue
        faces.append({'x': x, 'y': y, 'w': w, 'h': h, 'confidence': float(row[-1])})
    return faces, (iw, ih)


if os.environ.get('DEEPFACE_SKIP_WARMUP', '').lower() in ('1', 'true', 'yes'):
    print("Skipping YuNet pre-load (DEEPFACE_SKIP_WARMUP set)")
else:
    print("Pre-loading YuNet face detector...")
    try:
        _blank = np.zeros((64, 64, 3), dtype=np.uint8)
        detect_faces_yunet(_blank)
        print(f"YuNet detector loaded (inference capped at "
              f"{DETECTION_INPUT_MAX_DIM or 'no limit'} px, "
              f"score >= {YUNET_SCORE_THRESHOLD}).")
    except Exception as _e:
        # Note: this catches Python errors only. A macOS fork-safety abort is a
        # SIGABRT and cannot be caught here - it is prevented by loading weights
        # from disk and by OBJC_DISABLE_INITIALIZE_FORK_SAFETY, not by this
        # handler - and emphatically not by --preload, which causes a different
        # crash (SIGSEGV) of its own. See start-host.sh.
        print(f"Warning: could not pre-load YuNet detector: {_e}")

    # Build the embedding model(s) now, so the first /api/embed request is not
    # the one that pays for it.
    #
    # This MUST run inside the worker, not in a gunicorn master that later
    # forks. Building it starts TensorFlow's Eigen/OpenMP thread pools, and
    # fork() carries only the calling thread into the child - every other
    # thread disappears holding whatever locks it held. A child that then
    # touches TensorFlow dies with SIGSEGV, immediately and forever. That is
    # why start-host.sh deliberately does NOT pass --preload; see the long
    # comment above its gunicorn line.
    #
    # The cost of getting this right is one model load per worker (~550 MB
    # resident each, nothing shared). Set DEEPFACE_WARMUP_EMBEDDER=0 on a host
    # that only serves detection to skip it entirely.
    if os.environ.get('DEEPFACE_WARMUP_EMBEDDER', '1').lower() in ('0', 'false', 'no'):
        print("Skipping embedder pre-load (DEEPFACE_WARMUP_EMBEDDER=0)")
    else:
        for _model in WARMUP_MODELS:
            print(f"Pre-loading {_model} embedding model...")
            try:
                DeepFace.represent(
                    img_path=np.zeros((224, 224, 3), dtype=np.uint8),
                    model_name=_model,
                    enforce_detection=False,
                    detector_backend='skip',
                )
                print(f"{_model} model loaded.")
            except Exception as _e:
                # Not fatal: a model that fails to warm will be loaded on first
                # use, or will fail there with a clearer error than this.
                print(f"Warning: could not pre-load {_model} model: {_e}")

def requested_model(req):
    """
    The embedding model for one request: an explicit `model` field, else the
    deployment default. Unknown names are passed through to DeepFace, which
    raises a clear error of its own - this service does not keep a second,
    staler list of what DeepFace supports.
    """
    return (req.form.get('model') or req.args.get('model')
            or DEFAULT_EMBEDDING_MODEL)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/api/health', methods=['GET'])
def health_check():
    """
    Liveness, plus which model this service embeds with and whether the
    weights for each known model are actually on disk.

    `weights` matters before a migration: the cache is bind-mounted rather than
    baked into the image, so a model can be configured and still be missing -
    in which case DeepFace downloads it inside a gunicorn worker on the first
    request, which is a bad moment to find out. tools/fetch_deepface_weights.sh
    puts it there in advance.
    """
    weights = {}
    for _model, _file in MODEL_WEIGHT_FILES.items():
        _path = os.path.join(_weights_dir, _file)
        weights[_model] = os.path.exists(_path)
    return jsonify({'status': 'healthy', 'message': 'Backend is running',
                    'embedding_model': DEFAULT_EMBEDDING_MODEL,
                    'embedding_dims': MODEL_DIMS.get(DEFAULT_EMBEDDING_MODEL),
                    'warmed': WARMUP_MODELS,
                    'weights': weights})

@app.route('/api/upload', methods=['POST'])
def upload_image():
    """Upload an image and detect faces"""
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{timestamp}_{filename}"

        try:
            # Decode straight from the request body into an ndarray.
            #
            # This previously wrote the upload to uploads/, handed the *path* to
            # DeepFace (a second full decode), opened it a third time with PIL
            # just to read its size, then deleted it in a finally block. The
            # file was never readable afterwards - /api/image/<id> could only
            # ever 404 - so the disk round-trip bought nothing and cost two
            # extra decodes of a multi-megapixel JPEG on every single image.
            raw = np.frombuffer(file.read(), dtype=np.uint8)
            if raw.size == 0:
                return jsonify({'error': 'Empty upload'}), 400

            # cv2.imdecode yields BGR, which is what YuNet (an OpenCV DNN model)
            # was trained on and what DeepFace assumes for ndarray inputs - the
            # same colour order cv2.imread produced on the old path.
            bgr = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if bgr is None:
                return jsonify({'error': 'Could not decode image'}), 400

            img_height, img_width = bgr.shape[:2]

            # Detect faces using YuNet - OpenCV-native, no TensorFlow dependency.
            try:
                face_objs, inference_size = detect_faces_yunet(bgr)
                detector_used = 'yunet'
            except Exception as e:
                # A real failure (missing weights, corrupt model) must not be
                # reported as "this photo has no faces" - that marks the image
                # scanned and silently skips it forever.
                print(f"YuNet detection error: {e}")
                return jsonify({'error': f'Detection failed: {e}'}), 500

            if not face_objs:
                conn = _db()
                cursor = conn.cursor()
                cursor.execute('INSERT INTO images (filename) VALUES (?)', (filename,))
                image_id = cursor.lastrowid
                conn.commit()
                conn.close()
                return jsonify({
                    'success': True,
                    'image_id': image_id,
                    'filename': filename,
                    'faces': [],
                    'detector': detector_used,
                    'width': img_width,
                    'height': img_height,
                    'message': 'No faces detected in image'
                })

            # Save image to database
            conn = _db()
            cursor = conn.cursor()
            cursor.execute('INSERT INTO images (filename) VALUES (?)', (filename,))
            image_id = cursor.lastrowid

            max_face_width = img_width * 0.8
            max_face_height = img_height * 0.8

            faces = []
            for obj in face_objs:
                x, y, w, h = obj['x'], obj['y'], obj['w'], obj['h']
                confidence = obj['confidence']

                # Skip faces that are too large (likely false positives)
                if w > max_face_width or h > max_face_height:
                    print(f"Skipping suspiciously large face: {w}x{h} in {img_width}x{img_height} image")
                    continue

                cursor.execute('''
                    INSERT INTO face_detections (image_id, x, y, w, h, confidence)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (image_id, x, y, w, h, confidence))

                face_id = cursor.lastrowid

                faces.append({
                    'id': face_id,
                    'x': x,
                    'y': y,
                    'w': w,
                    'h': h,
                    'confidence': confidence,
                    'label': None
                })

            conn.commit()
            conn.close()

            return jsonify({
                'success': True,
                'image_id': image_id,
                'filename': filename,
                'faces': faces,
                'detector': detector_used,
                'width': img_width,
                'height': img_height,
                'inference_size': list(inference_size)
            })

        except Exception as e:
            return jsonify({'error': str(e)}), 500

    return jsonify({'error': 'Invalid file type'}), 400

@app.route('/api/image/<int:image_id>', methods=['GET'])
def get_image(image_id):
    """Get the original image"""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute('SELECT filename FROM images WHERE id = ?', (image_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], result[0])
        if os.path.exists(filepath):
            return send_file(filepath, mimetype='image/jpeg')
    
    return jsonify({'error': 'Image not found'}), 404

@app.route('/api/image/<int:image_id>/annotated', methods=['GET'])
def get_annotated_image(image_id):
    """Get image with face rectangles drawn"""
    conn = _db()
    cursor = conn.cursor()
    
    # Get image filename
    cursor.execute('SELECT filename FROM images WHERE id = ?', (image_id,))
    result = cursor.fetchone()
    
    if not result:
        conn.close()
        return jsonify({'error': 'Image not found'}), 404
    
    filename = result[0]
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    # Get face detections
    cursor.execute('''
        SELECT fd.x, fd.y, fd.w, fd.h, l.name
        FROM face_detections fd
        LEFT JOIN labels l ON fd.label_id = l.id
        WHERE fd.image_id = ?
    ''', (image_id,))
    
    faces = cursor.fetchall()
    conn.close()
    
    # Load and annotate image
    img = cv2.imread(filepath)
    
    for face in faces:
        x, y, w, h, label = face
        # Draw rectangle
        color = (0, 255, 0) if label else (255, 0, 0)  # Green if labeled, blue if not
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
        
        # Draw label if exists
        if label:
            cv2.putText(img, label, (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 
                       0.5, color, 2)
    
    # Convert to bytes
    _, buffer = cv2.imencode('.jpg', img)
    io_buf = BytesIO(buffer)
    io_buf.seek(0)
    
    return send_file(io_buf, mimetype='image/jpeg')

@app.route('/api/labels', methods=['GET'])
def get_labels():
    """Get all available labels"""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name FROM labels ORDER BY name')
    labels = [{'id': row[0], 'name': row[1]} for row in cursor.fetchall()]
    conn.close()
    
    return jsonify({'labels': labels})

@app.route('/api/labels', methods=['POST'])
def create_label():
    """Create a new label"""
    data = request.get_json()
    
    if not data or 'name' not in data:
        return jsonify({'error': 'Label name is required'}), 400
    
    label_name = data['name'].strip()
    
    if not label_name:
        return jsonify({'error': 'Label name cannot be empty'}), 400
    
    try:
        conn = _db()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO labels (name) VALUES (?)', (label_name,))
        label_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'id': label_id, 'name': label_name})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Label already exists'}), 400

@app.route('/api/face/<int:face_id>/label', methods=['PUT'])
def update_face_label(face_id):
    """Update the label for a specific face"""
    data = request.get_json()
    
    if not data or 'label_id' not in data:
        return jsonify({'error': 'Label ID is required'}), 400
    
    label_id = data['label_id']
    
    conn = _db()
    cursor = conn.cursor()
    
    # Verify label exists
    if label_id is not None:
        cursor.execute('SELECT id FROM labels WHERE id = ?', (label_id,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({'error': 'Label not found'}), 404
    
    # Update face label
    cursor.execute('UPDATE face_detections SET label_id = ? WHERE id = ?', 
                  (label_id, face_id))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@app.route('/api/compare', methods=['POST'])
def compare_face():
    """Compare a face region against database images"""
    data = request.get_json()
    
    if not data or 'image_id' not in data or 'face_id' not in data:
        return jsonify({'error': 'image_id and face_id are required'}), 400
    
    image_id = data['image_id']
    face_id = data['face_id']
    
    conn = _db()
    cursor = conn.cursor()
    
    # Get image and face coordinates
    cursor.execute('''
        SELECT i.filename, fd.x, fd.y, fd.w, fd.h
        FROM face_detections fd
        JOIN images i ON fd.image_id = i.id
        WHERE fd.id = ? AND i.id = ?
    ''', (face_id, image_id))
    
    result = cursor.fetchone()
    
    if not result:
        conn.close()
        return jsonify({'error': 'Face not found'}), 404
    
    filename, x, y, w, h = result
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    # Extract face region
    img = cv2.imread(filepath)
    face_img = img[y:y+h, x:x+w]
    
    # Save temporary face image
    temp_face_path = os.path.join(app.config['UPLOAD_FOLDER'], f'temp_face_{face_id}.jpg')
    cv2.imwrite(temp_face_path, face_img)
    
    try:
        # Compare against database
        if os.path.exists(app.config['DATABASE_FOLDER']) and \
           len(os.listdir(app.config['DATABASE_FOLDER'])) > 0:
            
            results = DeepFace.find(
                img_path=temp_face_path,
                db_path=app.config['DATABASE_FOLDER'],
                enforce_detection=False,
                silent=True
            )
            
            matches = []
            if len(results) > 0 and len(results[0]) > 0:
                df = results[0]
                for _, row in df.head(5).iterrows():  # Top 5 matches
                    matches.append({
                        'identity': os.path.basename(row['identity']),
                        'distance': float(row['distance']),
                        'threshold': float(row['threshold'])
                    })
            
            conn.close()
            os.remove(temp_face_path)
            
            return jsonify({
                'success': True,
                'matches': matches
            })
        else:
            conn.close()
            os.remove(temp_face_path)
            return jsonify({
                'success': True,
                'matches': [],
                'message': 'Database is empty'
            })
            
    except Exception as e:
        conn.close()
        if os.path.exists(temp_face_path):
            os.remove(temp_face_path)
        return jsonify({'error': str(e)}), 500
@app.route('/api/embed', methods=['POST'])
def embed_face():
    """
    Compute and return the embedding for a single face image.

    The caller should send a pre-cropped face JPEG (already aligned / resized).
    detector_backend='skip' is used intentionally: the workflow already isolates
    the face region before calling this endpoint, so re-running face detection
    would be redundant and error-prone on tight crops.

    Request (multipart/form-data):
        face_image  — the face image file
        model       — optional; defaults to DEEPFACE_MODEL (VGG-Face)

    Response:
        { success: true, embedding: [...], model: str, dims: int }

    `model` and `dims` are returned so the caller can record which model
    produced what it is about to store, and verify it got the length it
    expected. VGG-Face is 4096 - an earlier version of this docstring said 512,
    which was simply wrong.
    """
    if 'face_image' not in request.files:
        return jsonify({'error': 'face_image file is required'}), 400

    face_file = request.files['face_image']
    if face_file.filename == '':
        return jsonify({'error': 'No face image selected'}), 400

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    face_filename = f"temp_embed_{timestamp}.jpg"
    face_path = os.path.join(app.config['UPLOAD_FOLDER'], face_filename)

    try:
        face_file.save(face_path)

        model = requested_model(request)
        result = DeepFace.represent(
            img_path=face_path,
            model_name=model,
            enforce_detection=False,
            detector_backend='skip',
        )
        embedding = result[0]['embedding']

        return jsonify({'success': True, 'embedding': embedding,
                        'model': model, 'dims': len(embedding)})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

    finally:
        if os.path.exists(face_path):
            os.remove(face_path)


@app.route('/api/compare-multiple', methods=['POST'])
def compare_face_to_multiple():
    """
    Compare a face against multiple reference faces and return cosine distances.

    Supports two modes — both fully backward-compatible:

    Fast mode (pre-computed embeddings):
        Pass query_embedding as a JSON float array in the form field, and
        embedding_0, embedding_1, … for each reference face. No image files
        are required for the references that have embeddings. CNN inference is
        skipped entirely for those; only a cosine distance is computed.
        The query embedding is similarly skipped when query_embedding is provided.

    Legacy mode (image files):
        Send face_image + comparison_0, comparison_1, … as multipart files.
        DeepFace.verify() runs the full CNN on both images for every comparison,
        exactly as before.

    Mixed mode is also valid: supply query_embedding but not all reference
    embeddings, or vice versa. Each comparison falls back to image-based
    inference only for the missing embedding.

    Form fields:
        model                    — optional; defaults to DEEPFACE_MODEL
        query_embedding          — JSON-encoded List[float], 4096-dim for VGG-Face
        embedding_N              — JSON-encoded List[float] for reference N
        person_id_N, face_id_N, person_name_N — metadata echoed in response
    Files:
        face_image               — query face (required if query_embedding absent)
        comparison_N             — reference N (required if embedding_N absent)
    """
    import json as _json

    # ── Resolve query ──────────────────────────────────────────────────────────
    query_embedding_raw = request.form.get('query_embedding')
    query_embedding = _json.loads(query_embedding_raw) if query_embedding_raw else None

    # Still need an image file when no pre-computed embedding is supplied
    if query_embedding is None:
        if 'face_image' not in request.files or request.files['face_image'].filename == '':
            return jsonify({'error': 'face_image file or query_embedding is required'}), 400

    # ── Collect references ─────────────────────────────────────────────────────
    comparison_files = []
    comparison_metadata = []

    idx = 0
    while True:
        has_file = f'comparison_{idx}' in request.files and request.files[f'comparison_{idx}'].filename != ''
        has_emb  = f'embedding_{idx}' in request.form
        if not has_file and not has_emb:
            break
        comparison_files.append(request.files.get(f'comparison_{idx}'))
        comparison_metadata.append({
            'index': idx,
            'person_id':   request.form.get(f'person_id_{idx}'),
            'face_id':     request.form.get(f'face_id_{idx}'),
            'person_name': request.form.get(f'person_name_{idx}'),
            'embedding':   _json.loads(request.form[f'embedding_{idx}']) if has_emb else None,
        })
        idx += 1

    if idx == 0:
        return jsonify({'error': 'No comparison images or embeddings provided'}), 400

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    face_path = None
    comparison_paths = []

    try:
        # ── Save query image (only when no pre-computed embedding) ─────────────
        if query_embedding is None:
            face_file = request.files['face_image']
            face_filename = f"temp_query_{timestamp}.jpg"
            face_path = os.path.join(app.config['UPLOAD_FOLDER'], face_filename)
            face_file.save(face_path)

        # One model for the whole request. Resolved once rather than per
        # reference: comparing a query against references under two different
        # models would produce distances that cannot be ranked against each
        # other, which is the failure embedding_models.py exists to prevent.
        _cmp_model = requested_model(request)

        # ── Compare against each reference ─────────────────────────────────────
        results = []
        for idx, meta in enumerate(comparison_metadata):
            ref_embedding = meta['embedding']
            ref_path = None

            # Save reference image to disk only when no embedding is available
            if ref_embedding is None:
                comp_file = comparison_files[idx]
                if comp_file and comp_file.filename != '':
                    comp_filename = f"temp_comp_{timestamp}_{idx}.jpg"
                    ref_path = os.path.join(app.config['UPLOAD_FOLDER'], comp_filename)
                    comp_file.save(ref_path)
                    comparison_paths.append(ref_path)
                else:
                    results.append({
                        'index': idx,
                        'error': 'No image or embedding provided for this reference',
                        'person_id':   meta.get('person_id'),
                        'face_id':     meta.get('face_id'),
                        'person_name': meta.get('person_name'),
                    })
                    continue

            try:
                # Use pre-computed embeddings when available — DeepFace.verify()
                # accepts List[float] directly and skips CNN inference entirely.
                img1 = query_embedding if query_embedding is not None else face_path
                img2 = ref_embedding   if ref_embedding   is not None else ref_path

                result = DeepFace.verify(
                    img1_path=img1,
                    img2_path=img2,
                    enforce_detection=False,
                    model_name=_cmp_model,
                    distance_metric='cosine',
                )
                results.append({
                    'index':           idx,
                    'distance':        float(result['distance']),
                    'threshold':       float(result['threshold']),
                    'verified':        bool(result['verified']),
                    'model':           result.get('model', _cmp_model),
                    'distance_metric': result.get('distance_metric', 'cosine'),
                    'person_id':       meta.get('person_id'),
                    'face_id':         meta.get('face_id'),
                    'person_name':     meta.get('person_name'),
                })
            except Exception as e:
                results.append({
                    'index':       idx,
                    'error':       str(e),
                    'person_id':   meta.get('person_id'),
                    'face_id':     meta.get('face_id'),
                    'person_name': meta.get('person_name'),
                })

        valid_results = [r for r in results if 'distance' in r]
        valid_results.sort(key=lambda x: x['distance'])

        return jsonify({
            'success':                True,
            'total_comparisons':      len(results),
            'successful_comparisons': len(valid_results),
            'results':                results,
            'best_match':             valid_results[0] if valid_results else None,
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

    finally:
        # Cleanup belongs in `finally`, not duplicated across the success and
        # error branches. Any early return between them - a reference with
        # neither an image nor an embedding, a malformed request - used to leak
        # its temp files. 133 stale temp_comp_* files had accumulated this way.
        if face_path and os.path.exists(face_path):
            try:
                os.remove(face_path)
            except OSError:
                pass
        for p in comparison_paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


@app.route('/api/database/upload', methods=['POST'])
def upload_to_database():
    """Upload an image to the comparison database"""
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    
    file = request.files['file']
    label = request.form.get('label', 'unknown')
    
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        # Use label as part of filename for organization
        filename = f"{label}_{filename}"
        filepath = os.path.join(app.config['DATABASE_FOLDER'], filename)
        file.save(filepath)
        
        return jsonify({
            'success': True,
            'filename': filename,
            'message': 'Image added to database'
        })
    
    return jsonify({'error': 'Invalid file type'}), 400

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5001)
