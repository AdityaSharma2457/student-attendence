




import base64
import cv2
from flask import Flask, jsonify, request, send_file
import core, sheets

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024


@app.errorhandler(Exception)
def on_error(e):
    code = getattr(e, "code", 500)
    return jsonify(error=str(e)), code if isinstance(code, int) else 500


@app.get("/")
def index():
    return send_file("index.html")


@app.get("/api/students")
def students():
    return jsonify([{"name": n, "photos": len(e)} for n, e in core.load_db().items()])


@app.post("/api/enroll")
def enroll():
    name = (request.form.get("name") or "").strip()
    files = request.files.getlist("photos")
    if not name or not files:
        return jsonify(error="Name and at least one photo are required"), 400
    added, skipped = core.enroll(name, [core.decode(f.read()) for f in files])
    return jsonify(added=added, skipped=skipped)


@app.delete("/api/students/<name>")
def remove(name):
    db = core.load_db()
    db.pop(name, None)
    core.save_db(db)
    return jsonify(ok=True)


@app.post("/api/recognize")
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
def save():
    d = request.get_json(force=True)
    column, att = (d.get("column") or "").strip(), d.get("attendance") or {}
    if not column or not att:
        return jsonify(error="Missing column label or attendance"), 400
    return jsonify(ok=True, url=sheets.mark(column, att))


if __name__ == "__main__":
    app.run(debug=False, port=5000)
