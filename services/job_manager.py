"""
Job Registry (အလုပ်များ မှတ်တမ်းစာရင်း)
======================================
In-memory, thread-safe registry of every dashboard job (queued / running / done / error /
cancelled). It is the single shared state between the HTTP endpoints and the worker threads
in `web_ui.py`.

ဤ module သည် Web UI မှ စတင်သော အလုပ်အားလုံး၏ Status၊ Phase၊ အချိန်များကို
Memory ထဲတွင် Thread-safe ဖြစ်စွာ သိမ်းဆည်းပေးသည်။ (Process kill နှင့် SSE log streaming များကို
`web_ui.py` တွင်ပင် တိုက်ရိုက် ဆောင်ရွက်သည်။)
"""

import threading
from typing import Dict, Any

# jobs: job_id -> metadata (status, phase, buffer, created_at, name, source, project_dir, ...)
jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.RLock()

# Job retention policy: finished / cancelled jobs stay in memory for 2 hours (7200 seconds)
JOB_RETENTION_SECONDS: int = 7200


def has_running_job() -> bool:
    """
    True when any job currently has status == 'running'.
    လက်ရှိတွင် 'running' ဖြစ်နေသော အလုပ် ရှိ/မရှိ စစ်ဆေးပေးသည်။
    """
    with jobs_lock:
        return any(j.get("status") == "running" for j in jobs.values())
