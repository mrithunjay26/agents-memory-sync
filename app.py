import csv
import io
import json
import os
import re

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents import all_agent_adapters, get_agent_adapter
from auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    create_session_cookie,
    hash_password,
    verify_password,
    verify_session_cookie,
)
from backfill import backfill_project
from dispatch import (
    build_recategorize_prompt,
    cancel_dispatch_job,
    redirect_dispatch_job,
    resume_dispatch_job,
    run_coordinated_jobs,
    run_dispatch_job,
    start_coordinated_jobs,
    start_dispatch_job,
)
from handoff import generate_handoff
from store import (
    create_context_note,
    create_user,
    delete_local_provider,
    delete_project_permission,
    delete_context_note,
    delete_dispatch_job,
    find_project_root,
    get_agent_models,
    get_conflicts,
    get_context_bundle,
    get_enterprise_policy,
    get_local_provider,
    get_project_access,
    get_dispatch_job,
    get_dispatch_interaction,
    get_user_by_username,
    has_any_user,
    list_audit_events,
    list_active_dispatch_jobs,
    list_dispatch_jobs,
    list_dispatch_interactions,
    list_dispatch_logs,
    list_events,
    list_local_providers,
    list_project_permissions,
    list_projects,
    list_users,
    purge_expired_data,
    record_audit_event,
    redact_stored_secrets,
    register_project,
    resolve_dispatch_interaction,
    set_enterprise_policy,
    set_project_permission,
    set_user_role,
    set_agent_model,
    update_context_event,
    update_context_settings,
    upsert_local_provider,
    verify_audit_chain,
)
from telemetry import get_telemetry_summary

app = FastAPI(title="AgentMemorySync")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def _revalidate_static(request: Request, call_next):
    # Ask browsers to revalidate assets (they still get fast 304s via ETag),
    # so pulling an update and refreshing always loads the new JS/CSS instead
    # of a stale cached copy.
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


class SignupBody(BaseModel):
    username: str
    password: str


class LoginBody(BaseModel):
    username: str
    password: str


class DispatchBody(BaseModel):
    project_path: str
    agent: str
    prompt: str
    allow_edits: bool = True


class RedirectBody(BaseModel):
    direction: str


class CoordinationTaskBody(BaseModel):
    label: str
    prompt: str
    agent: str
    model: str = ""
    allow_edits: bool = False


class CoordinationBody(BaseModel):
    tasks: list[CoordinationTaskBody]


class AddProjectBody(BaseModel):
    path: str


class ContextEventUpdateBody(BaseModel):
    included: bool | None = None
    pinned: bool | None = None
    context_summary: str | None = None
    reset_summary: bool = False
    category: str | None = None
    reset_category: bool = False


class ContextNoteCreateBody(BaseModel):
    project_path: str
    content: str
    category: str = "note"


class ContextSettingsBody(BaseModel):
    project_path: str
    recent_limit: int


class ContextRecategorizeBody(BaseModel):
    project_path: str
    agent: str
    instructions: str


class InteractionResponseBody(BaseModel):
    response: str


class AgentModelBody(BaseModel):
    agent: str
    model: str = ""


class LocalProviderBody(BaseModel):
    agent_id: str
    display_name: str
    base_url: str
    model: str
    api_key_env: str = ""


class AdminUserCreateBody(BaseModel):
    username: str
    password: str
    role: str = "member"


class AdminUserUpdateBody(BaseModel):
    role: str
    active: bool = True


class ProjectPermissionBody(BaseModel):
    username: str
    project_path: str
    access_level: str


class EnterprisePolicyBody(BaseModel):
    project_path: str
    retention_days: int = 0
    secret_redaction: bool = True
    scan_existing: bool = False


def _current_username(request: Request) -> str | None:
    cookie = request.cookies.get(SESSION_COOKIE)
    username = verify_session_cookie(cookie) if cookie else None
    user = get_user_by_username(username) if username else None
    if not user or not user["active"]:
        return None
    return username


def require_auth(request: Request) -> str:
    username = _current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username


def require_admin(username: str = Depends(require_auth)) -> str:
    user = get_user_by_username(username)
    if user and user["active"] and user["role"] == "admin":
        return username
    if not has_any_user():
        return username
    raise HTTPException(status_code=403, detail="Administrator access required.")


def _require_project_access(project_path: str, username: str, required: str = "viewer") -> None:
    _require_tracked_project(project_path)
    effective = get_project_access(username, project_path)
    rank = {"viewer": 1, "editor": 2, "operator": 3}
    if not effective or rank[effective] < rank[required]:
        raise HTTPException(status_code=403, detail=f"{required.title()} access to this project is required.")


def _audit(actor: str, action: str, project_path: str | None = None,
           target_type: str | None = None, target_id: str | None = None,
           details: dict | None = None) -> None:
    record_audit_event(actor, action, project_path, target_type, target_id, details)


def _visible_projects(username: str) -> list[dict]:
    return [project for project in list_projects()
            if get_project_access(username, project["project_path"])]


@app.get("/")
def index(request: Request):
    no_cache = {"Cache-Control": "no-store"}
    if not _current_username(request):
        return FileResponse(os.path.join(STATIC_DIR, "login.html"), headers=no_cache)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers=no_cache)


@app.get("/api/auth/status")
def auth_status():
    return {"setup_needed": not has_any_user()}


@app.get("/api/auth/whoami")
def auth_whoami(request: Request):
    username = _current_username(request)
    user = get_user_by_username(username) if username else None
    return {"username": username, "role": user["role"] if user else None}


@app.post("/api/auth/signup")
def signup(payload: SignupBody):
    if has_any_user():
        raise HTTPException(status_code=400, detail="Admin account already exists.")
    if not payload.username.strip() or not payload.password:
        raise HTTPException(status_code=400, detail="Username and password are required.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    create_user(payload.username.strip(), hash_password(payload.password))
    _audit(payload.username.strip(), "auth.initial_admin_created", target_type="user",
           target_id=payload.username.strip())
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        create_session_cookie(payload.username.strip()),
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE,
    )
    return response


@app.post("/api/auth/login")
def login(payload: LoginBody):
    user = get_user_by_username(payload.username.strip())
    if not user or not user["active"] or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    _audit(user["username"], "auth.login", target_type="user", target_id=user["username"])
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        create_session_cookie(user["username"]),
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE,
    )
    return response


@app.post("/api/auth/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/api/admin/users")
def api_admin_users(_admin: str = Depends(require_admin)):
    return list_users()


@app.post("/api/admin/users")
def api_admin_create_user(payload: AdminUserCreateBody, admin: str = Depends(require_admin)):
    username = payload.username.strip()
    if not username or len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Username and an 8+ character password are required.")
    try:
        create_user(username, hash_password(payload.password), payload.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if "unique" in str(exc).lower():
            raise HTTPException(status_code=409, detail="Username already exists.") from exc
        raise
    _audit(admin, "user.created", target_type="user", target_id=username, details={"role": payload.role})
    return {key: value for key, value in get_user_by_username(username).items() if key != "password_hash"}


@app.put("/api/admin/users/{username}")
def api_admin_update_user(
    username: str, payload: AdminUserUpdateBody, admin: str = Depends(require_admin)
):
    if username == admin and (payload.role != "admin" or not payload.active):
        raise HTTPException(status_code=400, detail="Administrators cannot demote or disable themselves.")
    try:
        user = set_user_role(username, payload.role, payload.active)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(admin, "user.updated", target_type="user", target_id=username,
           details={"role": payload.role, "active": payload.active})
    return {key: value for key, value in user.items() if key != "password_hash"}


@app.get("/api/admin/permissions")
def api_admin_permissions(
    project: str | None = Query(default=None), _admin: str = Depends(require_admin)
):
    return list_project_permissions(project)


@app.put("/api/admin/permissions")
def api_admin_set_permission(
    payload: ProjectPermissionBody, admin: str = Depends(require_admin)
):
    try:
        result = set_project_permission(
            payload.username, payload.project_path, payload.access_level, admin
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(admin, "project.permission_set", payload.project_path, "user", payload.username,
           {"access_level": payload.access_level})
    return result


@app.delete("/api/admin/permissions")
def api_admin_delete_permission(
    username: str = Query(...), project: str = Query(...), admin: str = Depends(require_admin)
):
    deleted = delete_project_permission(username, project)
    _audit(admin, "project.permission_removed", project, "user", username)
    return {"deleted": deleted}


@app.get("/api/admin/policy")
def api_admin_policy(project: str = Query(...), _admin: str = Depends(require_admin)):
    _require_tracked_project(project)
    return get_enterprise_policy(project)


@app.put("/api/admin/policy")
def api_admin_update_policy(
    payload: EnterprisePolicyBody, admin: str = Depends(require_admin)
):
    try:
        policy = set_enterprise_policy(
            payload.project_path, payload.retention_days, payload.secret_redaction, admin
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    redacted = redact_stored_secrets(payload.project_path) if payload.secret_redaction and payload.scan_existing else None
    purged = purge_expired_data(payload.project_path)
    _audit(admin, "project.policy_updated", payload.project_path, "policy", payload.project_path,
           {"retention_days": payload.retention_days, "secret_redaction": payload.secret_redaction,
            "scan_existing": payload.scan_existing, "redacted": redacted, "purged": purged})
    return {"policy": policy, "redacted": redacted, "purged": purged}


@app.post("/api/admin/retention/run")
def api_admin_run_retention(
    project: str | None = Query(default=None), admin: str = Depends(require_admin)
):
    if project:
        _require_tracked_project(project)
    result = purge_expired_data(project)
    _audit(admin, "retention.executed", project, "policy", project, result)
    return result


@app.get("/api/admin/audit/export")
def api_admin_export_audit(
    project: str | None = Query(default=None),
    format: str = Query(default="jsonl", pattern="^(jsonl|csv)$"),
    _admin: str = Depends(require_admin),
):
    events = list_audit_events(project)
    if format == "jsonl":
        content = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
        media_type = "application/x-ndjson"
    else:
        output = io.StringIO(newline="")
        fields = ["id", "actor", "action", "project_path", "target_type", "target_id",
                  "details", "previous_hash", "entry_hash", "created_at"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for event in events:
            row = dict(event)
            row["details"] = json.dumps(row["details"], sort_keys=True)
            writer.writerow(row)
        content = output.getvalue()
        media_type = "text/csv"
    headers = {"Content-Disposition": f'attachment; filename="agentmemorysync-audit.{format}"'}
    return StreamingResponse(iter([content]), media_type=media_type, headers=headers)


@app.get("/api/admin/audit/verify")
def api_admin_verify_audit(_admin: str = Depends(require_admin)):
    return verify_audit_chain()


@app.get("/api/projects")
def api_projects(user: str = Depends(require_auth)):
    return _visible_projects(user)


@app.post("/api/projects/add")
def api_add_project(payload: AddProjectBody, admin: str = Depends(require_admin)):
    raw = payload.path.strip().strip('"')
    if not raw:
        raise HTTPException(status_code=400, detail="Path is required.")
    if not os.path.isdir(raw):
        raise HTTPException(
            status_code=400, detail=f"Not a folder that exists on this machine: {raw}"
        )
    root = find_project_root(raw)
    register_project(root)
    _audit(admin, "project.added", root, "project", root)
    return {"project_path": root}


@app.post("/api/projects/backfill")
def api_backfill(payload: AddProjectBody, admin: str = Depends(require_admin)):
    root = find_project_root(payload.path.strip().strip('"'))
    register_project(root)
    result = backfill_project(root)
    _audit(admin, "project.backfilled", root, "project", root, result)
    return result


UPLOAD_SUBDIR = ".agentmemorysync_uploads"


@app.post("/api/upload")
async def api_upload(
    project: str = Form(...),
    file: UploadFile = File(...),
    user: str = Depends(require_auth),
):
    tracked = {p["project_path"] for p in list_projects()}
    if project not in tracked:
        raise HTTPException(status_code=400, detail="Unknown project.")
    _require_project_access(project, user, "operator")
    safe_name = os.path.basename(file.filename or "upload.bin")
    dest_dir = os.path.join(project, UPLOAD_SUBDIR)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, safe_name)
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    rel = os.path.join(UPLOAD_SUBDIR, safe_name).replace("\\", "/")
    _audit(user, "project.file_uploaded", project, "file", rel)
    return {"path": rel, "name": safe_name}


@app.get("/api/events")
def api_events(
    project: str | None = Query(default=None),
    since_id: int = Query(default=0),
    limit: int = Query(default=200, le=1000),
    user: str = Depends(require_auth),
):
    if project:
        _require_project_access(project, user)
        return list_events(project_path=project, limit=limit, since_id=since_id)
    visible = {item["project_path"] for item in _visible_projects(user)}
    return [event for event in list_events(project_path=None, limit=limit, since_id=since_id)
            if event["project_path"] in visible]


@app.get("/api/conflicts")
def api_conflicts(project: str | None = Query(default=None), user: str = Depends(require_auth)):
    if project:
        _require_project_access(project, user)
        return get_conflicts(project_path=project)
    visible = {item["project_path"] for item in _visible_projects(user)}
    return [conflict for conflict in get_conflicts() if conflict["project_path"] in visible]


@app.get("/api/telemetry")
def api_telemetry(project: str = Query(...), user: str = Depends(require_auth)):
    _require_project_access(project, user)
    return get_telemetry_summary(project)


def _require_tracked_project(project_path: str) -> None:
    tracked = {p["project_path"] for p in list_projects()}
    if project_path not in tracked:
        raise HTTPException(status_code=400, detail="project_path must be an already-tracked project.")


@app.get("/api/context")
def api_get_context(project: str = Query(...), user: str = Depends(require_auth)):
    _require_project_access(project, user)
    return get_context_bundle(project)


@app.patch("/api/context/events/{event_id}")
def api_update_context_event(
    event_id: int,
    payload: ContextEventUpdateBody,
    project: str = Query(...),
    user: str = Depends(require_auth),
):
    _require_project_access(project, user, "editor")
    try:
        update_context_event(
            project,
            event_id,
            included=payload.included,
            pinned=payload.pinned,
            context_summary=payload.context_summary,
            reset_summary=payload.reset_summary,
            category=payload.category,
            reset_category=payload.reset_category,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(user, "context.event_updated", project, "event", str(event_id))
    return get_context_bundle(project)


@app.post("/api/context/notes")
def api_create_context_note(payload: ContextNoteCreateBody, user: str = Depends(require_auth)):
    _require_project_access(payload.project_path, user, "editor")
    try:
        event_id = create_context_note(payload.project_path, payload.content, payload.category)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(user, "context.note_created", payload.project_path, "event", str(event_id))
    return get_context_bundle(payload.project_path)


@app.delete("/api/context/notes/{event_id}")
def api_delete_context_note(
    event_id: int, project: str = Query(...), user: str = Depends(require_auth)
):
    _require_project_access(project, user, "editor")
    try:
        delete_context_note(project, event_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(user, "context.note_deleted", project, "event", str(event_id))
    return get_context_bundle(project)


@app.put("/api/context/settings")
def api_update_context_settings(payload: ContextSettingsBody, user: str = Depends(require_auth)):
    _require_project_access(payload.project_path, user, "editor")
    try:
        update_context_settings(payload.project_path, payload.recent_limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(user, "context.settings_updated", payload.project_path, "settings", payload.project_path,
           {"recent_limit": payload.recent_limit})
    return get_context_bundle(payload.project_path)


@app.post("/api/context/recategorize")
def api_recategorize_context(
    payload: ContextRecategorizeBody,
    background_tasks: BackgroundTasks,
    user: str = Depends(require_auth),
):
    _require_project_access(payload.project_path, user, "operator")
    try:
        get_agent_adapter(payload.agent, "dispatch")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not payload.instructions.strip():
        raise HTTPException(status_code=400, detail="instructions must not be empty.")

    bundle = get_context_bundle(payload.project_path)
    if not bundle["entries"]:
        raise HTTPException(status_code=400, detail="No context entries to recategorize.")
    prompt = build_recategorize_prompt(bundle["entries"], payload.instructions)

    job_id = start_dispatch_job(payload.project_path, payload.agent, prompt, False)
    background_tasks.add_task(
        run_dispatch_job, job_id, payload.project_path, payload.agent, prompt, False
    )
    _audit(user, "context.recategorization_started", payload.project_path, "dispatch", job_id)
    return {"job_id": job_id}


@app.post("/api/dispatch")
def api_dispatch(
    payload: DispatchBody, background_tasks: BackgroundTasks, user: str = Depends(require_auth)
):
    tracked_paths = {p["project_path"] for p in list_projects()}
    if payload.project_path not in tracked_paths:
        raise HTTPException(
            status_code=400,
            detail="project_path must be an already-tracked project, not an arbitrary path.",
        )
    _require_project_access(payload.project_path, user, "operator")
    try:
        get_agent_adapter(payload.agent, "dispatch")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not payload.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt must not be empty.")

    job_id = start_dispatch_job(payload.project_path, payload.agent, payload.prompt, payload.allow_edits)
    background_tasks.add_task(
        run_dispatch_job, job_id, payload.project_path, payload.agent, payload.prompt, payload.allow_edits
    )
    _audit(user, "dispatch.started", payload.project_path, "dispatch", job_id,
           {"agent": payload.agent, "allow_edits": payload.allow_edits})
    return {"job_id": job_id}


@app.get("/api/dispatch/{job_id}")
def api_dispatch_status(job_id: str, user: str = Depends(require_auth)):
    job = get_dispatch_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="No such dispatch job.")
    _require_project_access(job["project_path"], user)
    return job


@app.post("/api/dispatch/{job_id}/cancel")
async def api_cancel_dispatch(job_id: str, user: str = Depends(require_auth)):
    job = get_dispatch_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="No such dispatch job.")
    _require_project_access(job["project_path"], user, "operator")
    canceled = await cancel_dispatch_job(job_id)
    _audit(user, "dispatch.canceled", job["project_path"], "dispatch", job_id)
    return {"canceled": canceled}


@app.delete("/api/dispatch/{job_id}")
async def api_delete_dispatch(job_id: str, user: str = Depends(require_auth)):
    job = get_dispatch_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="No such dispatch job.")
    _require_project_access(job["project_path"], user, "operator")
    if job["status"] in ("running", "waiting", "canceling"):
        await cancel_dispatch_job(job_id)
    delete_dispatch_job(job_id)
    _audit(user, "dispatch.deleted", job["project_path"], "dispatch", job_id)
    return {"deleted": True}


@app.post("/api/dispatch/{job_id}/redirect")
async def api_redirect_dispatch(
    job_id: str,
    payload: RedirectBody,
    background_tasks: BackgroundTasks,
    user: str = Depends(require_auth),
):
    source = get_dispatch_job(job_id)
    if not source:
        raise HTTPException(status_code=404, detail="No such dispatch job.")
    _require_project_access(source["project_path"], user, "operator")
    if not payload.direction.strip():
        raise HTTPException(status_code=400, detail="direction must not be empty.")
    redirected = await redirect_dispatch_job(job_id, payload.direction.strip())
    if not redirected:
        raise HTTPException(status_code=404, detail="No such dispatch job.")
    background_tasks.add_task(
        run_dispatch_job,
        redirected["job_id"],
        redirected["project_path"],
        redirected["agent"],
        payload.direction.strip(),
        redirected["allow_edits"],
        redirected["resume_session_id"],
    )
    _audit(user, "dispatch.redirected", source["project_path"], "dispatch", job_id,
           {"replacement_job_id": redirected["job_id"]})
    return {"job_id": redirected["job_id"]}


@app.post("/api/dispatch/{job_id}/coordinate")
def api_coordinate_dispatch(
    job_id: str,
    payload: CoordinationBody,
    background_tasks: BackgroundTasks,
    user: str = Depends(require_auth),
):
    source_job = get_dispatch_job(job_id)
    if not source_job:
        raise HTTPException(status_code=404, detail="No such dispatch job.")
    _require_project_access(source_job["project_path"], user, "operator")
    if len(payload.tasks) < 2 or len(payload.tasks) > 8:
        raise HTTPException(status_code=400, detail="A coordination batch needs 2 to 8 tasks.")

    tasks = []
    for task in payload.tasks:
        label = task.label.strip()
        prompt = task.prompt.strip()
        model = task.model.strip()
        if not label or len(label) > 120:
            raise HTTPException(status_code=400, detail="Each task needs a label of 1 to 120 characters.")
        if not prompt or len(prompt) > 12000:
            raise HTTPException(status_code=400, detail="Each task prompt needs 1 to 12000 characters.")
        if len(model) > 200 or "\x00" in model:
            raise HTTPException(status_code=400, detail="Model identifiers must be at most 200 characters.")
        try:
            get_agent_adapter(task.agent, "dispatch")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tasks.append(
            {
                "label": label,
                "prompt": prompt,
                "agent": task.agent,
                "model": model,
                "allow_edits": task.allow_edits,
            }
        )

    coordination = start_coordinated_jobs(source_job, tasks)
    background_tasks.add_task(run_coordinated_jobs, coordination["jobs"])
    _audit(user, "dispatch.coordinated", source_job["project_path"], "dispatch", job_id,
           {"coordination_id": coordination["coordination_id"], "task_count": len(tasks)})
    return {
        "coordination_id": coordination["coordination_id"],
        "job_ids": [job["job_id"] for job in coordination["jobs"]],
    }


@app.get("/api/dispatch/{job_id}/logs")
def api_dispatch_logs(
    job_id: str, since_id: int = Query(default=0), user: str = Depends(require_auth)
):
    job = get_dispatch_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="No such dispatch job.")
    _require_project_access(job["project_path"], user)
    return list_dispatch_logs(job_id, since_id=since_id)


@app.get("/api/dispatch")
def api_dispatch_history(project: str = Query(...), user: str = Depends(require_auth)):
    _require_project_access(project, user)
    jobs = list_dispatch_jobs(project)
    for job in jobs:
        snapshot = job.pop("context_snapshot", None) or ""
        job["context_snapshot_available"] = bool(snapshot)
        job["context_tokens"] = len(snapshot) // 4
    return jobs


@app.get("/api/interactions")
def api_interactions(
    project: str | None = Query(default=None),
    pending_only: bool = Query(default=False),
    user: str = Depends(require_auth),
):
    if project is not None:
        _require_project_access(project, user)
        return list_dispatch_interactions(project, pending_only=pending_only)
    visible = {item["project_path"] for item in _visible_projects(user)}
    return [item for item in list_dispatch_interactions(None, pending_only=pending_only)
            if item["project_path"] in visible]


@app.post("/api/interactions/{interaction_id}/respond")
def api_respond_to_interaction(
    interaction_id: str,
    payload: InteractionResponseBody,
    background_tasks: BackgroundTasks,
    user: str = Depends(require_auth),
):
    interaction = get_dispatch_interaction(interaction_id)
    if not interaction:
        raise HTTPException(status_code=404, detail="No such interaction.")
    job = get_dispatch_job(interaction["job_id"])
    if not job:
        raise HTTPException(status_code=404, detail="The interaction's deployment no longer exists.")
    _require_project_access(job["project_path"], user, "operator")
    try:
        resolved = resolve_dispatch_interaction(interaction_id, payload.response)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(resume_dispatch_job, job["id"], resolved["response"])
    _audit(user, "interaction.responded", job["project_path"], "interaction", interaction_id)
    return resolved


@app.get("/api/agents/active")
def api_active_agents(user: str = Depends(require_auth)):
    visible = {item["project_path"] for item in _visible_projects(user)}
    return [job for job in list_active_dispatch_jobs() if job["project_path"] in visible]


@app.get("/api/agents/status")
def api_agents_status(_user: str = Depends(require_auth)):
    def probe(resolver):
        try:
            return {"found": True, "path": resolver()}
        except Exception as exc:
            return {"found": False, "detail": str(exc)}

    return {
        adapter.agent_id: probe(adapter.resolve_binary)
        for adapter in all_agent_adapters()
        if adapter.capabilities.dispatch
    }


@app.get("/api/agents")
def api_agents(_user: str = Depends(require_auth)):
    return [adapter.public_metadata() for adapter in all_agent_adapters()]


@app.get("/api/agents/models")
def api_agent_models(_user: str = Depends(require_auth)):
    return get_agent_models()


@app.put("/api/agents/models")
def api_update_agent_model(payload: AgentModelBody, admin: str = Depends(require_admin)):
    try:
        get_agent_adapter(payload.agent, "dispatch")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = set_agent_model(payload.agent, payload.model)
    _audit(admin, "agent.model_updated", target_type="agent", target_id=payload.agent,
           details={"model": payload.model})
    return result


_BUILTIN_AGENT_IDS = ("claude-code", "codex")
_LOCAL_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")


@app.get("/api/agents/providers")
def api_local_providers(_user: str = Depends(require_auth)):
    return list_local_providers()


@app.post("/api/agents/providers")
def api_create_local_provider(payload: LocalProviderBody, admin: str = Depends(require_admin)):
    agent_id = payload.agent_id.strip().lower()
    if agent_id in _BUILTIN_AGENT_IDS:
        raise HTTPException(status_code=400, detail=f"{agent_id!r} is a built-in agent id.")
    if not _LOCAL_PROVIDER_ID_RE.match(agent_id):
        raise HTTPException(
            status_code=400,
            detail="agent_id must start with a letter and contain only lowercase letters, digits, - or _.",
        )
    base_url = payload.base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url must start with http:// or https://")
    display_name = payload.display_name.strip() or agent_id
    model = payload.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required.")
    result = upsert_local_provider(
        agent_id, display_name, base_url, model, payload.api_key_env.strip() or None
    )
    _audit(admin, "agent.provider_saved", target_type="agent", target_id=agent_id,
           details={"base_url": base_url, "model": model})
    return result


@app.delete("/api/agents/providers/{agent_id}")
def api_delete_local_provider(agent_id: str, admin: str = Depends(require_admin)):
    if not delete_local_provider(agent_id):
        raise HTTPException(status_code=404, detail="No such local provider.")
    _audit(admin, "agent.provider_deleted", target_type="agent", target_id=agent_id)
    return {"deleted": agent_id}


@app.post("/api/agents/providers/{agent_id}/health")
def api_local_provider_health(agent_id: str, _user: str = Depends(require_auth)):
    if not get_local_provider(agent_id):
        raise HTTPException(status_code=404, detail="No such local provider.")
    try:
        adapter = get_agent_adapter(agent_id)
        adapter.resolve_binary()
        return {"agent_id": agent_id, "reachable": True}
    except Exception as exc:
        return {"agent_id": agent_id, "reachable": False, "detail": str(exc)}


@app.post("/api/dispatch/{job_id}/handoff")
def api_handoff(job_id: str, user: str = Depends(require_auth)):
    job = get_dispatch_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="No such dispatch job.")
    _require_project_access(job["project_path"], user, "operator")
    result = generate_handoff(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="No such dispatch job.")
    _audit(user, "dispatch.handoff_created", job["project_path"], "dispatch", job_id)
    return result
