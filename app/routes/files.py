from flask import Blueprint, request, abort, render_template, make_response, current_app
import os
import uuid
import json
import shutil
import time
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import limiter
from app.utils import parse_ttl, update_meta_cleanup

files_bp = Blueprint('files', __name__)

@files_bp.route('/<filename>', methods=['PUT'])
@limiter.limit("10 per minute")
def upload_file(filename):
    max_size = current_app.config['MAX_CONTENT_LENGTH']
    upload_folder = current_app.config['UPLOAD_FOLDER']

    content_length = request.content_length
    if content_length is None:
        return "Missing Content-Length header.\n", 411  # Length Required
    if content_length > max_size:
        return "File too large. Max allowed size is 50MB.\n", 413  # Payload Too Large

    random_id = str(uuid.uuid4())[:8]
    dir_path = os.path.join(upload_folder, random_id)
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
        
    current_app.logger.info(f"File uploaded: {random_id}/{filename} (Size: {content_length} bytes, TTL: {ttl_str}, Limit: {remaining_downloads}) from {request.remote_addr}")
        
    return f"You can download your file at https://qurl.sh/{random_id}/{filename}\nQR Code: https://qurl.sh/qr/{random_id}/{filename}\nTry wget http://qurl.sh/{random_id}/{filename}\n"

@files_bp.route('/<random_id>/<filename>', methods=['GET', 'POST'])
def serve_file(random_id, filename):
    upload_folder = current_app.config['UPLOAD_FOLDER']
    dir_path = os.path.join(upload_folder, random_id)
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
            current_app.logger.info(f"File Expired (during access): {random_id}/{filename}")
            abort(404)

        # Check Password Protection
        if 'password_hash' in meta_data:
            if request.method == 'POST':
                password_input = request.form.get('password')
                if not password_input or not check_password_hash(meta_data['password_hash'], password_input):
                    current_app.logger.warning(f"Failed password attempt for {random_id}/{filename} from {request.remote_addr}")
                    return render_template('password.html', error="Invalid Password"), 401
            else:
                return render_template('password.html')

        try:
            # Code/Media Viewer Logic
            agent = request.user_agent.string.lower()
            is_cli = any(cli in agent for cli in ['curl', 'wget', 'httpie'])
            is_raw = request.args.get('raw') == 'true'
            
            ext = os.path.splitext(filename)[1].lower()
            
            # Categories
            code_exts = [
                '.txt', '.py', '.js', '.html', '.css', '.json', '.yaml', '.yml', 
                '.sh', '.md', '.go', '.rs', '.c', '.cpp', '.h', '.java', '.rb', 
                '.php', '.sql', '.xml', '.log', '.ini', '.conf'
            ]
            image_exts = ['.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp']
            pdf_exts = ['.pdf']
            
            supported_exts = code_exts + image_exts + pdf_exts
            
            if not is_cli and not is_raw and ext in supported_exts:
                # Use raw=true in template for media src
                
                # Determine Type
                file_type = 'code'
                if ext in image_exts:
                    file_type = 'image'
                elif ext in pdf_exts:
                    file_type = 'pdf'
                
                # For code, read content. For media, we handle in template via src
                file_content = ""
                lang = "none"
                
                if file_type == 'code':
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            file_content = f.read()
                        
                        lang_map = {
                            '.py': 'python', '.js': 'javascript', '.sh': 'bash', 
                            '.md': 'markdown', '.go': 'go', '.rs': 'rust',
                            '.json': 'json', '.yaml': 'yaml', '.yml': 'yaml',
                            '.html': 'html', '.css': 'css', '.sql': 'sql',
                            '.java': 'java', '.c': 'c', '.cpp': 'cpp'
                        }
                        lang = lang_map.get(ext, 'none')
                    except UnicodeDecodeError:
                        # Fallback if binary detected in text ext
                        pass
                
                # Trigger cleanup (count as view) mechanism logic:
                if file_type == 'code':
                    update_meta_cleanup(file_path, dir_path, meta_path)
                    
                current_app.logger.info(f"Viewer accessed: {random_id}/{filename} ({file_type}) by {request.remote_addr}")

                return render_template('viewer.html', 
                                     filename=filename, 
                                     content=file_content, 
                                     language=lang,
                                     file_type=file_type)

            # Default File Serving (or ?raw=true)
            with open(file_path, 'rb') as f:
                file_data = f.read()

            response = make_response(file_data)
            
            # Set correct MIME for media
            if ext == '.pdf':
                response.headers['Content-Type'] = 'application/pdf'
            elif ext in ['.jpg', '.jpeg']:
                response.headers['Content-Type'] = 'image/jpeg'
            elif ext == '.png':
                response.headers['Content-Type'] = 'image/png'
            elif ext == '.gif':
                response.headers['Content-Type'] = 'image/gif'
            elif ext == '.svg':
                response.headers['Content-Type'] = 'image/svg+xml'
            elif ext == '.webp':
                response.headers['Content-Type'] = 'image/webp'
            else:
                response.headers['Content-Type'] = 'application/octet-stream'
                
            # Only force download for generic files, not media we want to view raw
            if is_raw and ext not in image_exts and ext not in pdf_exts:
                 response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            elif not is_raw:
                 # Standard curl/wget behavior
                 response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'

            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'

            @response.call_on_close
            def update_or_delete():
                update_meta_cleanup(file_path, dir_path, meta_path)
            
            current_app.logger.info(f"File served: {random_id}/{filename} to {request.remote_addr} (Raw/Download)")

            return response
        except Exception as e:
            current_app.logger.error(f"Error serving {random_id}/{filename}: {e}")
            abort(500, f"Error serving file: {e}")
    else:
        current_app.logger.warning(f"File not found: {random_id}/{filename} requested by {request.remote_addr}")
        abort(404)
