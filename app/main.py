from flask import Flask, request, abort, render_template, make_response, jsonify, redirect
from werkzeug.security import generate_password_hash, check_password_hash
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from apscheduler.schedulers.background import BackgroundScheduler
import os
import uuid
import json
import yaml
import xml.dom.minidom
import qrcode
import io
import time
import shutil

app = Flask(__name__)

# Rate Limiter
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# Auto-Cleanup Job
def cleanup_old_files():
    now = time.time()
    cutoff = now - (24 * 3600)  # 24 hours ago
    count = 0
    
    if os.path.exists(UPLOAD_FOLDER):
        for folder_name in os.listdir(UPLOAD_FOLDER):
            folder_path = os.path.join(UPLOAD_FOLDER, folder_name)
            if os.path.isdir(folder_path):
                # Check modification time of folder
                if os.path.getmtime(folder_path) < cutoff:
                    try:
                        shutil.rmtree(folder_path)
                        count += 1
                    except Exception as e:
                        print(f"Error cleaning {folder_path}: {e}")
    if count > 0:
        print(f"Cleanup: Removed {count} expired folders.")

scheduler = BackgroundScheduler()
scheduler.add_job(func=cleanup_old_files, trigger="interval", hours=1)
scheduler.start()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.abspath(os.path.join(BASE_DIR, '../uploads'))
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route('/', methods=['GET'])
def index():
    agent = request.user_agent.string.lower()
    if any(cli in agent for cli in ['curl', 'wget', 'httpie']):
        return """
qurl.sh - Secure File Sharing
================================

Upload:
  curl -T file.txt https://qurl.sh

  # With Password
  curl -T file.txt -H "X-Password: secret" https://qurl.sh

Download:
  wget https://qurl.sh/<id>/file.txt
  curl -O https://qurl.sh/<id>/file.txt

QR Code (View on phone):
  https://qurl.sh/qr/<id>/file.txt

Pretty Print (JSON/YAML/XML):
  curl -F "file=@config.yaml" https://qurl.sh/pretty

Note: Files auto-delete after the first download. Max 50MB.
"""
    return render_template('index.html')

@app.route('/<filename>', methods=['PUT'])
@limiter.limit("10 per minute")
def upload_file(filename):
    max_size = 50 * 1024 * 1024  # 5 MB

    content_length = request.content_length
    if content_length is None:
        return "Missing Content-Length header.\n", 411  # Length Required
    if content_length > max_size:
        return "File too large. Max allowed size is 50MB.\n", 413  # Payload Too Large

    random_id = str(uuid.uuid4())[:8]
    dir_path = os.path.join(UPLOAD_FOLDER, random_id)
    dir_path = os.path.join(UPLOAD_FOLDER, random_id)
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, filename)

    # Check for password header
    password = request.headers.get('X-Password')
    if password:
        meta_path = file_path + '.meta'
        with open(meta_path, 'w') as f:
            f.write(json.dumps({'password_hash': generate_password_hash(password)}))

    with open(file_path, 'wb') as f:
        f.write(request.data)
    return f"You can download your file at https://qurl.sh/{random_id}/{filename}\nQR Code: https://qurl.sh/qr/{random_id}/{filename}\nTry wget http://qurl.sh/{random_id}/{filename}\n"


@app.route('/<random_id>/<filename>', methods=['GET', 'POST'])
def serve_file(random_id, filename):
    dir_path = os.path.join(UPLOAD_FOLDER, random_id)
    file_path = os.path.join(dir_path, filename)
    meta_path = file_path + '.meta'

    if os.path.exists(file_path):
        # Check for Password Protection
        if os.path.exists(meta_path):
            with open(meta_path, 'r') as f:
                meta = json.load(f)
            
            # If POST, check credentials
            if request.method == 'POST':
                password_input = request.form.get('password')
                if not password_input or not check_password_hash(meta['password_hash'], password_input):
                    return render_template('password.html', error="Invalid Password"), 401
            # If GET, show prompt
            else:
                return render_template('password.html')

        try:
            with open(file_path, 'rb') as f:
                file_data = f.read()

            response = make_response(file_data)
            response.headers['Content-Type'] = 'application/octet-stream'
            response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'

            @response.call_on_close
            def remove_file():
                try:
                    os.remove(file_path)
                    if os.path.exists(meta_path):
                        os.remove(meta_path)
                    os.rmdir(dir_path)
                    app.logger.info(f"Deleted: {file_path} and {dir_path}")
                except Exception as e:
                    app.logger.error(f"Cleanup failed: {e}")

            return response
        except Exception as e:
            abort(500, f"Error serving file: {e}")
    else:
        abort(404)

@app.route('/qr/<random_id>/<filename>', methods=['GET'])
def get_qr(random_id, filename):
    # Verify file exists first (but don't delete it)
    dir_path = os.path.join(UPLOAD_FOLDER, random_id)
    file_path = os.path.join(dir_path, filename)
    
    if not os.path.exists(file_path):
        abort(404)
        
    # Generate QR Code
    url = f"https://qurl.sh/{random_id}/{filename}"
    img = qrcode.make(url)
    
    # Save to buffer
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    
    return make_response(buf.getvalue(), {'Content-Type': 'image/png'})

@app.route('/pretty', methods=['POST'])
@limiter.limit("10 per minute")
def upload_pretty_file():
    if 'file' not in request.files:
        return "No file uploaded", 400
    uploaded_file = request.files['file']

    if uploaded_file.filename == '':
        return "No selected file", 400

    ext = os.path.splitext(uploaded_file.filename)[1].lower()
    if ext not in ['.json', '.yaml', '.yml', '.xml']:
        return "Only .json, .yaml, .yml, and .xml files are allowed", 400

    random_id = str(uuid.uuid4())[:8]
    dir_path = os.path.join(UPLOAD_FOLDER, random_id)
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, uploaded_file.filename)
    uploaded_file.save(file_path)

    return f"You can access your pretty-printed file at https://qurl.sh/pretty/{random_id}/{uploaded_file.filename}\n"

@app.route('/pretty/<random_id>/<filename>', methods=['GET'])
def render_pretty_file(random_id, filename):
    dir_path = os.path.join(UPLOAD_FOLDER, random_id)
    file_path = os.path.join(dir_path, filename)

    if not os.path.exists(file_path):
        abort(404)

    ext = os.path.splitext(filename)[1].lower()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            raw_content = f.read()

        if ext == '.json':
            parsed = json.loads(raw_content)
            content = json.dumps(parsed, indent=4)
        elif ext in ['.yaml', '.yml']:
            parsed = yaml.safe_load(raw_content)
            content = yaml.dump(parsed, sort_keys=False, indent=4)
        elif ext == '.xml':
            dom = xml.dom.minidom.parseString(raw_content)
            content = '\n'.join([line for line in dom.toprettyxml().split('\n') if line.strip()])
        else:
            return "Unsupported file format", 415

        return render_template('pretty.html', content=content, filename=filename)
    except Exception as e:
        return f"Error parsing file: {e}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)