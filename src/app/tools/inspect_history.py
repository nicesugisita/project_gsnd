import sys
import json
# Ensure repository `src` is on sys.path when run from tools/
sys.path.insert(0, "src")
from app.core.config import Config
from app.conversation.history import get_chat_history_service

CONV_ID = "3914efdf-7f64-4814-a081-2221a6e879ae"

print(f"Using DB host: {Config.DB_HOST}:{Config.DB_PORT} user={Config.DB_USER} db={Config.DB_NAME}")

svc = get_chat_history_service(Config)
msgs = svc.get_history(CONV_ID, None)

print(f"Total messages: {len(msgs)}")
start = max(0, len(msgs)-20)
for idx, m in enumerate(msgs[start:], start=start+1):
    role = m.get('role')
    has_preprocess = 'preprocess' in m and isinstance(m.get('preprocess'), dict)
    has_meta_preprocess = False
    meta = m.get('metadata')
    if isinstance(meta, dict) and 'preprocess' in meta and isinstance(meta.get('preprocess'), dict):
        has_meta_preprocess = True
    print(idx, role, 'has_preprocess=', has_preprocess, 'has_meta_preprocess=', has_meta_preprocess)
    # Print small sample
    content = (m.get('content') or '').replace('\n', ' ')[:200]
    print('  content:', content)
    if has_preprocess:
        print('  preprocess.intent=', m['preprocess'].get('intent'))
    if has_meta_preprocess:
        print('  metadata.preprocess.intent=', meta['preprocess'].get('intent'))

if not msgs:
    print('No messages found for conv_id')
