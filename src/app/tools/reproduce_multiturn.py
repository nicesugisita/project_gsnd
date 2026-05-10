import requests
import time
import json
import uuid

BASE = "http://127.0.0.1:8000"
CONV_ID = "test-multiturn-1"

headers = {"Content-Type": "application/json"}

# First user query
payload1 = {
    "messages": [{"role": "user", "content": "창원에 사는데 치매 지원 사업 뭐 있어"}],
    "conv_id": CONV_ID,
    "stream": False
}
print("POST 1 payload:", payload1)
resp1 = requests.post(f"{BASE}/v1/chat/completions", headers=headers, json=payload1)
print("Response 1 status:", resp1.status_code)
try:
    print(json.dumps(resp1.json(), ensure_ascii=False, indent=2))
except Exception:
    print(resp1.text)

# wait a bit to allow background save
time.sleep(1)

# Follow-up short context-dependent question
payload2 = {
    "messages": [{"role": "user", "content": "더 알려줘"}],
    "conv_id": CONV_ID,
    "stream": False
}
print("POST 2 payload:", payload2)
resp2 = requests.post(f"{BASE}/v1/chat/completions", headers=headers, json=payload2)
print("Response 2 status:", resp2.status_code)
try:
    print(json.dumps(resp2.json(), ensure_ascii=False, indent=2))
except Exception:
    print(resp2.text)

# Now fetch history via internal service
print("\nFetching history from DB via internal service")
import sys
sys.path.insert(0, "src")
from app.conversation.history import get_chat_history_service
from app.core.config import Config

history_service = get_chat_history_service(Config)
hist = history_service.get_history(CONV_ID, None)
print(f"History length: {len(hist)}")
for i, m in enumerate(hist[-20:], start=max(1, len(hist)-19)):
    print(i, m.get('role'), m.get('content')[:200], m.get('preprocess') and 'HAS_PREPROCESS' or '')
