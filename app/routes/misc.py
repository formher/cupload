from flask import Blueprint, request, render_template, make_response, abort, current_app
import qrcode
import io
import os
import uuid
import json
import yaml
import xml.dom.minidom
from app.extensions import limiter
from app.config import Config

misc_bp = Blueprint('misc', __name__)

@misc_bp.route('/', methods=['GET'])
def index():
    agent = request.user_agent.string.lower()
    if any(cli in agent for cli in ['curl', 'wget', 'httpie']):
        return """
qurl.sh - Terminal friendly file sharing
================================

Upload:
  curl -T file.txt https://qurl.sh

  # With Password
  curl -T file.txt -H "X-Password: secret" https://qurl.sh

  # TTL & Limits: (Max 7d and 100 downloads))

  curl -T file.txt -H "X-TTL: 1h" https://qurl.sh
  curl -T file.txt -H "X-Downloads: 5" https://qurl.sh

Download:
  wget https://qurl.sh/<id>/file.txt
  curl -O https://qurl.sh/<id>/file.txt

QR Code (View on phone):
  https://qurl.sh/qr/<id>/file.txt

Pretty Print (JSON/YAML/XML):
  curl -F "file=@config.yaml" https://qurl.sh/pretty

Encrypted Secrets:
  echo "secret" | curl -d @- https://qurl.sh/secret

Note: Files auto-delete after the first download. Max 50MB.
"""
    return render_template('index.html')

@misc_bp.route('/qr/<random_id>/<filename>', methods=['GET'])
def get_qr(random_id, filename):
    # Verify file exists first (but don't delete it)
    upload_folder = current_app.config['UPLOAD_FOLDER']
    dir_path = os.path.join(upload_folder, random_id)
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

@misc_bp.route('/pretty', methods=['POST'])
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
    upload_folder = current_app.config['UPLOAD_FOLDER']
    dir_path = os.path.join(upload_folder, random_id)
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, uploaded_file.filename)
    uploaded_file.save(file_path)

    return f"You can access your pretty-printed file at https://qurl.sh/pretty/{random_id}/{uploaded_file.filename}\n"

@misc_bp.route('/pretty/<random_id>/<filename>', methods=['GET'])
def render_pretty_file(random_id, filename):
    upload_folder = current_app.config['UPLOAD_FOLDER']
    dir_path = os.path.join(upload_folder, random_id)
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

