import os
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, g, jsonify
from dotenv import load_dotenv
import requests

# load .env if present
load_dotenv()

# --- CONFIG ---
DATABASE = "patients.db"
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")  # accept either name
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama3-70b-8192"  # you can change to available Groq model in your account
# ----------------------------------------------------------------

app = Flask(__name__)

# ---------- Database helpers ----------
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            age INTEGER,
            gender TEXT,
            phone TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER,
            role TEXT,
            content TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(patient_id) REFERENCES patients(id)
        );
    """)
    db.commit()

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

# ---------- Routes ----------
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        age = request.form.get("age", "").strip() or None
        gender = request.form.get("gender", "").strip() or None
        phone = request.form.get("phone", "").strip() or None

        if not name:
            return render_template("login.html", error="Name is required")

        db = get_db()
        cur = db.cursor()
        cur.execute(
            "INSERT INTO patients (name, age, gender, phone) VALUES (?,?,?,?)",
            (name, age, gender, phone)
        )
        db.commit()
        patient_id = cur.lastrowid
        return redirect(url_for("dashboard", patient_id=patient_id))
    return render_template("login.html")

@app.route("/dashboard/<int:patient_id>")
def dashboard(patient_id):
    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if not patient:
        return "Patient not found", 404
    return render_template("dashboard.html", patient=patient)

@app.route("/chat/<int:patient_id>")
def chat(patient_id):
    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if not patient:
        return "Patient not found", 404
    rows = db.execute("SELECT role, content, created_at FROM chats WHERE patient_id = ? ORDER BY created_at ASC", (patient_id,)).fetchall()
    history = [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows]
    return render_template("chat.html", patient=patient, history=history)

@app.route("/api/send_message", methods=["POST"])
def api_send_message():
    data = request.get_json() or {}
    patient_id = data.get("patient_id")
    user_message = (data.get("message") or "").strip()
    if not patient_id or not user_message:
        return jsonify({"error":"invalid input"}), 400

    db = get_db()
    cur = db.cursor()
    # Save user message
    cur.execute("INSERT INTO chats (patient_id, role, content) VALUES (?, 'user', ?)", (patient_id, user_message))
    db.commit()

    # Build recent conversation for context
    rows = db.execute("SELECT role, content FROM chats WHERE patient_id = ? ORDER BY created_at ASC LIMIT 40", (patient_id,)).fetchall()
    messages = []
    # system instruction first
    system_msg = {
        "role": "system",
        "content": (
            "You are a helpful medical assistant. Provide general information and safe suggestions. "
            "Do NOT give definitive diagnoses. If symptoms sound urgent or dangerous, advise the user to seek immediate professional care."
        )
    }
    messages.append(system_msg)
    for r in rows:
        role = "user" if r["role"] == "user" else "assistant"
        messages.append({"role": role, "content": r["content"]})
    # add current user message as last
    messages.append({"role":"user", "content": user_message})

    # Call Groq (OpenAI-compatible chat completions endpoint)
    if not GROQ_API_KEY:
        assistant_text = "AI key not configured on server. Ask admin to set GROQ_API_KEY env variable."
    else:
        payload = {
            "model": GROQ_MODEL,
            "messages": messages,
            "max_tokens": 400,
            "temperature": 0.2
        }
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        try:
            resp = requests.post(GROQ_ENDPOINT, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            j = resp.json()
            # response shape follows OpenAI-compatible: choices[0].message.content
            assistant_text = j.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not assistant_text:
                # fallback to top-level text (some endpoints vary)
                assistant_text = j.get("choices", [{}])[0].get("text", "") or "Sorry, the AI returned an empty response."
        except Exception as e:
            assistant_text = "Sorry, AI service error: " + str(e)

    # Save assistant reply
    cur.execute("INSERT INTO chats (patient_id, role, content) VALUES (?, 'assistant', ?)", (patient_id, assistant_text))
    db.commit()

    return jsonify({"reply": assistant_text})

@app.route("/records")
def records():
    db = get_db()
    rows = db.execute("SELECT id, name, age, gender, phone, created_at FROM patients ORDER BY created_at DESC").fetchall()
    return render_template("records.html", patients=rows)

if __name__ == "__main__":
    # init DB if needed
    if not os.path.exists(DATABASE):
        with app.app_context():
            init_db()
            print("Initialized DB.")
    app.run(debug=True)
