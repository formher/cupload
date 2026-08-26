import os
import time
import json
import shutil
import uuid
from werkzeug.security import generate_password_hash

import logging

logger = logging.getLogger(__name__)

from flask import current_app
from app.extensions import expiries_total

# User-agent fragments for clients that fetch a URL without a human asking for
# that specific file: search crawlers, AI crawlers, SEO scrapers, archivers and
# link unfurlers. Every hit on /<id>/<filename> decrements the download counter
# and can delete the file, so an unfurl in a chat app or a crawl by Bingbot
# silently consumes somebody's one-and-only download before they read it. These
# clients are served a 404 and never reach the counter.
#
# Deliberately excludes curl/wget/httpie: those are real users, and serving them
# the raw bytes is the entire point of the service.
BOT_UA_TOKENS = (
    # Search engines
    'googlebot', 'bingbot', 'msnbot', 'slurp', 'duckduckbot', 'baiduspider',
    'yandex', 'sogou', 'exabot', 'petalbot', 'applebot', 'seznambot',
    'qwantify', 'naver', 'google-read-aloud', 'google-site-verification',
    # AI crawlers and assistants
    'gptbot', 'oai-searchbot', 'chatgpt-user', 'claudebot', 'claude-user',
    'claude-searchbot', 'anthropic-ai', 'perplexitybot', 'perplexity-user',
    'ccbot', 'google-extended', 'meta-externalagent', 'amazonbot',
    'bytespider', 'cohere-ai', 'diffbot', 'omgili', 'duckassistbot',
    'mistralai-user', 'timpibot', 'youbot', 'firecrawl',
    # SEO and commercial crawlers
    'ahrefsbot', 'semrushbot', 'mj12bot', 'dotbot', 'dataforseo', 'blexbot',
    'screaming frog', 'megaindex', 'serpstatbot', 'zoominfobot', 'barkrowler',
    # Link unfurlers and preview fetchers (chat apps, social networks)
    'slackbot', 'slack-imgproxy', 'twitterbot', 'facebookexternalhit', 'facebot',
    'linkedinbot', 'discordbot', 'telegrambot', 'whatsapp', 'skypeuripreview',
    'embedly', 'quora link preview', 'redditbot', 'pinterest', 'vkshare',
    'tumblr', 'flipboard', 'nuzzel', 'outbrain', 'bitlybot', 'viber',
    'googledocs', 'microsoftpreview', 'iframely',
    # Archivers
    'ia_archiver', 'archive.org_bot', 'wayback', 'heritrix',
)


CLI_UA_TOKENS = ('curl', 'wget', 'httpie')


def is_cli(user_agent):
    """True for terminal HTTP clients, which get plain text rather than HTML."""
    if not user_agent:
        return False
    agent = user_agent.lower()
    return any(token in agent for token in CLI_UA_TOKENS)


def is_bot(user_agent):
    """True for crawlers and link-preview fetchers, which must not consume a download."""
    if not user_agent:
        # A request with no User-Agent at all is far more likely to be a scraper
        # than a person; real browsers and curl always send one.
        return True
    agent = user_agent.lower()
    return any(token in agent for token in BOT_UA_TOKENS)


def parse_ttl(ttl_str):
    max_ttl = current_app.config.get('MAX_TTL_SECONDS', 604800)
    default_ttl = 24 * 3600

    if not ttl_str:
        return default_ttl
    try:
        unit = ttl_str[-1].lower()
        value = int(ttl_str[:-1])
        if unit == 's': result = value
        elif unit == 'm': result = value * 60
        elif unit == 'h': result = value * 3600
        elif unit == 'd': result = value * 86400
        else: result = default_ttl
    except ValueError:
        result = default_ttl
    
    return min(result, max_ttl)

def stream_and_cleanup(file_path, dir_path, meta_path, chunk_size=64 * 1024):
    """Yield a file in chunks, then run the download-counter cleanup.

    Reading a 500MB upload with f.read() costs 500MB of RSS per concurrent
    download, so the response body is generated lazily instead. The cleanup
    lives in a finally block so an aborted download still decrements the
    counter, matching the behaviour of the call_on_close hook it replaced.
    """
    try:
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk
    finally:
        update_meta_cleanup(file_path, dir_path, meta_path)


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
                expiries_total.labels(reason='downloads_exhausted').inc()
                logger.info(f"File deleted (Limit reached): {dir_path}")
        else:
             shutil.rmtree(dir_path)
             expiries_total.labels(reason='downloads_exhausted').inc()
             logger.info(f"File deleted (Default/No meta): {dir_path}")

    except Exception as e:
        logger.error(f"Cleanup failed for {dir_path}: {e}")

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
                        expiries_total.labels(reason='sweep').inc()
                        count += 1
                        logger.info(f"Cleanup job: Removed expired folder {folder_name}")
                except Exception as e:
                    logger.error(f"Error cleaning {folder_path}: {e}")
    
    if count > 0:
        logger.info(f"Cleanup job completed: Removed {count} expired folders.")
