from flask import (Blueprint, request, render_template, make_response, abort,
                   current_app, send_from_directory)
import qrcode
import io
import os
import uuid
import json
import yaml
# defusedxml, not xml.dom.minidom: the stdlib parser expands internal entities,
# so a 419-byte file with six levels of nested entities expanded to 10MB, and
# nine levels expands to 10GB. With a single gunicorn worker that is the whole
# service. defusedxml refuses entity and DTD declarations outright.
import defusedxml.minidom
from defusedxml.common import DefusedXmlException
import time
import shutil
from werkzeug.security import check_password_hash
from app.extensions import limiter, uploads_total, downloads_total, expiries_total
from werkzeug.utils import secure_filename
from app.utils import (is_bot, is_cli, update_meta_cleanup, resolve_upload_file,
                       is_metadata_sidecar)
from app.config import Config

misc_bp = Blueprint('misc', __name__)


class _AlwaysTty(io.StringIO):
    """qrcode.print_tty refuses to write anywhere that is not a terminal."""

    def isatty(self):
        return True


def render_qr_text(url, plain=False):
    """A QR code as terminal text, so it is usable over SSH with no image viewer."""
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)

    if plain:
        # Half-block form: ~500 bytes and survives being redirected to a file,
        # but it draws modules in the foreground colour, so it is only the right
        # way round on a dark terminal.
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
    else:
        # ANSI form: sets a white background and black modules explicitly, so it
        # scans identically on a light or a dark colour scheme. Larger, and the
        # escapes are noise if redirected, hence ?ascii=true above.
        buf = _AlwaysTty()
        qr.print_tty(out=buf)

    return f"{buf.getvalue()}\n{url}\n"

@misc_bp.route('/', methods=['GET'])
@limiter.limit("60 per minute")
def index():
    if is_cli(request.user_agent.string):
        max_size_mb = current_app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
        return f"""
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

Send to your phone (prints a QR in the terminal, then scan it):
  curl https://qurl.sh/qr/<id>/file.txt

Pretty Print (JSON/YAML/XML):
  curl -F "file=@config.yaml" https://qurl.sh/pretty

Encrypted Secrets:
  echo "secret" | curl -d @- https://qurl.sh/secret

Pipe anything (no temp file):
  kubectl logs my-pod | curl -T - https://qurl.sh/pod.log
  tar czf - ./dir     | curl -T - https://qurl.sh/dir.tar.gz

Full docs:
  https://qurl.sh/docs
  https://qurl.sh/llms.txt        (for AI assistants)
  https://qurl.sh/openapi.json    (machine-readable API)

Note: Files auto-delete after the first download. Max {max_size_mb}MB.
No signup, no API key, nothing to install.
"""
    max_size_mb = current_app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    return render_template('index.html', max_size_mb=max_size_mb)

@misc_bp.route('/docs', methods=['GET'])
@limiter.limit("60 per minute")
def docs():
    max_size_mb = current_app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    return render_template('docs.html', max_size_mb=max_size_mb)


@misc_bp.route('/transfer-sh-alternative', methods=['GET'])
@limiter.limit("60 per minute")
def transfer_sh_alternative():
    max_size_mb = current_app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    return render_template('transfer-sh-alternative.html', max_size_mb=max_size_mb)


# Crawlers and AI assistants look for these at the site root, not under
# /static/, so they get explicit routes rather than relying on the static mount.
def _serve_static(name, mimetype):
    response = send_from_directory(
        current_app.static_folder, name, mimetype=mimetype
    )
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response


@misc_bp.route('/robots.txt', methods=['GET'])
@limiter.exempt
def robots_txt():
    return _serve_static('robots.txt', 'text/plain')


@misc_bp.route('/sitemap.xml', methods=['GET'])
@limiter.exempt
def sitemap_xml():
    return _serve_static('sitemap.xml', 'application/xml')


@misc_bp.route('/llms.txt', methods=['GET'])
@limiter.exempt
def llms_txt():
    return _serve_static('llms.txt', 'text/plain')


@misc_bp.route('/llms-full.txt', methods=['GET'])
@limiter.exempt
def llms_full_txt():
    return _serve_static('llms-full.txt', 'text/plain')


@misc_bp.route('/openapi.json', methods=['GET'])
@limiter.exempt
def openapi_json():
    return _serve_static('openapi.json', 'application/json')


@misc_bp.route('/qr/<random_id>/<filename>', methods=['GET'])
def get_qr(random_id, filename):
    if is_bot(request.user_agent.string):
        abort(404)

    # Verify file exists first (but don't delete it)
    upload_folder = current_app.config['UPLOAD_FOLDER']
    _, file_path = resolve_upload_file(upload_folder, random_id, filename)

    if not file_path or not os.path.isfile(file_path) or is_metadata_sidecar(file_path):
        abort(404)
        
    url = f"https://qurl.sh/{random_id}/{filename}"

    # A terminal cannot display a PNG, which is exactly where this is most
    # useful: uploaded from an SSH session, wanted on a phone.
    if is_cli(request.user_agent.string):
        body = render_qr_text(url, plain=request.args.get('ascii') == 'true')
        return make_response(body, {'Content-Type': 'text/plain; charset=utf-8'})

    # Generate QR Code
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

    # Werkzeug does not sanitise multipart filenames, and this one is written
    # to disk: '../../x' walks out of the upload folder and '/tmp/x' ignores it
    # entirely. That is an arbitrary file write, as root in the container.
    safe_name = secure_filename(uploaded_file.filename)
    if not safe_name:
        return "Invalid filename.\n", 400

    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in ['.json', '.yaml', '.yml', '.xml']:
        return "Only .json, .yaml, .yml, and .xml files are allowed", 400

    # Pretty printing parses the whole document into memory, so it gets a much
    # tighter ceiling than a plain upload. Use PUT / for anything bigger.
    max_pretty = current_app.config['MAX_VIEWER_BYTES']
    if request.content_length and request.content_length > max_pretty:
        return f"File too large to pretty-print. Max is {max_pretty // (1024 * 1024)}MB.\n", 413

    random_id = uuid.uuid4().hex[:16]
    upload_folder = current_app.config['UPLOAD_FOLDER']
    dir_path, file_path = resolve_upload_file(upload_folder, random_id, safe_name)
    if not file_path:
        return "Invalid filename.\n", 400
    os.makedirs(dir_path, exist_ok=True)
    uploaded_file.save(file_path)

    uploads_total.labels(kind='pretty').inc()
    return f"You can access your pretty-printed file at https://qurl.sh/pretty/{random_id}/{safe_name}\n"

@misc_bp.route('/pretty/<random_id>/<filename>', methods=['GET', 'POST'])
def render_pretty_file(random_id, filename):
    if is_bot(request.user_agent.string):
        abort(404)

    upload_folder = current_app.config['UPLOAD_FOLDER']
    dir_path, file_path = resolve_upload_file(upload_folder, random_id, filename)
    if not file_path or not os.path.isfile(file_path) or is_metadata_sidecar(file_path):
        abort(404)
    meta_path = file_path + '.meta'

    # A file uploaded through PUT / carries a .meta; /pretty's own uploads do
    # not. Where one exists it has to be honoured exactly as on the download
    # route, because /<id>/<name>.json and /pretty/<id>/<name>.json read the
    # same bytes off disk. Without this, /pretty served password-protected and
    # expired files in full, unlimited times, without touching the counter.
    meta_data = {}
    if os.path.exists(meta_path):
        with open(meta_path, 'r') as f:
            meta_data = json.load(f)

    if 'expiry_time' in meta_data and time.time() > meta_data['expiry_time']:
        shutil.rmtree(dir_path, ignore_errors=True)
        expiries_total.labels(reason='ttl').inc()
        current_app.logger.info(f"File Expired (pretty access): {random_id}/{filename}")
        abort(404)

    if 'password_hash' in meta_data:
        if request.method == 'POST':
            password_input = request.form.get('password')
            if not password_input or not check_password_hash(meta_data['password_hash'], password_input):
                current_app.logger.warning(
                    f"Failed password attempt (pretty) for {random_id}/{filename} from {request.remote_addr}"
                )
                return render_template('password.html', error="Invalid Password"), 401
        else:
            return render_template('password.html')

    max_pretty = current_app.config['MAX_VIEWER_BYTES']
    if os.path.getsize(file_path) > max_pretty:
        return f"File too large to pretty-print. Max is {max_pretty // (1024 * 1024)}MB.\n", 413

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
            dom = defusedxml.minidom.parseString(raw_content)
            content = '\n'.join([line for line in dom.toprettyxml().split('\n') if line.strip()])
        else:
            return "Unsupported file format", 415

        # Only files that carry a .meta are metered. /pretty's own uploads have
        # none, and update_meta_cleanup deletes a directory outright when the
        # meta is missing, so calling it for those would destroy the document
        # on first view.
        if meta_data:
            update_meta_cleanup(file_path, dir_path, meta_path)

        downloads_total.labels(mode='pretty').inc()
        return render_template('pretty.html', content=content, filename=filename)
    except DefusedXmlException:
        current_app.logger.warning(
            f"Blocked XML entity/DTD declaration in {random_id}/{filename} "
            f"from {request.remote_addr}")
        return "This XML declares entities or a DTD, which are not accepted.\n", 400
    except Exception as e:
        # The exception text can carry document fragments and internal paths, so
        # it goes to the log rather than to the caller.
        current_app.logger.warning(f"Pretty-print failed for {random_id}/{filename}: {e}")
        return "Could not parse this file.\n", 400

@misc_bp.app_errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

