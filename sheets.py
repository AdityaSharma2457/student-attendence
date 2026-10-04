import os
import gspread

SHEET_NAME = "Attendance"


def _worksheet(sheet_id=None):
    gc = gspread.service_account(filename=os.getenv("GOOGLE_CREDS", "credentials.json"))
    sh = gc.open_by_key(sheet_id or os.environ["SHEET_ID"])
    try:
        return sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(SHEET_NAME, rows=200, cols=60)


def mark(column_label, status, sheet_id=None):
    """Create a column named `column_label` (from the photo's metadata) if it doesn't
    exist, add any new students as rows, and write Present/Absent for everyone.
    Re-saving the same label updates that column instead of duplicating it."""
    ws = _worksheet(sheet_id)
    grid = ws.get_all_values() or [["Student"]]
    header = grid[0] or ["Student"]
    if column_label in header:
        col = header.index(column_label)
    else:
        header.append(column_label)
        col = len(header) - 1
    known = {r[0] for r in grid[1:] if r}
    for name in status:
        if name not in known:
            grid.append([name])
    rows = [header]
    for r in grid[1:]:
        r = r + [""] * (len(header) - len(r))
        if r[0] in status:
            r[col] = "Present" if status[r[0]] else "Absent"
        rows.append(r)
    ws.clear()
    ws.update(range_name="A1", values=rows)
    return ws.spreadsheet.url


def student_attendance(student_id, sources):
    """Collect the student's attendance entries from all configured class sheets."""
    attendance = []
    for source in sources:
        ws = _worksheet(source["sheet_id"])
        grid = ws.get_all_values() or []
        if len(grid) < 2:
            continue
        header = grid[0]
        student_row = next((row for row in grid[1:] if row and row[0].strip() == student_id), None)
        if student_row is None:
            continue
        for index, label in enumerate(header[1:], start=1):
            if label:
                attendance.append({
                    "date": label,
                    "status": student_row[index] if index < len(student_row) else "",
                    "class_name": source["class_name"],
                    "teacher_id": source["teacher_id"],
                })
    return attendance
