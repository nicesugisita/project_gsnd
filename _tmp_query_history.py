import mysql.connector
import sys, io, json

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

c = mysql.connector.connect(
    host='10.10.10.102', port=3306, user='diquest',
    password='1qw2#ER$', database='mariner', charset='utf8mb4'
)
cur = c.cursor()
cur.execute(
    "SELECT messages_json FROM gsnd_chat_history WHERE conv_id = %s ORDER BY id DESC LIMIT 1",
    ('test-low-income-002',)
)
row = cur.fetchone()
c.close()
if not row:
    print('NO ROW')
    sys.exit(0)
msgs = json.loads(row[0])
for m in reversed(msgs):
    if m.get('role') == 'assistant':
        c = m.get('content', '')
        print('=== ASSISTANT (len={}) ==='.format(len(c)))
        print(c[:3500])
        break
