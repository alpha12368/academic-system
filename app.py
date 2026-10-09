"""Academic Performance & Early Warning System - Flask backend."""
import csv
import io
import random
import sqlite3
from collections import defaultdict

from flask import Flask, g, jsonify, render_template, request

DB = "data.db"
app = Flask(__name__)


# ---------------------------------------------------------------- database
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    with sqlite3.connect(DB) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS records(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id TEXT NOT NULL,
                name TEXT NOT NULL,
                subject TEXT NOT NULL,
                exam_no INTEGER NOT NULL,
                marks REAL NOT NULL,
                max_marks REAL NOT NULL DEFAULT 100,
                attendance REAL
            )"""
        )


# ---------------------------------------------------------------- analytics
def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def slope(ys):
    """Least-squares slope of ys against 0..n-1 (marks change per exam)."""
    n = len(ys)
    if n < 2:
        return 0.0
    mx, my = (n - 1) / 2, mean(ys)
    den = sum((x - mx) ** 2 for x in range(n))
    return sum((x - mx) * (y - my) for x, y in enumerate(ys)) / den


def level(score):
    return "High" if score >= 60 else "Medium" if score >= 30 else "Low"


def analyze_subject(subject, rows):
    rows = sorted(rows, key=lambda r: r["exam_no"])
    pcts = [r["marks"] / r["max_marks"] * 100 for r in rows]
    atts = [r["attendance"] for r in rows if r["attendance"] is not None]
    avg, latest, trend = mean(pcts), pcts[-1], slope(pcts)
    att = atts[-1] if atts else None
    predicted = None
    if len(pcts) >= 2:  # simple linear-regression forecast for next exam
        predicted = max(0, min(100, avg + trend * (len(pcts) - (len(pcts) - 1) / 2)))

    score, reasons, tips = 0.0, [], []
    if avg < 60:
        score += min(40, (60 - avg) / 60 * 80)
        reasons.append(f"Low average ({avg:.0f}%)")
        tips.append(f"Join remedial / doubt-clearing sessions for {subject} and revise the fundamentals.")
    if trend < -2:
        score += min(25, -trend / 10 * 25)
        reasons.append(f"Declining trend ({trend:.1f} pts per exam)")
        tips.append(f"Marks in {subject} are falling - review the latest topics and attempt a practice test this week.")
    if att is not None and att < 75:
        score += min(25, (75 - att) / 75 * 60)
        reasons.append(f"Low attendance ({att:.0f}%)")
        tips.append("Attendance is below 75% - attend regularly and catch up on missed lectures.")
    if len(pcts) >= 2 and latest < avg - 10:
        score += 10
        reasons.append("Sharp drop in latest exam")
        tips.append(f"Latest {subject} result dropped sharply - talk to the faculty to find the cause.")
    score = min(100, round(score))
    if not tips:
        tips.append(f"{subject}: on track - keep up the current study routine.")
    return {
        "subject": subject,
        "exams": [r["exam_no"] for r in rows],
        "scores": [round(p, 1) for p in pcts],
        "avg": round(avg, 1),
        "latest": round(latest, 1),
        "trend": round(trend, 2),
        "predicted": None if predicted is None else round(predicted, 1),
        "attendance": att,
        "risk_score": score,
        "risk": level(score),
        "reasons": reasons,
        "tips": tips,
    }


def analyze_all():
    rows = db().execute("SELECT * FROM records").fetchall()
    data = defaultdict(lambda: {"name": "", "subjects": defaultdict(list)})
    for r in rows:
        data[r["student_id"]]["name"] = r["name"]
        data[r["student_id"]]["subjects"][r["subject"]].append(dict(r))

    students, subj_stats = [], defaultdict(lambda: {"avgs": [], "high": 0})
    for sid, d in data.items():
        subs = [analyze_subject(s, rs) for s, rs in d["subjects"].items()]
        for s in subs:
            subj_stats[s["subject"]]["avgs"].append(s["avg"])
            subj_stats[s["subject"]]["high"] += s["risk"] != "Low"
        scores = [s["risk_score"] for s in subs]
        overall = round(0.6 * mean(scores) + 0.4 * max(scores))
        atts = [s["attendance"] for s in subs if s["attendance"] is not None]
        students.append(
            {
                "student_id": sid,
                "name": d["name"],
                "avg": round(mean([s["avg"] for s in subs]), 1),
                "attendance": round(mean(atts), 1) if atts else None,
                "risk_score": overall,
                "risk": level(overall),
                "weak_subjects": [s["subject"] for s in subs if s["risk"] != "Low"],
            }
        )
    students.sort(key=lambda s: -s["risk_score"])
    subjects = [
        {
            "subject": k,
            "avg": round(mean(v["avgs"]), 1),
            "students_at_risk": v["high"],
            "total": len(v["avgs"]),
        }
        for k, v in subj_stats.items()
    ]
    subjects.sort(key=lambda s: s["avg"])
    counts = {lv: sum(s["risk"] == lv for s in students) for lv in ("Low", "Medium", "High")}
    return {
        "kpis": {
            "students": len(students),
            "avg": round(mean([s["avg"] for s in students]), 1),
            "high": counts["High"],
            "medium": counts["Medium"],
        },
        "distribution": counts,
        "students": students,
        "subjects": subjects,
    }


# ---------------------------------------------------------------- routes
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/summary")
def summary():
    return jsonify(analyze_all())


@app.route("/api/student/<sid>")
def student(sid):
    rows = db().execute("SELECT * FROM records WHERE student_id=?", (sid,)).fetchall()
    if not rows:
        return jsonify({"error": "not found"}), 404
    by_sub = defaultdict(list)
    for r in rows:
        by_sub[r["subject"]].append(dict(r))
    subs = sorted(
        (analyze_subject(s, rs) for s, rs in by_sub.items()), key=lambda s: -s["risk_score"]
    )
    return jsonify({"student_id": sid, "name": rows[0]["name"], "subjects": subs})


def insert_rows(rows):
    db().executemany(
        "INSERT INTO records(student_id,name,subject,exam_no,marks,max_marks,attendance) VALUES(?,?,?,?,?,?,?)",
        rows,
    )
    db().commit()


@app.route("/api/upload", methods=["POST"])
def upload():
    """CSV columns: student_id,name,subject,exam_no,marks[,max_marks,attendance]"""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    reader = csv.DictReader(io.StringIO(f.read().decode("utf-8-sig")))
    reader.fieldnames = [h.strip().lower() for h in (reader.fieldnames or [])]
    rows, errors = [], []
    for i, r in enumerate(reader, start=2):
        try:
            att = (r.get("attendance") or "").strip()
            rows.append(
                (
                    r["student_id"].strip(),
                    r["name"].strip(),
                    r["subject"].strip(),
                    int(r["exam_no"]),
                    float(r["marks"]),
                    float(r.get("max_marks") or 100),
                    float(att) if att else None,
                )
            )
        except (KeyError, ValueError, AttributeError):
            errors.append(f"Row {i} skipped (missing/invalid values)")
    insert_rows(rows)
    return jsonify({"added": len(rows), "errors": errors[:10]})


@app.route("/api/record", methods=["POST"])
def add_record():
    j = request.get_json(force=True)
    try:
        insert_rows(
            [
                (
                    str(j["student_id"]).strip(),
                    str(j["name"]).strip(),
                    str(j["subject"]).strip(),
                    int(j["exam_no"]),
                    float(j["marks"]),
                    float(j.get("max_marks") or 100),
                    float(j["attendance"]) if j.get("attendance") not in (None, "") else None,
                )
            ]
        )
    except (KeyError, ValueError):
        return jsonify({"error": "Invalid input"}), 400
    return jsonify({"ok": True})


@app.route("/api/demo", methods=["POST"])
def demo():
    random.seed(7)
    names = ["Aarav", "Diya", "Rohan", "Sneha", "Karthik", "Meera", "Vikram", "Anjali",
             "Rahul", "Priya", "Arjun", "Kavya", "Nikhil", "Isha", "Harsha", "Lakshmi"]
    subjects = ["Maths", "Physics", "Chemistry", "English", "Programming"]
    rows = []
    for i, n in enumerate(names, start=1):
        base = random.randint(45, 90)
        for s in subjects:
            start = base + random.randint(-12, 12)
            drift = random.choice([-6, -3, 0, 0, 2, 4])  # some students decline
            att = max(40, min(100, random.gauss(80, 14)))
            for e in range(1, 6):
                m = max(5, min(100, start + drift * (e - 1) + random.randint(-5, 5)))
                rows.append((f"S{i:03d}", n, s, e, round(m), 100, round(att)))
    insert_rows(rows)
    return jsonify({"added": len(rows)})


@app.route("/api/reset", methods=["POST"])
def reset():
    db().execute("DELETE FROM records")
    db().commit()
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    app.run(debug=True)
