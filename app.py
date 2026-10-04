




import base64
import cv2
import os
import secrets
import sqlite3
from contextlib import contextmanager
from functools import wraps
from flask import Flask, jsonify, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash
import core, sheets

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
ACCOUNT_DB = os.getenv("ACCOUNT_DB", "accounts.sqlite3")


def connect_accounts():
    return sqlite3.connect(ACCOUNT_DB)


@contextmanager
def account_db():
    db = connect_accounts()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_accounts():
    with account_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY,
            role TEXT NOT NULL CHECK (role IN ('student', 'teacher')),
            account_id TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            department TEXT,
            sheet_id TEXT,
            UNIQUE (role, account_id)
        )""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
        if "sheet_id" not in columns:
            db.execute("ALTER TABLE accounts ADD COLUMN sheet_id TEXT")
        sheets_table_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'teacher_sheets'"
        ).fetchone()
        db.execute("""CREATE TABLE IF NOT EXISTS teacher_sheets (
            id INTEGER PRIMARY KEY,
            teacher_account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            class_name TEXT NOT NULL,
            sheet_id TEXT NOT NULL,
            UNIQUE (teacher_account_id, class_name)
        )""")
        if not sheets_table_exists:
            db.execute("""INSERT OR IGNORE INTO teacher_sheets (teacher_account_id, class_name, sheet_id)
                SELECT id, 'Default class', sheet_id FROM accounts
                WHERE role = 'teacher' AND sheet_id IS NOT NULL AND TRIM(sheet_id) != ''""")


init_accounts()


def require_role(role):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if session.get("role") != role:
                return jsonify(error="Please sign in with an authorized account"), 401
            return fn(*args, **kwargs)
        return wrapped
    return decorate


@app.errorhandler(Exception)
def on_error(e):
    code = getattr(e, "code", 500)
    return jsonify(error=str(e)), code if isinstance(code, int) else 500


@app.get("/")
def index():
    return send_file("index.html")


@app.post("/api/register")
def register():
    role = (request.form.get("role") or "").strip()
    account_id = (request.form.get("account_id") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    department = (request.form.get("department") or "").strip()
    if role not in {"student", "teacher"} or not account_id or not email or len(password) < 8:
        return jsonify(error="Choose an account type and provide an ID, email, and password of at least 8 characters"), 400
    if role == "student" and department not in {"CSE", "AI/ML"}:
        return jsonify(error="Choose CSE or AI/ML"), 400
    if role == "teacher":
        department = None
    elif len(request.files.getlist("photos")) < 3:
        return jsonify(error="Capture front, left, and right face photos to create a student account"), 400

    with account_db() as db:
        exists = db.execute(
            "SELECT 1 FROM accounts WHERE email = ? OR (role = ? AND account_id = ?)",
            (email, role, account_id),
        ).fetchone()
    if exists:
        return jsonify(error="That email or account ID is already registered"), 409

    embeddings = []
    if role == "student":
        photos = [core.decode(photo.read()) for photo in request.files.getlist("photos")]
        embeddings, _ = core.extract_embeddings(photos)
        if len(embeddings) < 3:
            return jsonify(error="Could not detect a face in all three angle photos; recapture them in good light"), 400

    try:
        with account_db() as db:
            db.execute(
                "INSERT INTO accounts (role, account_id, email, password_hash, department) VALUES (?, ?, ?, ?, ?)",
                (role, account_id, email, generate_password_hash(password), department),
            )
        if role == "student":
            faces = core.load_db()
            faces[account_id] = embeddings
            core.save_db(faces)
    except sqlite3.IntegrityError:
        return jsonify(error="That email or account ID is already registered"), 409
    except Exception:
        if role == "student":
            with account_db() as db:
                db.execute("DELETE FROM accounts WHERE email = ?", (email,))
        raise
    return jsonify(ok=True), 201


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or request.form
    role = (data.get("role") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    account_id = (data.get("account_id") or "").strip()
    if role not in {"student", "teacher"}:
        return jsonify(error="Choose student or teacher sign in"), 400
    with account_db() as db:
        account = db.execute(
            "SELECT role, account_id, email, password_hash, department, sheet_id FROM accounts WHERE email = ? AND role = ?",
            (email, role),
        ).fetchone()
    if not account or not check_password_hash(account[3], password) or (role == "teacher" and account[1] != account_id):
        return jsonify(error="Email, ID, or password is incorrect"), 401
    session.clear()
    session.update(role=account[0], account_id=account[1], email=account[2], department=account[4], sheet_id=account[5])
    return jsonify(ok=True, role=account[0], account_id=account[1], department=account[4], sheet_id=account[5])


@app.get("/api/me")
def me():
    if not session.get("role"):
        return jsonify(account=None)
    return jsonify(account={key: session.get(key) for key in ("role", "account_id", "email", "department")})


@app.get("/api/teacher/sheets")
@require_role("teacher")
def teacher_sheets():
    with account_db() as db:
        rows = db.execute("""SELECT ts.id, ts.class_name, ts.sheet_id
            FROM teacher_sheets ts JOIN accounts a ON a.id = ts.teacher_account_id
            WHERE a.role = 'teacher' AND a.account_id = ? ORDER BY ts.class_name""",
            (session["account_id"],)).fetchall()
    return jsonify(sheets=[{"id": row[0], "class_name": row[1], "sheet_id": row[2]} for row in rows])


@app.post("/api/teacher/sheets")
@require_role("teacher")
def save_teacher_sheet():
    data = request.get_json(silent=True) or {}
    class_name = (data.get("class_name") or "").strip()
    sheet_id = (data.get("sheet_id") or "").strip()
    if not class_name or len(class_name) > 100 or not sheet_id or len(sheet_id) > 200:
        return jsonify(error="Enter a class name and Google Sheets ID"), 400
    with account_db() as db:
        db.execute("""INSERT INTO teacher_sheets (teacher_account_id, class_name, sheet_id)
            SELECT id, ?, ? FROM accounts WHERE role = 'teacher' AND account_id = ?
            ON CONFLICT (teacher_account_id, class_name) DO UPDATE SET sheet_id = excluded.sheet_id""",
            (class_name, sheet_id, session["account_id"]))
        row = db.execute("""SELECT ts.id FROM teacher_sheets ts JOIN accounts a ON a.id = ts.teacher_account_id
            WHERE a.role = 'teacher' AND a.account_id = ? AND ts.class_name = ?""",
            (session["account_id"], class_name)).fetchone()
    return jsonify(ok=True, id=row[0], class_name=class_name, sheet_id=sheet_id)


@app.delete("/api/teacher/sheets/<int:sheet_row_id>")
@require_role("teacher")
def remove_teacher_sheet(sheet_row_id):
    with account_db() as db:
        db.execute("""DELETE FROM teacher_sheets WHERE id = ? AND teacher_account_id =
            (SELECT id FROM accounts WHERE role = 'teacher' AND account_id = ?)""",
            (sheet_row_id, session["account_id"]))
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/student/attendance")
@require_role("student")
def student_attendance():
    with account_db() as db:
        sources = db.execute("""SELECT ts.class_name, ts.sheet_id, a.account_id
            FROM teacher_sheets ts JOIN accounts a ON a.id = ts.teacher_account_id
            WHERE a.role = 'teacher' ORDER BY a.account_id, ts.class_name""").fetchall()
    configured = [{"class_name": row[0], "sheet_id": row[1], "teacher_id": row[2]} for row in sources]
    return jsonify(attendance=sheets.student_attendance(session["account_id"], configured))


@app.get("/api/students")
@require_role("teacher")
def students():
    return jsonify([{"name": n, "photos": len(e)} for n, e in core.load_db().items()])


@app.post("/api/enroll")
@require_role("teacher")
def enroll():
    name = (request.form.get("name") or "").strip()
    files = request.files.getlist("photos")
    if not name or not files:
        return jsonify(error="Name and at least one photo are required"), 400
    added, skipped = core.enroll(name, [core.decode(f.read()) for f in files])
    return jsonify(added=added, skipped=skipped)


@app.delete("/api/students/<name>")
@require_role("teacher")
def remove(name):
    db = core.load_db()
    db.pop(name, None)
    core.save_db(db)
    return jsonify(ok=True)


@app.post("/api/recognize")
@require_role("teacher")
def recognize():
    f = request.files.get("photo")
    db = core.load_db()
    if not f:
        return jsonify(error="No photo uploaded"), 400
    if not db:
        return jsonify(error="Enroll students first"), 400
    raw = f.read()
    img = core.decode(raw)
    if img is None:
        return jsonify(error="Could not read image (use JPG or PNG)"), 400
    meta = core.extract_metadata(raw, request.form.get("last_modified"))
    faces, matches = core.recognize(img, db, float(request.form.get("threshold", 0.4)))
    vis = core.annotate(img, faces, matches)
    s = 1600 / max(vis.shape[:2])
    if s < 1:
        vis = cv2.resize(vis, None, fx=s, fy=s)
    b64 = base64.b64encode(cv2.imencode(".jpg", vis)[1]).decode()
    present = {m[0] for m in matches.values()}
    return jsonify(image="data:image/jpeg;base64," + b64, faces=len(faces), metadata=meta,
                   column=meta["taken_at"],
                   students=[{"name": n, "present": n in present} for n in db])


@app.post("/api/save")
@require_role("teacher")
def save():
    d = request.get_json(force=True)
    column, att = (d.get("column") or "").strip(), d.get("attendance") or {}
    if not column or not att:
        return jsonify(error="Missing column label or attendance"), 400
    try:
        sheet_row_id = int(d.get("sheet_target"))
    except (TypeError, ValueError):
        return jsonify(error="Choose a class sheet before saving attendance"), 400
    with account_db() as db:
        row = db.execute("""SELECT ts.sheet_id FROM teacher_sheets ts
            JOIN accounts a ON a.id = ts.teacher_account_id
            WHERE ts.id = ? AND a.role = 'teacher' AND a.account_id = ?""",
            (sheet_row_id, session["account_id"])).fetchone()
    if not row:
        return jsonify(error="That class sheet is not configured for your teacher account"), 400
    sheet_id = row[0]
    return jsonify(ok=True, url=sheets.mark(column, att, sheet_id=sheet_id))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
