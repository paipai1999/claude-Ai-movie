"""
Job Queue State (တန်းစီစာရင်း)
==============================
The sequential FIFO queue shared by the dashboard endpoints and the dispatcher thread in
`web_ui.py`. Only one job renders at a time; further requests wait here in arrival order.

NOTE: the queue lives in memory. Queued (not yet started) jobs are lost on a server restart;
the SQLite `jobs` table only keeps their status history.

ဤ module သည် တန်းစီနေသော အလုပ်များ၏ စာရင်းနှင့် Lock ကို သိမ်းဆည်းပေးသည်။
Server ပြန်စတင်ပါက မစတင်ရသေးသော တန်းစီအလုပ်များ ပျောက်သွားမည်ဖြစ်သည်။
"""

import threading
from typing import Any, Dict, List

# Each entry: {"job_id", "target", "args", "name", "source", "language", "tts_engine", ...}
job_queue: List[Dict[str, Any]] = []
queue_lock = threading.RLock()
