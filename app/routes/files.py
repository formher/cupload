from flask import Blueprint, request, abort, render_template, current_app, stream_with_context
import os
import uuid
import json
import shutil
import time
import markdown as md
import bleach
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import limiter, uploads_total, downloads_total, upload_bytes, expiries_total
from app.utils import parse_ttl, update_meta_cleanup, stream_and_cleanup

MARKDOWN_ALLOWED_TAGS = list(bleach.sanitizer.ALLOWED_TAGS) + [
    'p', 'pre', 'code', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'br', 'hr', 'span', 'div', 'img', 'table', 'thead', 'tbody',
    'tr', 'th', 'td', 'del', 'sub', 'sup'
]
MARKDOWN_ALLOWED_ATTRS = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    'img': ['src', 'alt', 'title'],
    'a': ['href', 'title', 'rel'],
    'code': ['class'],
    'span': ['class'],
    'div': ['class'],
}

files_bp = Blueprint('files', __name__)

@files_bp.route('/<filename>', methods=['PUT'])
@limiter.limit("10 per minute")
def upload_file(filename):
    max_size = current_app.config['MAX_CONTENT_LENGTH']
    max_size_mb = max_size // (1024 * 1024)
    upload_folder = current_app.config['UPLOAD_FOLDER']

    content_length = request.content_length
    if content_length is not None and content_length > max_size:
        return f"File too large. Max allowed size is {max_size_mb}MB.\n", 413  # Payload Too Large

    # A piped upload (`... | curl -T - https://qurl.sh/name.log`) is sent with
    # chunked transfer encoding and carries no Content-Length. That is fine as
    # long as the WSGI server gives us a stream we can read to EOF — the size
    # ceiling is re-checked per chunk below regardless. Without that guarantee
    # Werkzeug hands back an empty stream and we would silently store a 0-byte
    # file, so those requests still get rejected.
    if content_length is None and not request.environ.get('wsgi.input_terminated'):
        return "Missing Content-Length header.\n", 411  # Length Required

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
        requested_downloads = int(downloads_str) if downloads_str else 1
    except ValueError:
        requested_downloads = 1
        
    max_downloads = current_app.config.get('MAX_DOWNLOADS', 100)
    remaining_downloads = min(requested_downloads, max_downloads)
    
    if requested_downloads > max_downloads:
         current_app.logger.warning(f"Clamped downloads from {requested_downloads} to {max_downloads} for {random_id}/{filename}")

    meta_data = {
        'expiry_time': expiry_time,
        'remaining_downloads': remaining_downloads
    }

    if password:
        meta_data['password_hash'] = generate_password_hash(password)

    meta_path = file_path + '.meta'
    with open(meta_path, 'w') as f:
        f.write(json.dumps(meta_data))

    bytes_written = 0
    chunk_size = 64 * 1024
    try:
        with open(file_path, 'wb') as f:
            while True:
                chunk = request.stream.read(chunk_size)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > max_size:
                    f.close()
                    shutil.rmtree(dir_path, ignore_errors=True)
                    return f"File too large. Max allowed size is {max_size_mb}MB.\n", 413
                f.write(chunk)
    except Exception as e:
        shutil.rmtree(dir_path, ignore_errors=True)
        current_app.logger.error(f"Upload failed for {random_id}/{filename}: {e}")
        return "Upload failed.\n", 500

    uploads_total.labels(kind='file').inc()
    upload_bytes.observe(bytes_written)
    current_app.logger.info(f"File uploaded: {random_id}/{filename} (Size: {bytes_written} bytes, TTL: {ttl_str}, Limit: {remaining_downloads}) from {request.remote_addr}")
        
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
            expiries_total.labels(reason='ttl').inc()
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
                '.sh', '.go', '.rs', '.c', '.cpp', '.h', '.java', '.rb',
                '.php', '.sql', '.xml', '.log', '.ini', '.conf'
            ]
            markdown_exts = ['.md', '.markdown']
            image_exts = ['.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp']
            pdf_exts = ['.pdf']

            supported_exts = code_exts + markdown_exts + image_exts + pdf_exts

            # Text is read fully into memory to be rendered into the template,
            # so large files skip the viewer and fall through to the streaming
            # raw path. Images/PDFs are fine: the viewer only embeds a ?raw=true
            # URL for those, it never reads the bytes here.
            text_exts = code_exts + markdown_exts
            renderable = (
                ext not in text_exts
                or os.path.getsize(file_path) <= current_app.config['MAX_VIEWER_BYTES']
            )

            if not is_cli and not is_raw and ext in supported_exts and renderable:
                # Use raw=true in template for media src

                # Determine Type
                file_type = 'code'
                if ext in markdown_exts:
                    file_type = 'markdown'
                elif ext in image_exts:
                    file_type = 'image'
                elif ext in pdf_exts:
                    file_type = 'pdf'

                # For code/markdown, read content. For media, we handle in template via src
                file_content = ""
                raw_content = ""
                lang = "none"

                if file_type in ('code', 'markdown'):
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            raw_content = f.read()
                        file_content = raw_content

                        if file_type == 'code':
                            lang_map = {
                                '.py': 'python', '.js': 'javascript', '.sh': 'bash',
                                '.go': 'go', '.rs': 'rust',
                                '.json': 'json', '.yaml': 'yaml', '.yml': 'yaml',
                                '.html': 'html', '.css': 'css', '.sql': 'sql',
                                '.java': 'java', '.c': 'c', '.cpp': 'cpp'
                            }
                            lang = lang_map.get(ext, 'none')
                        else:
                            rendered = md.markdown(
                                raw_content,
                                extensions=['fenced_code', 'tables', 'toc', 'sane_lists', 'codehilite'],
                                output_format='html5'
                            )
                            file_content = bleach.clean(
                                rendered,
                                tags=MARKDOWN_ALLOWED_TAGS,
                                attributes=MARKDOWN_ALLOWED_ATTRS,
                                strip=True
                            )
                    except UnicodeDecodeError:
                        # Fallback if binary detected in text ext
                        pass

                # Trigger cleanup (count as view) mechanism logic:
                if file_type in ('code', 'markdown'):
                    update_meta_cleanup(file_path, dir_path, meta_path)

                downloads_total.labels(mode='viewer').inc()
                current_app.logger.info(f"Viewer accessed: {random_id}/{filename} ({file_type}) by {request.remote_addr}")

                return render_template('viewer.html',
                                     filename=filename,
                                     content=file_content,
                                     raw_content=raw_content,
                                     language=lang,
                                     file_type=file_type)

            # Default File Serving (or ?raw=true) — streamed, never buffered.
            mime_types = {
                '.pdf': 'application/pdf',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.gif': 'image/gif',
                '.svg': 'image/svg+xml',
                '.webp': 'image/webp',
            }
            content_type = mime_types.get(ext, 'application/octet-stream')

            file_size = os.path.getsize(file_path)
            response = current_app.response_class(
                stream_with_context(
                    stream_and_cleanup(file_path, dir_path, meta_path)
                ),
                mimetype=content_type,
            )
            # Set explicitly: a generator body would otherwise go out chunked,
            # which costs curl/wget their progress bar on big files.
            response.headers['Content-Length'] = str(file_size)

            # Only force download for generic files, not media we want to view raw
            if not (is_raw and ext in image_exts + pdf_exts):
                # Standard curl/wget behavior
                response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'

            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'

            downloads_total.labels(mode='raw').inc()
            current_app.logger.info(f"File served: {random_id}/{filename} to {request.remote_addr} (Raw/Download)")

            return response
        except Exception as e:
            current_app.logger.error(f"Error serving {random_id}/{filename}: {e}")
            abort(500, f"Error serving file: {e}")
    else:
        current_app.logger.warning(f"File not found: {random_id}/{filename} requested by {request.remote_addr}")
        abort(404)
