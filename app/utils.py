import os
import time
import json
import shutil
import uuid
from werkzeug.security import generate_password_hash

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

def update_meta_cleanup(file_path, dir_path, meta_path):
    try:
        if os.path.exists(meta_path):
            with open(meta_path, 'r') as f:
                current_meta = json.load(f)
            
            remaining = current_meta.get('remaining_downloads', 1)
            # Check if this is the last download
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

def cleanup_old_files(upload_folder):
    now = time.time()
    count = 0
    
    if os.path.exists(upload_folder):
        for folder_name in os.listdir(upload_folder):
            folder_path = os.path.join(upload_folder, folder_name)
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
                    print(f"Error cleaning {folder_path}: {e}") # Logger to be injected?
    
    if count > 0:
        print(f"Cleanup: Removed {count} expired folders.")
