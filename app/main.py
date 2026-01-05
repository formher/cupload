from flask import Flask, request, abort, render_template, make_response, jsonify, redirect
import os
import uuid
import json
import yaml
import xml.dom.minidom


app = Flask(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.abspath(os.path.join(BASE_DIR, '../uploads'))
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/<filename>', methods=['PUT'])
def upload_file(filename):
    max_size = 50 * 1024 * 1024  # 5 MB

    content_length = request.content_length
    if content_length is None:
        return "Missing Content-Length header.\n", 411  # Length Required
    if content_length > max_size:
        return "File too large. Max allowed size is 50MB.\n", 413  # Payload Too Large

    random_id = str(uuid.uuid4())[:8]
    dir_path = os.path.join(UPLOAD_FOLDER, random_id)
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, filename)
    with open(file_path, 'wb') as f:
        f.write(request.data)
    return f"You can download your file at http://cupload.io/{random_id}/{filename}\nTry wget http://cupload.io/{random_id}/{filename}\n"


@app.route('/<random_id>/<filename>', methods=['GET'])
def serve_file(random_id, filename):
    dir_path = os.path.join(UPLOAD_FOLDER, random_id)
    file_path = os.path.join(dir_path, filename)

    if os.path.exists(file_path):
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
                    os.rmdir(dir_path)
                    app.logger.info(f"Deleted: {file_path} and {dir_path}")
                except Exception as e:
                    app.logger.error(f"Cleanup failed: {e}")

            return response
        except Exception as e:
            abort(500, f"Error serving file: {e}")
    else:
        abort(404)

@app.route('/pretty', methods=['POST'])
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

    return f"You can access your pretty-printed file at https://cupload.io/pretty/{random_id}/{uploaded_file.filename}\n"

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