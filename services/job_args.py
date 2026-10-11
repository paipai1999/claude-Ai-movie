"""
Worker argument builder
=======================
Workers are called positionally, and queued jobs are stored as ``list(args)`` in SQLite, so the
argument ORDER is part of the persisted contract. Instead of hand-writing a tuple per engine
(which silently breaks when a worker signature changes), the order is read from the worker's own
signature and every value comes from ``core.options.resolve``.

Workers အတွက် argument များကို worker ၏ signature အတိုင်းသာ တည်ဆောက်သည် (လက်ဖြင့် tuple မရေးတော့ပါ)။
"""

import inspect
from typing import Any, Callable, List, Mapping

# worker parameter name -> option name (only where they differ)
_ALIASES = {
    "input_source": "input",
    "inputs_list": "inputs",
    "url": "input",
    "custom_filename": "project_name",
}


def worker_option_names(worker: Callable) -> List[str]:
    """Option names a worker consumes, in positional order (``job_id`` excluded)."""
    params = list(inspect.signature(worker).parameters)
    return [_ALIASES.get(p, p) for p in params if p != "job_id"]


def build_worker_args(worker: Callable, job_id: str, resolved: Mapping[str, Any], **extra: Any) -> tuple:
    """
    ``(job_id, *values)`` in the order of ``worker``'s signature.

    ``resolved`` is the output of ``core.options.resolve``; ``extra`` carries values that are not
    options (the batch ``inputs`` list). A parameter without a value is a programming error and
    fails loudly instead of being passed as ``None``.
    """
    values: List[Any] = [job_id]
    for name in worker_option_names(worker):
        if name in extra:
            values.append(extra[name])
        elif name in resolved:
            values.append(resolved[name])
        else:
            raise KeyError(f"{worker.__name__}() needs '{name}' but no option provides it")
    return tuple(values)
