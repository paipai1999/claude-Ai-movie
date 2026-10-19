"""
Studio API (projects, script editor, quality, profiles, history)
================================================================
The dashboard's "studio" endpoints. They sit on top of ``services.projects`` / ``services.profiles``
(pure, unit-tested) and only translate their errors into HTTP responses. Authentication and the
same-origin guard are applied by the app-wide middleware in ``web_ui``.

    GET    /api/projects                     all projects with review counts and quality score
    GET    /api/projects/{name}/script       the editable narration script
    PUT    /api/projects/{name}/script       save edits (un-completes voice / render phases)
    POST   /api/projects/{name}/script/replace  find & replace across the script (preview or apply)
    POST   /api/projects/{name}/capcut       build capcut_package.zip (narration track + BOM SRT + guide)
    GET    /api/projects/{name}/capcut       download that package
    GET    /api/projects/{name}/quality      what still needs a human look (also saved as quality_report.json)
    GET    /api/jobs/history                 recent jobs
    GET    /api/storage                      disk usage of outputs / movies / temp
    GET/POST/PUT/DELETE /api/profiles[/{id}] channel profiles (Brand Kit)
    GET/PUT /api/dictionary                  pronunciation dictionary (spoken text only)
"""

import os
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from urllib.parse import quote
from pydantic import BaseModel, Field

from brain.sqlite_store import list_jobs
from core import pronunciation
from services import capcut_export
from services import profiles as profile_store
from services import projects as project_store

router = APIRouter(prefix="/api")

OUTPUTS_DIR = "outputs"
_STATUS_FOR_CODE = {"not_found": 404, "no_state": 409, "no_script": 409, "duplicate": 409, "limit": 409,
                    "not_ready": 409, "no_audio": 409, "encode_failed": 500}


def _http_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=_STATUS_FOR_CODE.get(getattr(exc, "code", ""), 400), detail=str(exc))


# ── projects ───────────────────────────────────────────────────────────────────
@router.get("/projects")
def list_all_projects():
    return {"projects": project_store.list_projects(OUTPUTS_DIR)}


@router.get("/projects/{name}/script")
def get_project_script(name: str):
    try:
        return project_store.load_script(project_store.resolve_project(OUTPUTS_DIR, name), name)
    except project_store.ProjectError as exc:
        raise _http_error(exc)


class ScriptEdit(BaseModel):
    index: int
    narration: Optional[str] = None
    gender: Optional[str] = None


class ScriptEditRequest(BaseModel):
    edits: List[ScriptEdit] = Field(default_factory=list)


@router.put("/projects/{name}/script")
def save_project_script(name: str, req: ScriptEditRequest):
    # Only the fields the caller actually sent are edits (an omitted narration is not "").
    edits = [e.model_dump(exclude_none=True) for e in req.edits]
    try:
        project_dir = project_store.resolve_project(OUTPUTS_DIR, name)
        result = project_store.save_script(project_dir, edits)
        result["script"] = project_store.load_script(project_dir, name)["summary"]
        return result
    except project_store.ProjectError as exc:
        raise _http_error(exc)


class ReplaceRequest(BaseModel):
    find: str
    replace: str = ""
    match_case: bool = False
    whole_word: bool = False
    preview: bool = False


@router.post("/projects/{name}/script/replace")
def replace_in_project_script(name: str, req: ReplaceRequest):
    try:
        project_dir = project_store.resolve_project(OUTPUTS_DIR, name)
        return project_store.replace_in_script(
            project_dir, req.find, req.replace, match_case=req.match_case, whole_word=req.whole_word, preview=req.preview)
    except project_store.ProjectError as exc:
        raise _http_error(exc)


@router.post("/projects/{name}/capcut")
def build_capcut_package(name: str):
    try:
        project_dir = project_store.resolve_project(OUTPUTS_DIR, name)
        result = capcut_export.export_capcut_package(project_dir, name)
    except project_store.ProjectError as exc:
        raise _http_error(exc)
    result["download"] = "/api/projects/" + quote(name, safe="") + "/capcut"
    return result


@router.get("/projects/{name}/capcut")
def download_capcut_package(name: str):
    try:
        project_dir = project_store.resolve_project(OUTPUTS_DIR, name)
    except project_store.ProjectError as exc:
        raise _http_error(exc)
    path = os.path.join(project_dir, capcut_export.PACKAGE_NAME)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Build the CapCut package first.")
    return FileResponse(path, media_type="application/zip", filename=f"{name}_capcut.zip")


@router.get("/projects/{name}/quality")
def get_project_quality(name: str):
    try:
        project_dir = project_store.resolve_project(OUTPUTS_DIR, name)
        report = project_store.write_quality_report(project_dir, name)
        if report is None:
            raise project_store.ProjectError("This project has no readable state.json.", code="no_state")
        return report
    except project_store.ProjectError as exc:
        raise _http_error(exc)


# ── history / storage ──────────────────────────────────────────────────────────
@router.get("/jobs/history")
def job_history(limit: int = Query(50, ge=1, le=200)):
    return {"jobs": list_jobs(limit)}


@router.get("/storage")
def storage():
    return project_store.storage_summary({"outputs": OUTPUTS_DIR, "movies": "movies", "temp": "temp"})


# ── profiles (Brand Kit) ───────────────────────────────────────────────────────
class ProfileRequest(BaseModel):
    name: str
    engine_mode: str = "recap"
    options: dict = Field(default_factory=dict)


@router.get("/profiles")
def get_profiles():
    return {"profiles": profile_store.list_profiles()}


@router.post("/profiles")
def create_profile(req: ProfileRequest):
    try:
        return profile_store.save_profile(req.name, req.engine_mode, req.options)
    except profile_store.ProfileError as exc:
        raise _http_error(exc)


@router.put("/profiles/{profile_id}")
def update_profile(profile_id: str, req: ProfileRequest):
    try:
        return profile_store.save_profile(req.name, req.engine_mode, req.options, profile_id=profile_id)
    except profile_store.ProfileError as exc:
        raise _http_error(exc)


@router.delete("/profiles/{profile_id}")
def remove_profile(profile_id: str):
    try:
        profile_store.delete_profile(profile_id)
        return {"status": "deleted"}
    except profile_store.ProfileError as exc:
        raise _http_error(exc)


# ── pronunciation dictionary ───────────────────────────────────────────────────
class DictionaryRequest(BaseModel):
    """Either structured ``entries`` or the editor ``text`` (one ``from => to`` per line)."""
    entries: Optional[List[Any]] = None
    text: Optional[str] = None


def _dictionary_payload(entries: list) -> dict:
    return {"entries": entries, "text": pronunciation.format_lines(entries), "count": len(entries)}


@router.get("/dictionary")
def get_dictionary():
    return _dictionary_payload(pronunciation.load_entries())


@router.put("/dictionary")
def save_dictionary(req: DictionaryRequest):
    try:
        raw = pronunciation.parse_lines(req.text) if req.text is not None else req.entries
        return _dictionary_payload(pronunciation.save_entries(raw))
    except pronunciation.DictionaryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
