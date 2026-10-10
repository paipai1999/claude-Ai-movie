"""
Services Package (စနစ်ဝန်ဆောင်မှုများ)
=====================================
Shared runtime state and policies used by the Web UI server:

  - job_manager   : in-memory job registry (status / phase / retention)
  - queue_manager : sequential FIFO job queue
  - security          : authentication, same-origin and bind-address policy
  - process_registry  : which child processes (ffmpeg, demucs ...) belong to which job

ဤ package သည် Web UI server အတွက် အလုပ်စာရင်း၊ တန်းစီစနစ်နှင့် လုံခြုံရေးမူဝါဒများကို ထောက်ပံ့ပေးသည်။
"""

from services.job_manager import jobs, jobs_lock, JOB_RETENTION_SECONDS, has_running_job
from services.queue_manager import job_queue, queue_lock

__all__ = [
    "jobs",
    "jobs_lock",
    "JOB_RETENTION_SECONDS",
    "has_running_job",
    "job_queue",
    "queue_lock",
]
