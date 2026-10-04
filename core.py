import io, os, pickle
from datetime import datetime
import cv2, numpy as np
from PIL import Image
from insightface.app import FaceAnalysis
from scipy.optimize import linear_sum_assignment

DB_PATH = "embeddings.pkl"
_app = None


def get_app(det_size=1280):
    global _app
    if _app is None:
        _app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        _app.prepare(ctx_id=-1, det_size=(det_size, det_size))
    return _app


def load_db():
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "rb") as f:
            return pickle.load(f)
    return {}


def save_db(db):
    with open(DB_PATH, "wb") as f:
        pickle.dump(db, f)


def decode(file_bytes):
    return cv2.imdecode(np.frombuffer(file_bytes, np.uint8), cv2.IMREAD_COLOR)


def detect_faces(img):
    """Detect faces; retry with padding for tightly cropped photos."""
    if img is None:
        return []
    faces = get_app().get(img)
    if not faces:
        pad = int(0.5 * max(img.shape[:2]))
        padded = cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(0, 0, 0))
        faces = get_app().get(padded)
    return faces


def enroll(name, images):
    """Add embeddings for a student (largest face per photo). Returns (added, skipped)."""
    db = load_db()
    added = 0
    for img in images:
        faces = detect_faces(img)
        if not faces:
            continue
        face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        db.setdefault(name, []).append(face.normed_embedding)
        added += 1
    save_db(db)
    return added, len(images) - added


def recognize(img, db, threshold=0.4):
    """Returns (faces, matches) with matches = {face_index: (name, score)}.
    One-to-one assignment: a student can't be matched to two faces."""
    faces = get_app().get(img)
    names = list(db)
    if not faces or not names:
        return faces, {}
    S = np.array([[max(float(f.normed_embedding @ e) for e in db[n]) for n in names] for f in faces])
    rows, cols = linear_sum_assignment(-S)
    return faces, {r: (names[c], float(S[r, c])) for r, c in zip(rows, cols) if S[r, c] >= threshold}


def annotate(img, faces, matches):
    out = img.copy()
    scale = max(out.shape[:2]) / 1500
    for i, f in enumerate(faces):
        x1, y1, x2, y2 = map(int, f.bbox)
        ok = i in matches
        color = (0, 200, 0) if ok else (0, 0, 255)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, max(2, int(2 * scale)))
        cv2.putText(out, matches[i][0] if ok else "?", (x1, max(15, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6 * max(scale, 0.7), color, max(1, int(2 * scale)))
    return out


def extract_metadata(raw, last_modified_ms=None):
    """Read EXIF (capture time, camera, size). Falls back to the file's modified
    time (sent by the browser), then to the current time."""
    meta = {"camera": None, "width": None, "height": None, "taken_at": None, "source": None}
    dt = None
    try:
        im = Image.open(io.BytesIO(raw))
        meta["width"], meta["height"] = im.size
        exif = im.getexif()
        sub = exif.get_ifd(0x8769)  # Exif sub-IFD
        stamp = sub.get(36867) or sub.get(36868) or exif.get(306)  # DateTimeOriginal / Digitized / DateTime
        meta["camera"] = " ".join(str(x).strip() for x in (exif.get(271), exif.get(272)) if x) or None
        if stamp:
            dt = datetime.strptime(str(stamp).strip(), "%Y:%m:%d %H:%M:%S")
            meta["source"] = "EXIF capture time"
    except Exception:
        pass
    if dt is None and last_modified_ms:
        try:
            dt = datetime.fromtimestamp(int(float(last_modified_ms)) / 1000)
            meta["source"] = "file modified time (no EXIF found)"
        except ValueError:
            pass
    if dt is None:
        dt = datetime.now()
        meta["source"] = "upload time (no EXIF found)"
    meta["taken_at"] = dt.strftime("%Y-%m-%d %H:%M")
    return meta
