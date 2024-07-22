import asyncio
import concurrent.futures
import os
import shutil
import uuid
from collections import OrderedDict
from functools import wraps

import edge_tts
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
executor = concurrent.futures.ThreadPoolExecutor()

# Configurable settings
API_KEY = os.getenv("API_KEY")
MAX_TASKS = int(os.getenv("MAX_TASKS", "10"))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
PORT = int(os.getenv("PORT", "5000"))

# Use OrderedDict to maintain task order and limit
tasks = OrderedDict()


def cleanup_output_directory():
    if os.path.exists(OUTPUT_DIR):
        # Remove all files in the directory
        for filename in os.listdir(OUTPUT_DIR):
            file_path = os.path.join(OUTPUT_DIR, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print(f"Failed to delete {file_path}. Reason: {e}")
    else:
        # Create the directory if it doesn't exist
        os.makedirs(OUTPUT_DIR)


# Run cleanup when the app starts
cleanup_output_directory()


def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"error": "Invalid API key"}), 401
        return f(*args, **kwargs)

    return decorated_function


def delete_task_files(task_id):
    file_types = [".mp3", ".vtt"]
    for file_type in file_types:
        file_path = os.path.join(OUTPUT_DIR, f"{task_id}{file_type}")
        if os.path.exists(file_path):
            os.remove(file_path)


@app.route("/tts", methods=["POST"])
@require_api_key
def create_tts_task():
    if len(tasks) >= MAX_TASKS:
        # Remove the oldest task and its files
        oldest_task_id, _ = tasks.popitem(last=False)
        delete_task_files(oldest_task_id)

    data = request.json
    voice = data.get("voice", "en-GB-SoniaNeural")
    subtitle = data.get("subtitle", False)
    text = data.get("text", "Hello World!")
    task_id = str(uuid.uuid4())

    tasks[task_id] = {"status": "pending", "url": None, "error": None}
    base_url = request.url_root
    executor.submit(run_tts_task, task_id, text, voice, subtitle, base_url)

    return jsonify({"taskId": task_id})


@app.route("/tts/<task_id>", methods=["GET"])
@require_api_key
def get_tts_task_status(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "Invalid taskId"}), 404
    return jsonify(
        {"status": task["status"], "url": task["url"], "error": task["error"]}
    )


def run_tts_task(task_id, text, voice, subtitle, base_url):
    try:
        asyncio.run(generate_tts(task_id, text, voice, subtitle, base_url))
    except Exception as e:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = str(e)
        delete_task_files(task_id)  # Delete files if task errors out
    finally:
        # Move the task to the end of the OrderedDict to mark it as most recently used
        tasks.move_to_end(task_id)


async def generate_tts(task_id, text, voice, subtitle, base_url):
    output_file = os.path.join(OUTPUT_DIR, f"{task_id}.mp3")
    webvtt_file = os.path.join(OUTPUT_DIR, f"{task_id}.vtt") if subtitle else None

    communicate = edge_tts.Communicate(text, voice)
    submaker = edge_tts.SubMaker() if subtitle else None

    with open(output_file, "wb") as file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                file.write(chunk["data"])
            elif subtitle and chunk["type"] == "WordBoundary":
                submaker.create_sub((chunk["offset"], chunk["duration"]), chunk["text"])

    if subtitle:
        with open(webvtt_file, "w", encoding="utf-8") as file:
            file.write(submaker.generate_subs())

    tasks[task_id]["status"] = "done"
    tasks[task_id]["url"] = base_url + OUTPUT_DIR + "/" + os.path.basename(output_file)


@app.route("/" + OUTPUT_DIR + "/<filename>")
@require_api_key
def serve_file(filename):
    return send_from_directory(OUTPUT_DIR, filename)


if __name__ == "__main__":
    app.run(debug=True, port=PORT)
