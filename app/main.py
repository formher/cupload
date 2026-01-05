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
from cryptography.fernet import Fernet
import base64

app = Flask(__name__)

# Rate Limiter
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# Auto-Cleanup Job
def parse_ttl(ttl_str):
    if not ttl_str:
        return 24 * 3600  # Default 24h
    try:
        unit = ttl_str[-1].lower()
        value = int(ttl_str[:-1])
        if unit == 's': return value
        if unit == 'm': return value * 60
        if unit == 'h': return value * 3600
        if unit == 'd': return value * 86400
        return 24 * 3600
    except ValueError:
        return 24 * 3600

# Auto-Cleanup Job
def cleanup_old_files():
    now = time.time()
    count = 0
    
    if os.path.exists(UPLOAD_FOLDER):
        for folder_name in os.listdir(UPLOAD_FOLDER):
            folder_path = os.path.join(UPLOAD_FOLDER, folder_name)
            if os.path.isdir(folder_path):
                # Check for meta file
                expiry_time = None
                try:
                    for f_name in os.listdir(folder_path):
                        if f_name.endswith('.meta'):
                            with open(os.path.join(folder_path, f_name), 'r') as f:
                                meta = json.load(f)
                                expiry_time = meta.get('expiry_time')
                            break
                    
                    should_delete = False
                    if expiry_time:
                        if now > expiry_time:
                            should_delete = True
                    else:
                        # Fallback: delete if older than 24h
                        if os.path.getmtime(folder_path) < (now - 86400):
                            should_delete = True

                    if should_delete:
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
    
    # Check for TTL and Downloads
    ttl_str = request.headers.get('X-TTL')
    downloads_str = request.headers.get('X-Downloads')
    
    expiry_time = time.time() + parse_ttl(ttl_str)
    try:
        remaining_downloads = int(downloads_str) if downloads_str else 1
    except ValueError:
        remaining_downloads = 1

    meta_data = {
        'expiry_time': expiry_time,
        'remaining_downloads': remaining_downloads
    }

    if password:
        meta_data['password_hash'] = generate_password_hash(password)

    meta_path = file_path + '.meta'
    with open(meta_path, 'w') as f:
        f.write(json.dumps(meta_data))

    with open(file_path, 'wb') as f:
        f.write(request.data)
        
    return f"You can download your file at https://qurl.sh/{random_id}/{filename}\nQR Code: https://qurl.sh/qr/{random_id}/{filename}\nTry wget http://qurl.sh/{random_id}/{filename}\n"


def update_meta_cleanup(file_path, dir_path, meta_path):
    try:
        if os.path.exists(meta_path):
            with open(meta_path, 'r') as f:
                current_meta = json.load(f)
            
            remaining = current_meta.get('remaining_downloads', 1)
            if remaining > 1:
                current_meta['remaining_downloads'] = remaining - 1
                with open(meta_path, 'w') as f:
                    f.write(json.dumps(current_meta))
            else:
                shutil.rmtree(dir_path)
                # print(f"Deleted (Limits reached): {dir_path}") 
        else:
             shutil.rmtree(dir_path)
             # print(f"Deleted (Default): {dir_path}")

    except Exception as e:
        print(f"Cleanup failed: {e}")

@app.route('/<random_id>/<filename>', methods=['GET', 'POST'])
def serve_file(random_id, filename):
    dir_path = os.path.join(UPLOAD_FOLDER, random_id)
    file_path = os.path.join(dir_path, filename)
    meta_path = file_path + '.meta'

    if os.path.exists(file_path):
        # Start matching Metadata Logic
        meta_data = {}
        if os.path.exists(meta_path):
            with open(meta_path, 'r') as f:
                meta_data = json.load(f)

        # Check Expiry
        if 'expiry_time' in meta_data and time.time() > meta_data['expiry_time']:
            shutil.rmtree(dir_path, ignore_errors=True)
            abort(404)

        # Check Password Protection
        if 'password_hash' in meta_data:
            if request.method == 'POST':
                password_input = request.form.get('password')
                if not password_input or not check_password_hash(meta_data['password_hash'], password_input):
                    return render_template('password.html', error="Invalid Password"), 401
            else:
                return render_template('password.html')

        try:
            # Code Viewer Logic
            agent = request.user_agent.string.lower()
            is_cli = any(cli in agent for cli in ['curl', 'wget', 'httpie'])
            is_raw = request.args.get('raw') == 'true'
            
            ext = os.path.splitext(filename)[1].lower()
            supported_exts = [
                '.txt', '.py', '.js', '.html', '.css', '.json', '.yaml', '.yml', 
                '.sh', '.md', '.go', '.rs', '.c', '.cpp', '.h', '.java', '.rb', 
                '.php', '.sql', '.xml', '.log', '.ini', '.conf'
            ]
            
            if not is_cli and not is_raw and ext in supported_exts:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                    
                    # Trigger cleanup (count as view)
                    update_meta_cleanup(file_path, dir_path, meta_path)
                    
                    lang_map = {
                        '.py': 'python', '.js': 'javascript', '.sh': 'bash', 
                        '.md': 'markdown', '.go': 'go', '.rs': 'rust',
                        '.json': 'json', '.yaml': 'yaml', '.yml': 'yaml',
                        '.html': 'html', '.css': 'css', '.sql': 'sql',
                        '.java': 'java', '.c': 'c', '.cpp': 'cpp'
                    }
                    lang = lang_map.get(ext, 'none')
                    
                    return render_template('viewer.html', 
                                         filename=filename, 
                                         content=file_content, 
                                         language=lang)
                except UnicodeDecodeError:
                    pass

            # Default File Serving
            with open(file_path, 'rb') as f:
                file_data = f.read()

            response = make_response(file_data)
            response.headers['Content-Type'] = 'application/octet-stream'
            response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'

            @response.call_on_close
            def update_or_delete():
                update_meta_cleanup(file_path, dir_path, meta_path)

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

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)

@app.route('/secret', methods=['POST'])
@limiter.limit("10 per minute")
def create_secret():
    # Read raw text or form data
    data = request.get_data()
    if not data:
        return "No content provided\n", 400
        
    # Generate Key and encryption suite
    # We use Fernet (AES-128 CBC + HMAC) for simplicity and safety
    key = Fernet.generate_key() 
    f = Fernet(key)
    
    # Encrypt
    token = f.encrypt(data)
    
    # Store
    random_id = str(uuid.uuid4())[:12] # Longer ID for secrets
    dir_path = os.path.join(UPLOAD_FOLDER, 'secrets', random_id)
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, 'secret.enc')
    
    with open(file_path, 'wb') as file:
        file.write(token)
    
    # Return URL with Key (Key is URL-safe base64)
    # Fernet key is bytes, need to decode for URL
    key_str = key.decode('utf-8')
    
    return f"Secret Link (Burn after reading): https://qurl.sh/secret/{random_id}/{key_str}\n"

@app.route('/secret/<random_id>/<key>', methods=['GET'])
def get_secret(random_id, key):
    try:
        dir_path = os.path.join(UPLOAD_FOLDER, 'secrets', random_id)
        file_path = os.path.join(dir_path, 'secret.enc')
        
        if not os.path.exists(file_path):
            abort(404)
            
        # Decrypt
        try:
            f = Fernet(key.encode('utf-8'))
            with open(file_path, 'rb') as file:
                token = file.read()
            secret_data = f.decrypt(token)
        except Exception:
            return "Invalid Key or Corrupt Data", 400
            
        # BURN IT
        try:
             shutil.rmtree(dir_path)
             app.logger.info(f"Burned secret: {random_id}")
        except Exception as e:
            app.logger.error(f"Failed to burn secret {random_id}: {e}")
            
        return make_response(secret_data, {'Content-Type': 'text/plain'})
        
    except Exception as e:
        app.logger.error(f"Secret error: {e}")
        abort(404)