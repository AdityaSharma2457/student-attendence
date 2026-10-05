




import base64
import cv2
import io
import os
import secrets
import sqlite3
from contextlib import contextmanager
from functools import wraps
from flask import Flask, jsonify, request, send_file, session
from openpyxl import Workbook
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import core

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
            UNIQUE (role, account_id)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS teacher_classes (
            id INTEGER PRIMARY KEY,
            teacher_account_id INTEGER NOT NULL,
            class_name TEXT NOT NULL,
            UNIQUE (teacher_account_id, class_name)
        )""")
        legacy_sheets = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'teacher_sheets'"
        ).fetchone()
        if legacy_sheets:
            old_classes = db.execute(
                "SELECT teacher_account_id, class_name FROM teacher_sheets"
            ).fetchall()
            for teacher_id, class_name in old_classes:
                db.execute(
                    "INSERT OR IGNORE INTO teacher_classes (teacher_account_id, class_name) VALUES (?, ?)",
                    (teacher_id, class_name),
                )
            db.execute("DROP TABLE teacher_sheets")
        class_ids = db.execute("SELECT id FROM teacher_classes").fetchall()
        for (class_id,) in class_ids:
            db.execute(f"""CREATE TABLE IF NOT EXISTS attendance_class_{int(class_id)} (
                session_label TEXT NOT NULL,
                student_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('Present', 'Absent')),
                PRIMARY KEY (session_label, student_id)
            )""")


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
            "SELECT role, account_id, email, password_hash, department FROM accounts WHERE email = ? AND role = ?",
            (email, role),
        ).fetchone()
    if not account or not check_password_hash(account[3], password) or (role == "teacher" and account[1] != account_id):
        return jsonify(error="Email, ID, or password is incorrect"), 401
    session.clear()
    session.update(role=account[0], account_id=account[1], email=account[2], department=account[4])
    return jsonify(ok=True, role=account[0], account_id=account[1], department=account[4])


@app.get("/api/me")
def me():
    if not session.get("role"):
        return jsonify(account=None)
    return jsonify(account={key: session.get(key) for key in ("role", "account_id", "email", "department")})


@app.get("/api/teacher/classes")
@require_role("teacher")
def teacher_classes():
    with account_db() as db:
        rows = db.execute("""SELECT tc.id, tc.class_name
            FROM teacher_classes tc JOIN accounts a ON a.id = tc.teacher_account_id
            WHERE a.role = 'teacher' AND a.account_id = ? ORDER BY tc.class_name""",
            (session["account_id"],)).fetchall()
    return jsonify(classes=[{"id": row[0], "class_name": row[1]} for row in rows])


def owned_teacher_class(db, class_id):
    return db.execute("""SELECT tc.id, tc.class_name FROM teacher_classes tc
        JOIN accounts a ON a.id = tc.teacher_account_id
        WHERE tc.id = ? AND a.role = 'teacher' AND a.account_id = ?""",
        (class_id, session["account_id"])).fetchone()


@app.get("/api/teacher/classes/<int:class_id>/attendance")
@require_role("teacher")
def teacher_class_attendance(class_id):
    with account_db() as db:
        klass = owned_teacher_class(db, class_id)
        if not klass:
            return jsonify(error="Class not found"), 404
        records = db.execute(
            f"SELECT session_label, student_id, status FROM attendance_class_{int(class_id)} "
            "ORDER BY session_label, student_id"
        ).fetchall()
    return jsonify(class_name=klass[1], attendance=[
        {"session_label": row[0], "student_id": row[1], "status": row[2]} for row in records
    ])


@app.get("/api/teacher/classes/<int:class_id>/export.xlsx")
@require_role("teacher")
def export_teacher_class_attendance(class_id):
    with account_db() as db:
        klass = owned_teacher_class(db, class_id)
        if not klass:
            return jsonify(error="Class not found"), 404
        records = db.execute(
            f"SELECT session_label, student_id, status FROM attendance_class_{int(class_id)} "
            "ORDER BY session_label, student_id"
        ).fetchall()

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Attendance"
    worksheet.append(["Session", "Student ID", "Status"])
    for record in records:
        worksheet.append([excel_cell(value) for value in record])
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    worksheet.column_dimensions["A"].width = 24
    worksheet.column_dimensions["B"].width = 24
    worksheet.column_dimensions["C"].width = 16

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    filename = secure_filename(klass[1]) or "class"
    return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"{filename}-attendance.xlsx")


def excel_cell(value):
    text = str(value)
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


@app.post("/api/teacher/classes")
@require_role("teacher")
def create_teacher_classes():
    data = request.get_json(silent=True) or {}
    names = data.get("class_names")
    if not isinstance(names, list):
        names = [data.get("class_name", "")]
    class_names = list(dict.fromkeys(name.strip() for name in names if isinstance(name, str) and name.strip()))
    if not class_names or len(class_names) > 30 or any(len(name) > 100 for name in class_names):
        return jsonify(error="Enter up to 30 class names, each no longer than 100 characters"), 400
    with account_db() as db:
        teacher = db.execute(
            "SELECT id FROM accounts WHERE role = 'teacher' AND account_id = ?",
            (session["account_id"],),
        ).fetchone()
        for class_name in class_names:
            db.execute(
                "INSERT OR IGNORE INTO teacher_classes (teacher_account_id, class_name) VALUES (?, ?)",
                (teacher[0], class_name),
            )
        class_ids = db.execute("SELECT id FROM teacher_classes WHERE teacher_account_id = ?", (teacher[0],)).fetchall()
        for (class_id,) in class_ids:
            db.execute(f"""CREATE TABLE IF NOT EXISTS attendance_class_{int(class_id)} (
                session_label TEXT NOT NULL,
                student_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('Present', 'Absent')),
                PRIMARY KEY (session_label, student_id)
            )""")
    return jsonify(ok=True, created=len(class_names))


@app.delete("/api/teacher/classes/<int:class_id>")
@require_role("teacher")
def remove_teacher_class(class_id):
    with account_db() as db:
        row = owned_teacher_class(db, class_id)
        if row:
            db.execute(f"DROP TABLE IF EXISTS attendance_class_{int(class_id)}")
            db.execute("DELETE FROM teacher_classes WHERE id = ?", (class_id,))
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/student/attendance")
@require_role("student")
def student_attendance():
    attendance = []
    with account_db() as db:
        classes = db.execute("""SELECT tc.id, tc.class_name, a.account_id
            FROM teacher_classes tc JOIN accounts a ON a.id = tc.teacher_account_id
            WHERE a.role = 'teacher' ORDER BY a.account_id, tc.class_name""").fetchall()
        for class_id, class_name, teacher_id in classes:
            rows = db.execute(
                f"SELECT session_label, status FROM attendance_class_{int(class_id)} WHERE student_id = ? ORDER BY session_label",
                (session["account_id"],),
            ).fetchall()
            attendance.extend({
                "date": row[0], "status": row[1], "class_name": class_name, "teacher_id": teacher_id,
            } for row in rows)
    return jsonify(attendance=attendance)


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
                   session_label=meta["taken_at"],
                   students=[{"name": n, "present": n in present} for n in db])


@app.post("/api/save")
@require_role("teacher")
def save():
    d = request.get_json(force=True)
    session_label, attendance = (d.get("session_label") or "").strip(), d.get("attendance")
    if not session_label or not isinstance(attendance, dict) or not attendance:
        return jsonify(error="Missing session label or attendance"), 400
    if any(not isinstance(student_id, str) or not isinstance(present, bool)
           for student_id, present in attendance.items()):
        return jsonify(error="Attendance must map student IDs to present/absent values"), 400
    try:
        class_id = int(d.get("class_id"))
    except (TypeError, ValueError):
        return jsonify(error="Choose a class before saving attendance"), 400
    with account_db() as db:
        row = db.execute("""SELECT tc.id FROM teacher_classes tc
            JOIN accounts a ON a.id = tc.teacher_account_id
            WHERE tc.id = ? AND a.role = 'teacher' AND a.account_id = ?""",
            (class_id, session["account_id"])).fetchone()
        if not row:
            return jsonify(error="That class is not configured for your teacher account"), 400
        table_name = f"attendance_class_{class_id}"
        db.executemany(
            f"""INSERT INTO {table_name} (session_label, student_id, status) VALUES (?, ?, ?)
                ON CONFLICT (session_label, student_id) DO UPDATE SET status = excluded.status""",
            [(session_label, student_id, "Present" if present else "Absent")
             for student_id, present in attendance.items()],
        )
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
