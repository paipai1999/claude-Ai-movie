"""
Where per-installation settings files live
==========================================
``profiles.json`` (Brand Kit) and ``dictionary.json`` (pronunciation dictionary) are the user's own data, not
project data: they are git-ignored and must survive a Google Colab runtime reset. config.json, the database and
the cookies already live on Google Drive in that case, so these follow them.
"""

import os
from typing import Optional

DRIVE_DIR = "/content/drive/MyDrive/MovieRecapOutputs"


def settings_path(env_var: str, filename: str, drive_dir: Optional[str] = None) -> str:
    """
    The file to use: the path named by ``env_var`` if set, else ``filename`` inside the Google Drive folder when
    it exists (Colab), else ``filename`` in the working directory.
    """
    override = os.environ.get(env_var)
    if override:
        return override
    drive = DRIVE_DIR if drive_dir is None else drive_dir
    if os.path.isdir(drive):
        return os.path.join(drive, filename)
    return filename
