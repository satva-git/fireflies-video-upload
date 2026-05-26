import os
import uuid
import threading
import time
from pathlib import Path

import requests
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    send_from_directory,
    jsonify,
)
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32).hex())
app.config["MAX_CONTENT_LENGTH"] = 1.5 * 1024 * 1024 * 1024  # 1.5 GB

FIREFLIES_API_URL = "https://api.fireflies.ai/graphql"
FIREFLIES_API_KEY = os.environ.get("FIREFLIES_API_KEY", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE = 1.5 * 1024 * 1024 * 1024  # 1.5 GB
ALLOWED_EXTENSIONS = {"mp3", "mp4", "wav", "m4a", "ogg"}

# Public base URL for serving uploaded files to Fireflies.
# Set via env var (e.g., https://fireflies-upload.synctechnolabs.com)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def delete_file_later(filepath: Path, delay_seconds: int = 300):
    """Delete file after delay (default 5 min — enough for Fireflies to download)."""
    def _delete():
        time.sleep(delay_seconds)
        try:
            filepath.unlink(missing_ok=True)
        except OSError:
            pass
    t = threading.Thread(target=_delete, daemon=True)
    t.start()


def send_to_fireflies(file_url: str, title: str) -> dict:
    """Call Fireflies uploadAudio mutation."""
    mutation = """
    mutation uploadAudio($input: AudioUploadInput) {
        uploadAudio(input: $input) {
            success
            title
            message
        }
    }
    """
    variables = {
        "input": {
            "url": file_url,
            "title": title,
            "save_video": True,
        }
    }
    headers = {
        "Authorization": f"Bearer {FIREFLIES_API_KEY}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        FIREFLIES_API_URL,
        json={"query": mutation, "variables": variables},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        return {"success": False, "message": data["errors"][0].get("message", "Unknown error")}
    return data.get("data", {}).get("uploadAudio", {})


@app.route("/uploads/<filename>")
def serve_upload(filename):
    """Serve uploaded files so Fireflies can download them."""
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        session["authenticated"] = True
        return redirect(url_for("upload"))

    if request.method == "POST":
        password = request.form.get("password", "")
        if password == APP_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("upload"))
        flash("Incorrect password.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET", "POST"])
def upload():
    if APP_PASSWORD and not session.get("authenticated"):
        return redirect(url_for("login"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        file = request.files.get("file")

        if not title:
            flash("Please enter a title.", "error")
            return render_template("upload.html")

        if not file or file.filename == "":
            flash("Please select a file.", "error")
            return render_template("upload.html")

        if not allowed_file(file.filename):
            flash(f"Invalid file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}", "error")
            return render_template("upload.html")

        ext = file.filename.rsplit(".", 1)[1].lower()
        safe_name = f"{uuid.uuid4().hex}.{ext}"
        filepath = UPLOAD_DIR / safe_name
        file.save(filepath)

        file_size = filepath.stat().st_size
        if file_size > MAX_FILE_SIZE:
            filepath.unlink(missing_ok=True)
            flash("File too large. Maximum size is 1.5 GB.", "error")
            return render_template("upload.html")

        if not PUBLIC_BASE_URL:
            filepath.unlink(missing_ok=True)
            flash("Server configuration error: PUBLIC_BASE_URL not set.", "error")
            return render_template("upload.html")

        file_url = f"{PUBLIC_BASE_URL}/uploads/{safe_name}"

        try:
            result = send_to_fireflies(file_url, title)
        except Exception as e:
            filepath.unlink(missing_ok=True)
            flash(f"Failed to send to Fireflies: {e}", "error")
            return render_template("upload.html")

        if not result.get("success"):
            filepath.unlink(missing_ok=True)
            flash(f"Fireflies rejected the upload: {result.get('message', 'Unknown error')}", "error")
            return render_template("upload.html")

        # Schedule file deletion after Fireflies has time to download
        delete_file_later(filepath, delay_seconds=600)

        return render_template("thankyou.html", title=title)

    return render_template("upload.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
