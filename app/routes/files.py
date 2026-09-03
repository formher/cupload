from flask import (Blueprint, request, abort, render_template, current_app,
                   stream_with_context, make_response)
import os
import uuid
import json
import shutil
import time
import markdown as md
import bleach
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import limiter, uploads_total, downloads_total, upload_bytes, expiries_total
from app.utils import (parse_ttl, update_meta_cleanup, stream_and_cleanup,
                       is_bot, is_cli, human_size, human_duration,
                       resolve_upload_file, is_metadata_sidecar)

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

# Categories the browser viewer can render. Defined at module level because
# upload_file needs them too, to decide the default TTL and download count.
CODE_EXTS = [
    '.txt', '.py', '.js', '.html', '.css', '.json', '.yaml', '.yml',
    '.sh', '.go', '.rs', '.c', '.cpp', '.h', '.java', '.rb',
    '.php', '.sql', '.xml', '.log', '.ini', '.conf'
]
MARKDOWN_EXTS = ['.md', '.markdown']
IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp']
PDF_EXTS = ['.pdf']
VIEWABLE_EXTS = frozenset(CODE_EXTS + MARKDOWN_EXTS + IMAGE_EXTS + PDF_EXTS)

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

    # 64 bits. The 32 bits of the old 8-char id were the only thing standing
    # between an enumeration scan and somebody's upload. Existing links keep
    # working: lookup is a path stat, not a length check.
    random_id = uuid.uuid4().hex[:16]
    dir_path, file_path = resolve_upload_file(upload_folder, random_id, filename)
    if not file_path:
        current_app.logger.warning(
            f"Rejected upload path from {request.remote_addr}: {filename!r}")
        return "Invalid filename.\n", 400
    os.makedirs(dir_path, exist_ok=True)

    # Check for password header
    password = request.headers.get('X-Password')
    
    # Check for TTL and Downloads
    ttl_str = request.headers.get('X-TTL')
    downloads_str = request.headers.get('X-Downloads')
    
    # A browser-viewable file is consumed by being looked at, so burning it
    # after one download leaves the recipient (or the sender re-checking their
    # own link) with a dead URL. Those default to a week and a high count
    # instead. An explicit header always wins over both defaults.
    viewable = os.path.splitext(filename)[1].lower() in VIEWABLE_EXTS
    default_ttl = (current_app.config['VIEWABLE_DEFAULT_TTL_SECONDS'] if viewable
                   else 24 * 3600)
    default_downloads = (current_app.config['VIEWABLE_DEFAULT_DOWNLOADS'] if viewable
                         else 1)

    ttl_seconds = parse_ttl(ttl_str, default_ttl=default_ttl)
    expiry_time = time.time() + ttl_seconds
    try:
        requested_downloads = int(downloads_str) if downloads_str else default_downloads
    except ValueError:
        requested_downloads = default_downloads
        
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
        
    download_url = f"https://qurl.sh/{random_id}/{filename}"
    qr_url = f"https://qurl.sh/qr/{random_id}/{filename}"

    # The friendly banner below is worth nothing to a build script, which wants
    # one value it can assign to a variable. Scraping prose for it is brittle -
    # doubly so now that the prose has a header line - so offer the two shapes a
    # CI job actually wants. X-Format matches the X-TTL/X-Downloads convention
    # already used here; Accept is honoured too for anything speaking HTTP
    # properly. curl sends Accept: */* by default, which stays human-readable.
    fmt = (request.headers.get('X-Format') or '').strip().lower()
    if not fmt and 'application/json' in request.headers.get('Accept', ''):
        fmt = 'json'

    if fmt == 'url':
        return make_response(download_url + "\n",
                             {'Content-Type': 'text/plain; charset=utf-8'})

    if fmt == 'json':
        payload = {
            'url': download_url,
            'qr_url': qr_url,
            'filename': filename,
            'size_bytes': bytes_written,
            'expires_at': int(expiry_time),
            'expires_in_seconds': ttl_seconds,
            'remaining_downloads': remaining_downloads,
            'password_protected': bool(password),
        }
        return current_app.response_class(
            json.dumps(payload, indent=2) + "\n", mimetype='application/json')

    # Lead with an explicit confirmation: curl -T prints nothing of its own on
    # success, so without this the reply reads as output with no verdict
    # attached. The byte count is the useful half - it is how you tell a
    # complete upload from one that was cut short.
    downloads_label = "1 download" if remaining_downloads == 1 else f"{remaining_downloads} downloads"
    summary = [
        f"\u2714 Success - uploaded {filename} ({human_size(bytes_written)})",
        f"  Expires in {human_duration(ttl_seconds)} or after {downloads_label}, whichever comes first",
    ]
    if password:
        summary.append("  Password required to download")

    body = "\n".join(summary) + (
        f"\n\n"
        f"You can download your file at {download_url}\n"
        f"QR Code: {qr_url}\n"
        f"Try wget http://qurl.sh/{random_id}/{filename}\n"
    )
    # Without this Flask labels a bare string text/html.
    return make_response(body, {'Content-Type': 'text/plain; charset=utf-8'})

@files_bp.route('/<random_id>/<filename>', methods=['GET', 'POST'])
def serve_file(random_id, filename):
    # Crawlers and link unfurlers must never reach the download counter: a
    # single Slack unfurl or Bingbot crawl would consume the default one
    # allowed download and delete the file before the recipient opens it.
    if is_bot(request.user_agent.string):
        current_app.logger.info(
            f"Bot refused: {random_id}/{filename} ua={request.user_agent.string[:80]!r}"
        )
        abort(404)

    upload_folder = current_app.config['UPLOAD_FOLDER']
    dir_path, file_path = resolve_upload_file(upload_folder, random_id, filename)
    if not file_path:
        current_app.logger.warning(
            f"Rejected download path from {request.remote_addr}: {random_id!r}/{filename!r}")
        abort(404)
    if is_metadata_sidecar(file_path):
        current_app.logger.warning(
            f"Refused metadata sidecar {random_id}/{filename} from {request.remote_addr}")
        abort(404)

    meta_path = file_path + '.meta'

    if os.path.isfile(file_path):
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
            cli_client = is_cli(request.user_agent.string)
            is_raw = request.args.get('raw') == 'true'
            
            ext = os.path.splitext(filename)[1].lower()
            
            code_exts = CODE_EXTS
            markdown_exts = MARKDOWN_EXTS
            image_exts = IMAGE_EXTS
            pdf_exts = PDF_EXTS
            supported_exts = VIEWABLE_EXTS

            # Text is read fully into memory to be rendered into the template,
            # so large files skip the viewer and fall through to the streaming
            # raw path. Images/PDFs are fine: the viewer only embeds a ?raw=true
            # URL for those, it never reads the bytes here.
            text_exts = code_exts + markdown_exts
            renderable = (
                ext not in text_exts
                or os.path.getsize(file_path) <= current_app.config['MAX_VIEWER_BYTES']
            )

            if not cli_client and not is_raw and ext in supported_exts and renderable:
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
