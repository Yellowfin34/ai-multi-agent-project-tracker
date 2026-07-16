#!/usr/bin/env python3
"""AI Multi-Agent Project Tracker.

A self-contained SQLite + HTTP dashboard for coordinating multiple AI agents.
It tracks projects, project owners, handoff/production requests, activity logs,
calendar-style work history, project shorthand IDs, and token/cost reporting.

Security model:
- The first/admin agent can administer all projects.
- Non-admin agents can read everything, but can edit/archive only their own
  projects. They can submit requests for ownership or Production changes.
- Automation should authenticate with ``X-API-Key``. API keys are generated on
  first run into ``agent_tokens.json``; that file is intentionally ignored by
  git and must never be committed.

Configuration is via environment variables:
- PROJECT_TRACKER_DB=/path/to/project_tracker.db
- PROJECT_TRACKER_TOKEN_FILE=/path/to/agent_tokens.json
- PROJECT_TRACKER_HOST=127.0.0.1
- PROJECT_TRACKER_PORT=5055
- PROJECT_TRACKER_TOKEN_INPUT_RATE_PER_M=5.0         # dollars per million uncached input tokens
- PROJECT_TRACKER_TOKEN_CACHED_INPUT_RATE_PER_M=0.5  # dollars per million cached input tokens
- PROJECT_TRACKER_TOKEN_OUTPUT_RATE_PER_M=30.0       # dollars per million output tokens

Run locally with: ``python3 app.py`` then open http://127.0.0.1:5055/
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PROJECT_TRACKER_DB", APP_DIR / "project_tracker.db"))
HOST = os.environ.get("PROJECT_TRACKER_HOST", "0.0.0.0")
PORT = int(os.environ.get("PROJECT_TRACKER_PORT", "5055"))
TOKEN_PATH = Path(os.environ.get("PROJECT_TRACKER_TOKEN_FILE", APP_DIR / "agent_tokens.json"))

STAGES = ["Intake", "Planning", "In Progress", "Production"]
PRIORITIES = ["High", "Medium", "Low"]
REQUEST_TYPES = ["owner_change", "mark_production", "cross_project_update", "restore_archived", "permission_change"]

SEED_AGENTS = [
    # Generic sample agents for a fresh install. Replace these with your real
    # assistant/worker profiles in SQLite after first run if desired.
    {"id": "admin", "display_name": "Coordinator Agent", "profile_name": "default", "role_summary": "Primary coordinator and project tracker administrator.", "is_admin": 1, "can_mark_production": 1, "can_reassign_projects": 1, "can_change_permissions": 1, "allowed_tags": ["all"]},
    {"id": "research", "display_name": "Research Agent", "profile_name": "research", "role_summary": "Research and discovery support for assigned projects.", "is_admin": 0, "allowed_tags": ["research", "assigned"]},
    {"id": "operations", "display_name": "Operations Agent", "profile_name": "operations", "role_summary": "Operations and follow-up support for assigned projects.", "is_admin": 0, "allowed_tags": ["operations", "assigned"]},
]

SEED_PROJECTS = [
    # Sample projects are generic and safe for public repos. Project codes are
    # generated as AGENT-ACRONYM, for example COOR-PTS for Coordinator Agent +
    # Project Tracker Setup.
    {"name": "Project Tracker Setup", "stage": "In Progress", "priority": "High", "owner": "Coordinator Agent", "responsible_agent_id": "admin", "backup_agent_id": "operations", "summary": "Install and configure the shared multi-agent project tracker.", "completed_items": ["Application started", "SQLite database initialized", "Agent API keys generated locally"], "next_steps": ["Replace sample agents with your real agents", "Store agent_tokens.json securely", "Create each agent's first owned project"], "blockers": []},
    {"name": "Research Intake Workflow", "stage": "Planning", "priority": "Medium", "owner": "Research Agent", "responsible_agent_id": "research", "summary": "Example recurring intake project for an agent-owned lane.", "completed_items": ["Project created as an example"], "next_steps": ["Schedule a cron job that posts updates through /api/projects/{id}/updates"], "blockers": []},
    {"name": "Production Approval Example", "stage": "Intake", "priority": "Low", "owner": "Operations Agent", "responsible_agent_id": "operations", "summary": "Demonstrates that worker agents request Production instead of changing stage directly.", "completed_items": [], "next_steps": ["Submit a mark_production request when ready"], "blockers": ["Waiting for admin review"]},
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# Open a SQLite connection for one request/operation. WAL mode lets the
# app handle multiple readers while a writer is active.
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def normalize_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [line.strip(" -\t") for line in value.splitlines() if line.strip(" -\t")]
    return []


def parse_json_list(value):
    try:
        return json.loads(value or "[]")
    except Exception:
        return []


def nonnegative_int(value, default: int = 0) -> int:
    try:
        return max(0, int(value if value is not None else default))
    except (TypeError, ValueError):
        return max(0, int(default or 0))


def token_rates() -> dict:
    def rate(new_name: str, old_name: str | None, default: str) -> float:
        raw = os.environ.get(new_name)
        if raw is None and old_name:
            raw = os.environ.get(old_name)
        try:
            return float(raw if raw is not None else default)
        except (TypeError, ValueError):
            return float(default)
    return {
        "input_rate_per_million": rate("PROJECT_TRACKER_TOKEN_INPUT_RATE_PER_M", "PROJECT_TRACKER_INPUT_RATE", "5.00"),
        "cached_input_rate_per_million": rate("PROJECT_TRACKER_TOKEN_CACHED_INPUT_RATE_PER_M", None, "0.50"),
        "output_rate_per_million": rate("PROJECT_TRACKER_TOKEN_OUTPUT_RATE_PER_M", "PROJECT_TRACKER_OUTPUT_RATE", "30.00"),
    }


def estimated_token_cost(row: dict) -> float:
    rates = token_rates()
    input_tokens = nonnegative_int(row.get("input_tokens", 0))
    cached_input_tokens = min(input_tokens, nonnegative_int(row.get("cached_input_tokens", 0)))
    uncached_input_tokens = max(0, input_tokens - cached_input_tokens)
    output_tokens = nonnegative_int(row.get("output_tokens", 0))
    return round(
        (uncached_input_tokens / 1000000.0 * rates["input_rate_per_million"])
        + (cached_input_tokens / 1000000.0 * rates["cached_input_rate_per_million"])
        + (output_tokens / 1000000.0 * rates["output_rate_per_million"]),
        6,
    )


def add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def agent_from_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["allowed_tags"] = parse_json_list(item.get("allowed_tags"))
    item.pop("api_token", None)
    return item


def project_from_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key in ("completed_items", "next_steps", "blockers", "tags"):
        item[key] = parse_json_list(item.get(key))
    return item


def agent_code_prefix(conn: sqlite3.Connection, agent_id: str) -> str:
    row = conn.execute("SELECT display_name FROM agents WHERE id=?", (agent_id or "admin",)).fetchone()
    source = (row["display_name"] if row else agent_id or "admin").split("/")[0]
    letters = re.sub(r"[^A-Za-z0-9]", "", source).upper()
    return (letters + "XXXX")[:4]


def title_acronym(name: str) -> str:
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|[A-Z]+|\d+", name or "")
    letters = []
    for w in words:
        if not w:
            continue
        # Preserve CamelCase/ALLCAP chunks like ServiceHub -> SH, APIKit -> AK.
        caps = re.findall(r"[A-Z]", w)
        if len(caps) >= 2 and not w.isupper():
            letters.extend(caps)
        else:
            letters.append(w[0].upper())
    return ("".join(letters) or "PRJ")[:8]


# Build stable shorthand IDs such as ADMI-PTS. These codes let humans
# refer to a project quickly when asking an agent about it.
def make_project_code(conn: sqlite3.Connection, agent_id: str, name: str, project_id: int | None = None) -> str:
    base = f"{agent_code_prefix(conn, agent_id)}-{title_acronym(name)}"
    candidate = base
    n = 2
    while True:
        row = conn.execute("SELECT id FROM projects WHERE project_code=?", (candidate,)).fetchone()
        if not row or (project_id is not None and int(row["id"]) == int(project_id)):
            return candidate
        candidate = f"{base}-{n}"
        n += 1


def ensure_project_codes(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, name, responsible_agent_id, project_code FROM projects ORDER BY id").fetchall()
    for r in rows:
        desired = make_project_code(conn, r["responsible_agent_id"] or "admin", r["name"], r["id"])
        if not r["project_code"] or r["project_code"] != desired:
            conn.execute("UPDATE projects SET project_code=? WHERE id=?", (desired, r["id"]))


def event_from_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["input_tokens"] = nonnegative_int(item.get("input_tokens") or 0)
    item["cached_input_tokens"] = min(item["input_tokens"], nonnegative_int(item.get("cached_input_tokens") or 0))
    item["output_tokens"] = nonnegative_int(item.get("output_tokens") or 0)
    item["reasoning_tokens"] = nonnegative_int(item.get("reasoning_tokens") or 0)
    item["total_tokens"] = nonnegative_int(item.get("total_tokens") or (item["input_tokens"] + item["output_tokens"]))
    item["estimated_cost_usd"] = estimated_token_cost(item)
    item["commit_refs"] = parse_json_list(item.get("commit_refs_json"))
    return item


# Append an immutable changelog entry. All meaningful agent work should go
# through this path so Activity, Calendar, project pages, and token totals agree.
def event(conn: sqlite3.Connection, project_id: int | None, agent_id: str, event_type: str, summary: str, before=None, after=None, input_tokens: int = 0, cached_input_tokens: int = 0, output_tokens: int = 0, reasoning_tokens: int = 0, total_tokens: int | None = None, model: str = "", commit_refs=None) -> None:
    input_tokens = nonnegative_int(input_tokens)
    cached_input_tokens = min(input_tokens, nonnegative_int(cached_input_tokens))
    output_tokens = nonnegative_int(output_tokens)
    reasoning_tokens = nonnegative_int(reasoning_tokens)
    total_tokens = nonnegative_int(total_tokens if total_tokens is not None else input_tokens + output_tokens)
    conn.execute(
        """
        INSERT INTO project_events (project_id, agent_id, event_type, summary, before_json, after_json, created_at, input_tokens, cached_input_tokens, output_tokens, reasoning_tokens, total_tokens, model, commit_refs_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, agent_id, event_type, summary, json.dumps(before, sort_keys=True) if before is not None else None, json.dumps(after, sort_keys=True) if after is not None else None, now_iso(), input_tokens, cached_input_tokens, output_tokens, reasoning_tokens, total_tokens, model or "", json.dumps(normalize_list(commit_refs))),
    )


# Create and migrate the database in-place. The app is intentionally
# migration-light: new columns are added if missing.
def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'Intake',
                priority TEXT NOT NULL DEFAULT 'Medium',
                owner TEXT NOT NULL DEFAULT '',
                due_date TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                completed_items TEXT NOT NULL DEFAULT '[]',
                next_steps TEXT NOT NULL DEFAULT '[]',
                blockers TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        for column, definition in {
            "responsible_agent_id": "TEXT NOT NULL DEFAULT 'admin'",
            "created_by_agent_id": "TEXT NOT NULL DEFAULT 'admin'",
            "backup_agent_id": "TEXT NOT NULL DEFAULT ''",
            "human_owner": "TEXT NOT NULL DEFAULT 'Owner'",
            "next_action_owner": "TEXT NOT NULL DEFAULT ''",
            "visibility": "TEXT NOT NULL DEFAULT 'shared-agents'",
            "automation_policy": "TEXT NOT NULL DEFAULT 'agent_may_update_tracker_only'",
            "tags": "TEXT NOT NULL DEFAULT '[]'",
            "last_verified_by": "TEXT NOT NULL DEFAULT ''",
            "last_verified_at": "TEXT NOT NULL DEFAULT ''",
            "production_approved_by": "TEXT NOT NULL DEFAULT ''",
            "production_approved_at": "TEXT NOT NULL DEFAULT ''",
            "archived_at": "TEXT NOT NULL DEFAULT ''",
            "archived_by": "TEXT NOT NULL DEFAULT ''",
            "project_code": "TEXT NOT NULL DEFAULT ''",
        }.items():
            add_column(conn, "projects", column, definition)

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                profile_name TEXT NOT NULL DEFAULT '',
                role_summary TEXT NOT NULL DEFAULT '',
                allowed_tags TEXT NOT NULL DEFAULT '[]',
                is_admin INTEGER NOT NULL DEFAULT 0,
                can_create_projects INTEGER NOT NULL DEFAULT 1,
                can_reassign_projects INTEGER NOT NULL DEFAULT 0,
                can_mark_production INTEGER NOT NULL DEFAULT 0,
                can_change_permissions INTEGER NOT NULL DEFAULT 0,
                escalation_agent_id TEXT NOT NULL DEFAULT 'admin',
                status TEXT NOT NULL DEFAULT 'active',
                last_seen_at TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                api_token TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                agent_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        for column, definition in {
            "input_tokens": "INTEGER NOT NULL DEFAULT 0",
            "cached_input_tokens": "INTEGER NOT NULL DEFAULT 0",
            "output_tokens": "INTEGER NOT NULL DEFAULT 0",
            "reasoning_tokens": "INTEGER NOT NULL DEFAULT 0",
            "total_tokens": "INTEGER NOT NULL DEFAULT 0",
            "model": "TEXT NOT NULL DEFAULT ''",
            "commit_refs_json": "TEXT NOT NULL DEFAULT '[]'",
        }.items():
            add_column(conn, "project_events", column, definition)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                request_type TEXT NOT NULL,
                requested_by_agent_id TEXT NOT NULL,
                target_agent_id TEXT NOT NULL DEFAULT '',
                requested_value TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                evidence_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                reviewed_by_agent_id TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )

        for a in SEED_AGENTS:
            existing = conn.execute("SELECT id, api_token FROM agents WHERE id=?", (a["id"],)).fetchone()
            token = existing["api_token"] if existing and existing["api_token"] else secrets.token_urlsafe(24)
            conn.execute(
                """
                INSERT INTO agents (id, display_name, profile_name, role_summary, allowed_tags, is_admin, can_create_projects, can_reassign_projects, can_mark_production, can_change_permissions, escalation_agent_id, api_token)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'admin', ?)
                ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name, profile_name=excluded.profile_name, role_summary=excluded.role_summary, allowed_tags=excluded.allowed_tags, is_admin=excluded.is_admin, can_reassign_projects=excluded.can_reassign_projects, can_mark_production=excluded.can_mark_production, can_change_permissions=excluded.can_change_permissions
                """,
                (a["id"], a["display_name"], a.get("profile_name", ""), a.get("role_summary", ""), json.dumps(a.get("allowed_tags", [])), int(a.get("is_admin", 0)), int(a.get("can_reassign_projects", 0)), int(a.get("can_mark_production", 0)), int(a.get("can_change_permissions", 0)), token),
            )

        count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        if count == 0:
            stamp = now_iso()
            for p in SEED_PROJECTS:
                responsible = p.get("responsible_agent_id", "admin")
                backup = p.get("backup_agent_id", "")
                conn.execute(
                    """
                    INSERT INTO projects (name, stage, priority, owner, due_date, summary, completed_items, next_steps, blockers, responsible_agent_id, backup_agent_id, created_by_agent_id, next_action_owner, last_verified_by, last_verified_at, production_approved_by, production_approved_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, 'admin', ?, 'admin', ?, ?, ?, ?, ?)
                    """,
                    (p["name"], p["stage"], p["priority"], p["owner"], p["summary"], json.dumps(p["completed_items"]), json.dumps(p["next_steps"]), json.dumps(p["blockers"]), responsible, backup, responsible, stamp, "admin" if p["stage"] == "Production" else "", stamp if p["stage"] == "Production" else "", stamp, stamp),
                )
        else:
            # Existing rows predate agents. Keep current data and conservatively assign Admin as coordinator.
            conn.execute("UPDATE projects SET responsible_agent_id='admin' WHERE responsible_agent_id='' OR responsible_agent_id IS NULL")
            conn.execute("UPDATE projects SET created_by_agent_id='admin' WHERE created_by_agent_id='' OR created_by_agent_id IS NULL")
            conn.execute("UPDATE projects SET next_action_owner=responsible_agent_id WHERE next_action_owner='' OR next_action_owner IS NULL")
            conn.execute("UPDATE projects SET production_approved_by='admin', production_approved_at=updated_at WHERE stage='Production' AND production_approved_by='' ")

        ensure_project_codes(conn)
        write_token_file(conn)


# Generate per-agent API keys. This writes bearer tokens to TOKEN_PATH.
# Keep that file secret and out of git.
def write_token_file(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, display_name, profile_name, api_token, is_admin FROM agents WHERE status='active' ORDER BY is_admin DESC, id").fetchall()
    data = {r["id"]: {"display_name": r["display_name"], "profile_name": r["profile_name"], "api_token": r["api_token"], "is_admin": bool(r["is_admin"])} for r in rows}
    TOKEN_PATH.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(TOKEN_PATH, 0o600)


def get_agent(conn: sqlite3.Connection, agent_id: str | None) -> sqlite3.Row:
    agent_id = (agent_id or "admin").strip() or "admin"
    row = conn.execute("SELECT * FROM agents WHERE id=? AND status='active'", (agent_id,)).fetchone()
    if not row:
        raise PermissionError(f"unknown or inactive agent: {agent_id}")
    return row


def agent_can_edit_project(agent: sqlite3.Row, project: dict) -> bool:
    if agent["is_admin"]:
        return True
    return project.get("responsible_agent_id") == agent["id"] or (not project.get("responsible_agent_id") and project.get("created_by_agent_id") == agent["id"])


def public_agent(agent: sqlite3.Row) -> dict:
    return agent_from_row(agent)


def validate_project(payload: dict, existing: dict | None = None, agent: sqlite3.Row | None = None, creating: bool = False) -> dict:
    base = existing.copy() if existing else {}
    for field in ["name", "stage", "priority", "owner", "due_date", "summary", "responsible_agent_id", "backup_agent_id", "human_owner", "next_action_owner", "visibility", "automation_policy", "last_verified_by", "last_verified_at"]:
        if field in payload:
            base[field] = str(payload.get(field, "")).strip()
    for field in ["completed_items", "next_steps", "blockers", "tags"]:
        if field in payload:
            base[field] = normalize_list(payload.get(field))
    if not base.get("name"):
        raise ValueError("Project name is required")
    if base.get("stage") not in STAGES:
        base["stage"] = "Intake"
    if base.get("priority") not in PRIORITIES:
        base["priority"] = "Medium"
    if creating and agent is not None and not agent["is_admin"]:
        base["responsible_agent_id"] = agent["id"]
        base["created_by_agent_id"] = agent["id"]
    if creating:
        base.setdefault("created_by_agent_id", agent["id"] if agent is not None else "admin")
        base.setdefault("responsible_agent_id", agent["id"] if agent is not None else "admin")
    for field in ["owner", "due_date", "summary", "responsible_agent_id", "created_by_agent_id", "backup_agent_id", "human_owner", "next_action_owner", "visibility", "automation_policy", "last_verified_by", "last_verified_at", "production_approved_by", "production_approved_at", "archived_at", "archived_by"]:
        base.setdefault(field, "")
    for field in ["completed_items", "next_steps", "blockers", "tags"]:
        base.setdefault(field, [])
    return base


HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI Multi-Agent Project Tracker</title>
  <style>
    :root { --bg:#0b1120; --panel:#111827; --card:#172033; --muted:#94a3b8; --text:#e5e7eb; --accent:#38bdf8; --green:#22c55e; --yellow:#facc15; --red:#fb7185; --border:#2b3548; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background: radial-gradient(circle at top left,#1e3a8a55,transparent 35%), var(--bg); color: var(--text); }
    header { padding: 24px clamp(16px, 4vw, 44px); border-bottom: 1px solid var(--border); background: rgba(15,23,42,.86); position: sticky; top:0; backdrop-filter: blur(10px); z-index:5; }
    h1 { margin:0 0 8px; font-size: clamp(28px, 4vw, 44px); letter-spacing:-.04em; }
    .sub { color: var(--muted); }
    main { padding: 24px clamp(16px, 4vw, 44px) 60px; }
    .toolbar { display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin-bottom:20px; }
    input, select, textarea { background:#0f172a; color:var(--text); border:1px solid var(--border); border-radius:12px; padding:11px 12px; font:inherit; }
    input[type="search"] { min-width:min(360px,100%); flex:1; }
    button, .button { border:0; color:#06111f; background:var(--accent); border-radius:12px; padding:11px 14px; font-weight:800; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; gap:8px; }
    button.secondary, .button.secondary { color:var(--text); background:#233047; border:1px solid var(--border); }
    button.danger { background:var(--red); color:#28010a; }
    button.warn { background:var(--yellow); color:#291e00; }
    button:disabled { opacity:.48; cursor:not-allowed; }
    .tabs { display:flex; flex-wrap:wrap; gap:8px; margin: 16px 0 20px; align-items:center; }
    .tab { color:var(--text); background:#111827; border:1px solid var(--border); }
    .tab.active, .sort-tab.active { background:var(--accent); color:#06111f; }
    .sort-tabs { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-left:4px; }
    .sort-tab { color:var(--text); background:#111827; border:1px solid var(--border); }
    .stats { display:grid; grid-template-columns: repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin: 12px 0 24px; }
    .stat { background:rgba(17,24,39,.82); border:1px solid var(--border); border-radius:16px; padding:15px; }
    .stat strong { font-size:28px; display:block; }
    .board { display:grid; grid-template-columns: repeat(auto-fit,minmax(295px,1fr)); gap:16px; align-items:start; }
    .col { background:rgba(15,23,42,.68); border:1px solid var(--border); border-radius:18px; padding:12px; min-height:180px; }
    .col h2 { margin:2px 4px 12px; font-size:17px; display:flex; justify-content:space-between; align-items:center; color:#dbeafe; }
    .count { color:var(--muted); font-size:13px; }
    .card { background:linear-gradient(180deg,rgba(30,41,59,.96),rgba(17,24,39,.96)); border:1px solid var(--border); border-radius:16px; padding:14px; margin-bottom:12px; box-shadow:0 12px 30px #0005; }
    .card h3 { margin:0 0 6px; font-size:18px; }
    .meta { display:flex; flex-wrap:wrap; gap:7px; margin:8px 0; }
    .pill { border:1px solid var(--border); color:#cbd5e1; border-radius:999px; padding:4px 8px; font-size:12px; background:#0f172a; }
    .priority-High { border-color:#fb7185; color:#fecdd3; }
    .priority-Medium { border-color:#facc15; color:#fef08a; }
    .priority-Low { border-color:#22c55e; color:#bbf7d0; }
    .admin-pill { border-color:#38bdf8; color:#bae6fd; }
    p { color:#cbd5e1; line-height:1.45; }
    ul { padding-left:20px; margin:8px 0; color:#dbeafe; }
    li.done { color:#bbf7d0; }
    li.blocker { color:#fecdd3; }
    .label { color:var(--muted); font-size:12px; text-transform:uppercase; font-weight:900; letter-spacing:.07em; margin-top:10px; }
    .actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
    .card-summary { display:flex; justify-content:space-between; gap:10px; align-items:flex-start; }
    .card-title { min-width:0; }
    .card-title h3 { margin-bottom:8px; }
    .expand-btn { min-width:42px; justify-content:center; padding:8px 10px; font-size:18px; line-height:1; }
    .project-details { border-top:1px solid var(--border); margin-top:12px; padding-top:12px; }
    .project-details[hidden] { display:none; }
    dialog { width:min(820px, calc(100vw - 28px)); border:1px solid var(--border); background:#111827; color:var(--text); border-radius:18px; padding:0; box-shadow:0 24px 80px #000b; }
    dialog::backdrop { background:#020617aa; backdrop-filter:blur(4px); }
    form { padding:20px; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
    .grid .full { grid-column:1/-1; }
    label { display:flex; flex-direction:column; gap:6px; color:#cbd5e1; font-size:14px; font-weight:700; }
    textarea { min-height:86px; resize:vertical; }
    .form-actions { display:flex; justify-content:space-between; gap:12px; margin-top:16px; }
    .empty { color:var(--muted); border:1px dashed var(--border); border-radius:14px; padding:16px; text-align:center; }
    .agent-grid, .request-grid { display:grid; grid-template-columns: repeat(auto-fit,minmax(280px,1fr)); gap:14px; }
    .notice { border:1px solid var(--border); background:#0f172a; color:#cbd5e1; padding:12px 14px; border-radius:14px; margin: 12px 0; }
    .hidden { display:none; }
    @media (max-width:720px){ .grid{grid-template-columns:1fr;} header{position:static;} }
  
    .cost-hero { margin-bottom:20px; }
    .cost-hero .card { border-color:#38bdf866; }
    .calendar-shell { display:grid; grid-template-columns:minmax(0,1fr) 380px; gap:16px; align-items:start; }
    .calendar-grid { display:grid; gap:8px; }
    .calendar-grid.week { grid-template-columns:repeat(7,minmax(120px,1fr)); }
    .calendar-grid.month { grid-template-columns:repeat(7,minmax(110px,1fr)); }
    .calendar-day { min-height:150px; background:rgba(15,23,42,.72); border:1px solid var(--border); border-radius:14px; padding:8px; }
    .calendar-day.today { border-color:var(--accent); box-shadow:0 0 0 1px #38bdf855 inset; }
    .calendar-day.muted { opacity:.55; }
    .calendar-date { display:flex; justify-content:space-between; align-items:center; color:#dbeafe; font-weight:800; font-size:13px; margin-bottom:7px; }
    .appt { width:100%; text-align:left; border:1px solid #2563eb88; background:#1d4ed888; color:#e0f2fe; border-radius:8px; padding:6px 7px; margin:4px 0; font-size:12px; line-height:1.25; cursor:pointer; }
    .appt:hover { background:#2563eb; }
    .appt small { display:block; color:#bfdbfe; font-size:10px; margin-top:2px; }
    .calendar-detail { position:sticky; top:120px; }
    .project-link { color:#e0f2fe; text-decoration:none; }
    .project-link:hover { color:white; text-decoration:underline; }
    @media (max-width: 980px) { .calendar-shell { grid-template-columns:1fr; } .calendar-grid.week,.calendar-grid.month { grid-template-columns:1fr; } .calendar-detail { position:static; } }

  </style>
</head>
<body>
  <header>
    <h1>AI Multi-Agent Project Tracker</h1>
    <div class="sub">Shared project and agent responsibility registry. Agents can read all projects; non-admin agents modify only their own projects and submit requests for ownership/Production changes.</div>
    <div class="toolbar" style="margin:16px 0 0">
      <span id="permissionSummary" class="pill">Grouped by responsible agent; all projects remain visible.</span>
    </div>
  </header>
  <main>
    <div class="tabs">
      <button class="tab active" data-view="projects">Projects</button>
      <span class="sort-tabs" aria-label="Project sort options">
        <button class="sort-tab active" data-sort="agent">By agent</button>
        <button class="sort-tab" data-sort="stage">By stage</button>
        <button class="sort-tab" data-sort="priority">By priority</button>
        <button class="sort-tab" data-sort="agent-stage">Pipeline</button>
      </span>
      <label id="pipelineAgentLabel" style="display:none">Pipeline agent <select id="pipelineAgent"></select></label>
      <button class="tab" data-view="agents">Agents</button>
      <button class="tab" data-view="requests">Requests</button>
      <button class="tab" data-view="activity">Activity</button>
      <button class="tab" data-view="calendar">Calendar</button>
      <button class="tab" data-view="tokens">Tokens</button>
      <a class="button secondary" href="/api/export" target="_blank">Export JSON</a>
    </div>

    <section id="projectsView">
      <section id="mainCostSummary" class="cost-hero"></section>
      <div class="toolbar">
        <input id="search" type="search" placeholder="Search projects, agents, owners, next steps…" />
        <select id="priorityFilter"><option value="">All priorities</option><option>High</option><option>Medium</option><option>Low</option></select>
        <select id="ownerFilter"><option value="">All responsible agents</option></select>
        <button id="addBtn">+ Add project</button>
      </div>
      <section class="stats" id="stats"></section>
      <section class="board" id="board"></section>
    </section>

    <section id="agentsView" class="hidden"><div id="agentsGrid" class="agent-grid"></div></section>
    <section id="requestsView" class="hidden"><div id="requestsGrid" class="request-grid"></div></section>
    <section id="activityView" class="hidden"><div id="activityList"></div></section>
    <section id="calendarView" class="hidden">
      <div class="toolbar">
        <label>Calendar date <input id="calendarDate" type="date" /></label>
        <select id="calendarMode"><option value="day">Day</option><option value="week" selected>Week</option><option value="month">Month</option></select>
        <button class="secondary" id="calendarPrev">← Previous</button><button class="secondary" id="calendarToday">Today</button><button class="secondary" id="calendarNext">Next →</button><button class="secondary" id="calendarRefresh">Refresh</button>
      </div>
      <div id="calendarTitle" class="sub"></div>
      <div class="calendar-shell"><div id="calendarList"></div><aside id="calendarDetail" class="calendar-detail"><article class="card"><h3>Select an appointment</h3><p>Click a work item to load the work done for that day.</p></article></aside></div>
    </section>
    <section id="tokensView" class="hidden"><div class="toolbar"><label>Date <input id="tokenDate" type="date" /></label><button class="secondary" id="tokenRefresh">Refresh</button></div><div id="tokenSummary"></div></section>
  </main>

  <dialog id="editor">
    <form method="dialog" id="projectForm">
      <h2 id="formTitle">Project</h2>
      <input type="hidden" id="projectId" />
      <div id="aclNotice" class="notice"></div>
      <div class="grid">
        <label class="full">Project name <input id="name" required /></label>
        <label>Stage <select id="stage"></select></label>
        <label>Priority <select id="priority"><option>High</option><option selected>Medium</option><option>Low</option></select></label>
        <label>Responsible agent <select id="responsible_agent_id"></select></label>
        <label>Backup agent <select id="backup_agent_id"></select></label>
        <label>Human owner <input id="human_owner" placeholder="Owner" /></label>
        <label>Due date <input id="due_date" type="date" /></label>
        <label class="full">Legacy owner label <input id="owner" placeholder="Freeform old owner field" /></label>
        <label class="full">Summary <textarea id="summary"></textarea></label>
        <label class="full">Completed items <textarea id="completed_items" placeholder="One item per line"></textarea></label>
        <label class="full">Next steps <textarea id="next_steps" placeholder="One item per line"></textarea></label>
        <label class="full">Blockers <textarea id="blockers" placeholder="One item per line"></textarea></label>
        <label>Next-action owner <select id="next_action_owner"></select></label>
        <label>Visibility <select id="visibility"><option>shared-agents</option><option>david-only</option><option>business</option><option>personal</option><option>family</option></select></label>
        <label class="full">Automation policy <select id="automation_policy"><option>agent_may_update_tracker_only</option><option>manual_only</option><option>agent_may_act_when_assigned</option></select></label>
      </div>
      <div class="form-actions">
        <button type="button" class="danger" id="deleteBtn">Archive</button>
        <span style="flex:1"></span>
        <button type="button" class="secondary" id="requestOwnerBtn">Request owner change</button>
        <button type="button" class="warn" id="requestProdBtn">Request Production</button>
        <button type="button" class="secondary" id="cancelBtn">Cancel</button>
        <button type="submit" id="saveBtn">Save</button>
      </div>
    </form>
  </dialog>

<script>
const STAGES = __STAGES__;
let projects = [], agents = [], requests = [], activity = [], calendarData = [], tokenData = {}, tokenCost = {}, currentAgentId = 'admin', currentSortMode = 'agent';
const $ = sel => document.querySelector(sel);
const board = $('#board'), stats = $('#stats'), editor = $('#editor');
function esc(s){ return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function linesToText(arr){ return (arr || []).join('\n'); }
function textToLines(txt){ return String(txt || '').split('\n').map(s=>s.replace(/^[-\s]+/,'').trim()).filter(Boolean); }
function agentName(id){ const a=agents.find(x=>x.id===id); return a ? a.display_name : (id || 'Unassigned'); }
function currentAgent(){ return agents.find(a=>a.id===currentAgentId) || agents[0] || {id:'admin', is_admin:true}; }
function canEdit(p){ const a=currentAgent(); return !!a.is_admin || p.responsible_agent_id === a.id || (!p.responsible_agent_id && p.created_by_agent_id === a.id); }
async function api(path, opts={}){
  const headers = {'Content-Type':'application/json', 'X-Agent-Id': currentAgentId, ...(opts.headers||{})};
  const res = await fetch(path, {...opts, headers});
  if(!res.ok) throw new Error(await res.text());
  return res.json();
}
function todayLocal(){ return new Date().toISOString().slice(0,10); }
function dateObj(s){ const [y,m,d]=String(s||todayLocal()).split('-').map(Number); return new Date(y,m-1,d); }
function dateStr(d){ const z=new Date(d.getTime()-d.getTimezoneOffset()*60000); return z.toISOString().slice(0,10); }
function addDays(d,n){ const x=new Date(d); x.setDate(x.getDate()+n); return x; }
function startOfWeek(d){ const x=new Date(d); x.setDate(x.getDate()-x.getDay()); return x; }
function endOfWeek(d){ return addDays(startOfWeek(d),6); }
function startOfMonth(d){ return new Date(d.getFullYear(), d.getMonth(), 1); }
function endOfMonth(d){ return new Date(d.getFullYear(), d.getMonth()+1, 0); }
function calendarRange(){ const base=dateObj($('#calendarDate')?.value || todayLocal()), mode=$('#calendarMode')?.value || 'week'; if(mode==='day') return {mode, start:base, end:base}; if(mode==='month') return {mode, start:startOfMonth(base), end:endOfMonth(base)}; return {mode, start:startOfWeek(base), end:endOfWeek(base)}; }
async function refreshCalendar(){ const r=calendarRange(); calendarData = await api(`/api/calendar?start=${encodeURIComponent(dateStr(r.start))}&end=${encodeURIComponent(dateStr(r.end))}`); renderCalendar(); }
async function refreshTokens(){ const d=$('#tokenDate')?.value || todayLocal(); tokenData = await api(`/api/token-summary?date=${encodeURIComponent(d)}`); tokenCost = await api('/api/token-cost-summary'); renderTokens(); }
async function load(){
  if($('#calendarDate') && !$('#calendarDate').value) $('#calendarDate').value = todayLocal();
  if($('#tokenDate') && !$('#tokenDate').value) $('#tokenDate').value = todayLocal();
  agents = await api('/api/agents');
  renderAgentSelectors();
  projects = await api('/api/projects');
  requests = await api('/api/requests');
  activity = await api('/api/activity');
  await refreshCalendar();
  await refreshTokens();
  render(); renderAgents(); renderRequests(); renderActivity();
}
function renderAgentSelectors(){
  const opts = agents.map(a=>`<option value="${esc(a.id)}">${esc(a.display_name)}</option>`).join('');
  ['responsible_agent_id','backup_agent_id','next_action_owner','ownerFilter'].forEach(id=>{
    const empty = id==='backup_agent_id' || id==='ownerFilter' ? '<option value="">None / all</option>' : '';
    $('#'+id).innerHTML = empty + opts;
  });
  if($('#pipelineAgent')){
    $('#pipelineAgent').innerHTML = opts;
    if(!$('#pipelineAgent').value && agents.length) $('#pipelineAgent').value = agents[0].id;
  }
  updateSortHelp();
}
function updateSortHelp(){
  const mode = currentSortMode || 'agent';
  document.querySelectorAll('.sort-tab').forEach(btn=>btn.classList.toggle('active', btn.dataset.sort === mode));
  const label = $('#pipelineAgentLabel');
  if(label) label.style.display = mode === 'agent-stage' ? 'flex' : 'none';
  const help = $('#permissionSummary');
  if(!help) return;
  if(mode === 'agent-stage') help.textContent = `Showing ${agentName($('#pipelineAgent')?.value)} projects as a stage pipeline.`;
  else if(mode === 'agent') help.textContent = 'Grouped by responsible agent; all projects and details remain visible.';
  else if(mode === 'stage') help.textContent = 'Grouped by stage across all visible projects.';
  else help.textContent = 'Grouped by priority across all visible projects.';
}
function filtered(){
  const q = $('#search').value.toLowerCase(); const pri=$('#priorityFilter').value; const owner=$('#ownerFilter').value;
  return projects.filter(p => { const hay = JSON.stringify(p).toLowerCase(); return (!q || hay.includes(q)) && (!pri || p.priority===pri) && (!owner || p.responsible_agent_id===owner); });
}
function renderStats(items){
  const total=items.length, high=items.filter(p=>p.priority==='High'&&p.stage!=='Production').length, blocked=items.filter(p=>(p.blockers||[]).length).length, stale=items.filter(p=>!p.last_verified_at).length;
  stats.innerHTML = `<div class="stat"><strong>${total}</strong><span class="sub">Projects shown</span></div><div class="stat"><strong>${high}</strong><span class="sub">High priority non-production</span></div><div class="stat"><strong>${blocked}</strong><span class="sub">With blockers</span></div><div class="stat"><strong>${stale}</strong><span class="sub">Never verified</span></div>`;
}
function card(p, prefix=""){
  const completed=(p.completed_items||[]).slice(0,8).map(x=>`<li class="done">${esc(x)}</li>`).join('') || '<li class="emptyline">None yet</li>';
  const next=(p.next_steps||[]).slice(0,8).map(x=>`<li>${esc(x)}</li>`).join('') || '<li class="emptyline">Add next step</li>';
  const blockers=(p.blockers||[]).map(x=>`<li class="blocker">${esc(x)}</li>`).join('');
  const editable=canEdit(p);
  const cardId = `${prefix}${p.id}`;
  const detailsId = `details-${cardId}`;
  const arrowId = `arrow-${cardId}`;
  return `<article class="card">
    <div class="card-summary">
      <div class="card-title">
        <h3><a class="project-link" href="/project/${p.id}" target="_blank"><span class="pill">${esc(p.project_code||'')}</span> ${esc(p.name)}</a></h3>
        <div class="meta"><span class="pill priority-${esc(p.priority)}">${esc(p.priority)}</span><span class="pill">Responsible: ${esc(agentName(p.responsible_agent_id))}</span>${p.backup_agent_id?`<span class="pill">Backup: ${esc(agentName(p.backup_agent_id))}</span>`:''}${p.due_date?`<span class="pill">Due ${esc(p.due_date)}</span>`:''}</div>
      </div>
      <button class="secondary expand-btn" onclick="toggleDetails('${cardId}')" aria-expanded="false" aria-controls="${detailsId}" title="Expand project details"><span id="${arrowId}">▼</span></button>
    </div>
    <div class="project-details" id="${detailsId}" hidden>
      ${p.summary?`<p>${esc(p.summary)}</p>`:''}
      <div class="label">Complete</div><ul>${completed}</ul><div class="label">Next steps</div><ul>${next}</ul>${blockers?`<div class="label">Blockers</div><ul>${blockers}</ul>`:''}
      <div class="meta"><span class="pill">Next: ${esc(agentName(p.next_action_owner || p.responsible_agent_id))}</span>${p.last_verified_at?`<span class="pill">Verified by ${esc(agentName(p.last_verified_by))}</span>`:'<span class="pill">Not verified</span>'}</div>
      <div class="actions"><button class="secondary" onclick="openEditor(${p.id})">${editable?'Edit':'View / request'}</button></div>
    </div>
  </article>`;
}
function render(){
  renderMainCost();
  let items=filtered(); renderStats(items); updateSortHelp();
  const mode = currentSortMode || 'agent';
  if(mode === 'agent-stage'){
    const aid = $('#pipelineAgent')?.value || (agents[0]?.id || '');
    const pipeItems = items.filter(p=>p.responsible_agent_id===aid);
    renderStats(pipeItems);
    board.innerHTML = STAGES.map(stage=>{ const list=pipeItems.filter(p=>p.stage===stage); return `<section class="col"><h2>${stage}<span class="count">${list.length}</span></h2>${list.length?list.map(card).join(''):'<div class="empty">No projects</div>'}</section>`; }).join('');
    updateSortHelp();
    return;
  }
  if(mode === 'agent'){
    const sortedAgents = [...agents].sort((a,b)=> (b.is_admin-a.is_admin) || a.display_name.localeCompare(b.display_name));
    const sections = sortedAgents.map(a=>{ const list=items.filter(p=>p.responsible_agent_id===a.id); return `<section class="col"><h2>${esc(a.display_name)}<span class="count">${list.length}</span></h2>${list.length?list.map(card).join(''):'<div class="empty">No projects</div>'}</section>`; });
    const unassigned = items.filter(p=>!p.responsible_agent_id || !agents.some(a=>a.id===p.responsible_agent_id));
    if(unassigned.length) sections.push(`<section class="col"><h2>Unassigned<span class="count">${unassigned.length}</span></h2>${unassigned.map(card).join('')}</section>`);
    board.innerHTML = sections.join('');
    return;
  }
  if(mode === 'priority'){
    board.innerHTML = ['High','Medium','Low'].map(priority=>{ const list=items.filter(p=>p.priority===priority); return `<section class="col"><h2>${priority}<span class="count">${list.length}</span></h2>${list.length?list.map(card).join(''):'<div class="empty">No projects</div>'}</section>`; }).join('');
    return;
  }
  board.innerHTML = STAGES.map(stage=>{ const list=items.filter(p=>p.stage===stage); return `<section class="col"><h2>${stage}<span class="count">${list.length}</span></h2>${list.length?list.map(card).join(''):'<div class="empty">No projects</div>'}</section>`; }).join('');
}
function fillForm(p={}){
  const a=currentAgent(); const editable = !p.id || canEdit(p);
  $('#projectId').value=p.id||''; $('#name').value=p.name||''; $('#stage').value=p.stage||'Intake'; $('#priority').value=p.priority||'Medium'; $('#owner').value=p.owner||''; $('#due_date').value=p.due_date||''; $('#summary').value=p.summary||'';
  $('#responsible_agent_id').value=p.responsible_agent_id || a.id; $('#backup_agent_id').value=p.backup_agent_id||''; $('#human_owner').value=p.human_owner||'Owner'; $('#next_action_owner').value=p.next_action_owner || p.responsible_agent_id || a.id; $('#visibility').value=p.visibility||'shared-agents'; $('#automation_policy').value=p.automation_policy||'agent_may_update_tracker_only';
  $('#completed_items').value=linesToText(p.completed_items); $('#next_steps').value=linesToText(p.next_steps); $('#blockers').value=linesToText(p.blockers);
  $('#deleteBtn').style.visibility = p.id && editable ? 'visible' : 'hidden'; $('#saveBtn').disabled=!editable; $('#formTitle').textContent=p.id?(editable?'Edit project':'View project / submit request'):'Add project';
  $('#aclNotice').textContent = p.id ? (editable ? 'You can edit/archive this project as '+agentName(a.id)+'. Non-Admin agents cannot reassign owner or directly mark Production.' : 'Read-only for '+agentName(a.id)+'. You may submit owner-change or Production requests.') : 'New projects are owned by the creating agent unless Admin assigns another owner.';
  ['name','stage','priority','owner','due_date','summary','completed_items','next_steps','blockers','backup_agent_id','human_owner','next_action_owner','visibility','automation_policy'].forEach(id=>$('#'+id).disabled=!editable);
  $('#responsible_agent_id').disabled = !editable || !a.is_admin;
  $('#requestOwnerBtn').style.display = p.id ? 'inline-flex' : 'none'; $('#requestProdBtn').style.display = p.id && p.stage !== 'Production' ? 'inline-flex' : 'none';
}
function readForm(){ return {name:$('#name').value, stage:$('#stage').value, priority:$('#priority').value, owner:$('#owner').value, due_date:$('#due_date').value, summary:$('#summary').value, responsible_agent_id:$('#responsible_agent_id').value, backup_agent_id:$('#backup_agent_id').value, human_owner:$('#human_owner').value, next_action_owner:$('#next_action_owner').value, visibility:$('#visibility').value, automation_policy:$('#automation_policy').value, completed_items:textToLines($('#completed_items').value), next_steps:textToLines($('#next_steps').value), blockers:textToLines($('#blockers').value)}; }
window.openEditor = id => { const p=projects.find(x=>x.id===id); fillForm(p); editor.showModal(); };
window.toggleDetails = id => {
  const details = document.getElementById(`details-${id}`);
  const arrow = document.getElementById(`arrow-${id}`);
  if(!details) return;
  const willOpen = details.hasAttribute('hidden');
  if(willOpen) details.removeAttribute('hidden'); else details.setAttribute('hidden', '');
  if(arrow) arrow.textContent = willOpen ? '▲' : '▼';
  const btn = document.querySelector(`[aria-controls="details-${id}"]`);
  if(btn) btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
};
function renderAgents(){
  const html = agents.map(a=>{
    const owned=projects.filter(p=>p.responsible_agent_id===a.id);
    const backup=projects.filter(p=>p.backup_agent_id===a.id);
    const ownedCards = owned.length ? `<div class="owned-projects">${owned.map(p=>card(p, 'agent-')).join('')}</div>` : '<div class="empty">None assigned</div>';
    return `<article class="card agent-card"><h3>${esc(a.display_name)} ${a.is_admin?'<span class="pill admin-pill">admin</span>':''}</h3><p>${esc(a.role_summary)}</p><div class="meta"><span class="pill">Profile: ${esc(a.profile_name)}</span><span class="pill">Owns ${owned.length}</span><span class="pill">Backup for ${backup.length}</span></div><div class="label">Owned projects</div>${ownedCards}</article>`;
  }).join('');
  $('#agentsGrid').innerHTML = html;
}
function renderRequests(){
  const a=currentAgent();
  $('#requestsGrid').innerHTML = requests.length ? requests.map(r=>{ const controls = a.is_admin && r.status === 'pending' ? `<div class="actions"><button onclick="reviewRequest(${r.id}, 'approve')">Approve</button><button class="secondary" onclick="reviewRequest(${r.id}, 'reject')">Reject</button></div>` : ''; return `<article class="card"><h3>${esc(r.request_type)} <span class="pill">${esc(r.status)}</span></h3><p><strong>${esc(r.project_name||('Project '+r.project_id))}</strong></p><p>${esc(r.reason)}</p><div class="meta"><span class="pill">From ${esc(agentName(r.requested_by_agent_id))}</span>${r.target_agent_id?`<span class="pill">Target ${esc(agentName(r.target_agent_id))}</span>`:''}${r.requested_value?`<span class="pill">Value ${esc(r.requested_value)}</span>`:''}</div>${controls}</article>`; }).join('') : '<div class="empty">No requests</div>';
}
window.reviewRequest = async (id, action) => { if(!confirm(`${action} request #${id}?`)) return; await api(`/api/requests/${id}/${action}`, {method:'POST'}); await load(); };
function eventCard(e){
  const commits = (e.commit_refs||[]).length ? `<div class="label">Commits</div><ul>${(e.commit_refs||[]).map(c=>`<li>${esc(c)}</li>`).join('')}</ul>` : '';
  const tok = Number(e.total_tokens||0) ? `<span class="pill">${Number(e.total_tokens||0).toLocaleString()} tokens</span>` : '';
  return `<article class="card"><div class="meta"><span class="pill">${esc(e.created_at)}</span><span class="pill">${esc(agentName(e.agent_id))}</span><span class="pill">${esc(e.event_type)}</span>${tok}${e.model?`<span class="pill">${esc(e.model)}</span>`:''}</div><h3><a class="project-link" href="/project/${esc(e.project_id||'')}" target="_blank">${esc(e.project_code||'')} ${esc(e.project_name||'Tracker')}</a></h3><p>${esc(e.summary)}</p>${commits}</article>`;
}
function renderActivity(){
  $('#activityList').innerHTML = activity.length ? activity.map(eventCard).join('') : '<div class="empty">No activity</div>';
}
function apptButton(e){ const time=String(e.created_at||'').slice(11,16); return `<button class="appt" onclick="showCalendarEvent(${Number(e.id)})"><strong>${esc(time)} ${esc(e.project_code||'')}</strong><span>${esc(e.project_name||'Tracker')}</span><small>${esc(agentName(e.agent_id))} · ${esc(e.event_type)}</small></button>`; }
function showCalendarEvent(id){ const e=calendarData.find(x=>Number(x.id)===Number(id)); if(e) $('#calendarDetail').innerHTML = eventCard(e); }
function renderCalendar(){
  const r=calendarRange(), mode=r.mode, today=todayLocal();
  const fmt=d=>d.toLocaleDateString(undefined,{weekday:'short', month:'short', day:'numeric'});
  $('#calendarTitle').textContent = mode==='day' ? fmt(r.start) : `${fmt(r.start)} — ${fmt(r.end)}`;
  const byDate={}; calendarData.forEach(e=>{ const d=String(e.created_at||'').slice(0,10); (byDate[d] ||= []).push(e); });
  if(mode==='day'){
    const d=dateStr(r.start), events=byDate[d]||[];
    $('#calendarList').innerHTML = `<div class="calendar-grid"><section class="calendar-day today"><div class="calendar-date"><span>${esc(fmt(r.start))}</span><span>${events.length}</span></div>${events.length?events.map(apptButton).join(''):'<div class="empty">No work recorded</div>'}</section></div>`;
  } else {
    const start = mode==='month' ? startOfWeek(startOfMonth(r.start)) : r.start;
    const days = mode==='month' ? 42 : 7;
    const month = r.start.getMonth();
    $('#calendarList').innerHTML = `<div class="calendar-grid ${mode}">` + Array.from({length:days}, (_,i)=>{ const d=addDays(start,i), key=dateStr(d), events=byDate[key]||[], muted=mode==='month'&&d.getMonth()!==month; return `<section class="calendar-day ${key===today?'today':''} ${muted?'muted':''}"><div class="calendar-date"><span>${esc(fmt(d))}</span><span>${events.length}</span></div>${events.slice(0,6).map(apptButton).join('')}${events.length>6?`<div class="sub">+${events.length-6} more</div>`:''}</section>`; }).join('') + `</div>`;
  }
  if(calendarData[0]) showCalendarEvent(calendarData[0].id); else $('#calendarDetail').innerHTML = '<article class="card"><h3>No work recorded</h3><p>No changelog entries for this range.</p></article>';
}
function tokenTable(title, rows, nameFn){
  if(!rows || !rows.length) return `<article class="card"><h3>${esc(title)}</h3><p>No token usage recorded.</p></article>`;
  return `<article class="card"><h3>${esc(title)}</h3><div class="tablewrap"><table><thead><tr><th>Name</th><th>Total</th><th>Input</th><th>Cached input</th><th>Output</th><th>Reasoning</th><th>Updates</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(nameFn(r))}</td><td>${Number(r.total_tokens||0).toLocaleString()}</td><td>${Number(r.input_tokens||0).toLocaleString()}</td><td>${Number(r.cached_input_tokens||0).toLocaleString()}</td><td>${Number(r.output_tokens||0).toLocaleString()}</td><td>${Number(r.reasoning_tokens||0).toLocaleString()}</td><td>${Number(r.updates||0)}</td></tr>`).join('')}</tbody></table></div></article>`;
}
function money(n){ return '$' + Number(n||0).toFixed(2); }
function costCard(label, data){
  data = data || {};
  return `<div class="stat"><strong>${money(data.estimated_cost_usd)}</strong><span class="sub">${esc(label)} · ${Number(data.total_tokens||0).toLocaleString()} tokens</span><div class="sub">Input ${Number(data.input_tokens||0).toLocaleString()} (${Number(data.cached_input_tokens||0).toLocaleString()} cached) / Out ${Number(data.output_tokens||0).toLocaleString()} / Reasoning ${Number(data.reasoning_tokens||0).toLocaleString()}</div></div>`;
}
function costBlockHtml(){
  const periods = tokenCost.periods || {};
  const inRate = Number(tokenCost.input_rate_per_million ?? 5).toFixed(2);
  const cachedRate = Number(tokenCost.cached_input_rate_per_million ?? 0.5).toFixed(2);
  const outRate = Number(tokenCost.output_rate_per_million ?? 30).toFixed(2);
  return `<article class="card"><h3>Estimated token cost — all agents</h3><p class="sub">$${inRate} / 1M uncached input + $${cachedRate} / 1M cached input + $${outRate} / 1M output. Reasoning tokens are tracked separately and included in output when providers report them that way.</p><section class="stats">${costCard('Last 24 hours', periods.last_24h)}${costCard('Last 7 days', periods.last_7d)}${costCard('Last 30 days', periods.last_30d)}</section></article>`;
}
function renderMainCost(){ if($('#mainCostSummary')) $('#mainCostSummary').innerHTML = costBlockHtml(); }
function renderTokens(){
  const totals = tokenData.totals || {};
  const costBlock = costBlockHtml();
  $('#tokenSummary').innerHTML = costBlock + `<section class="stats"><div class="stat"><strong>${Number(totals.total_tokens||0).toLocaleString()}</strong><span class="sub">Tokens selected day</span></div><div class="stat"><strong>${Number(totals.input_tokens||0).toLocaleString()}</strong><span class="sub">Input tokens</span></div><div class="stat"><strong>${Number(totals.cached_input_tokens||0).toLocaleString()}</strong><span class="sub">Cached input tokens</span></div><div class="stat"><strong>${Number(totals.output_tokens||0).toLocaleString()}</strong><span class="sub">Output tokens</span></div><div class="stat"><strong>${Number(totals.reasoning_tokens||0).toLocaleString()}</strong><span class="sub">Reasoning tokens</span></div><div class="stat"><strong>${Number(totals.updates||0)}</strong><span class="sub">Updates with usage</span></div></section>` + tokenTable('By agent', tokenData.by_agent||[], r=>agentName(r.agent_id)) + tokenTable('By project', tokenData.by_project||[], r=>r.project_name||'Tracker');
}
$('#stage').innerHTML = STAGES.map(s=>`<option>${s}</option>`).join('');
$('#addBtn').onclick=()=>{ fillForm(); editor.showModal(); };
$('#cancelBtn').onclick=()=>editor.close();
if($('#pipelineAgent')) $('#pipelineAgent').onchange=render;
document.querySelectorAll('.sort-tab').forEach(btn=>btn.onclick=()=>{ currentSortMode = btn.dataset.sort || 'agent'; document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active')); const projectsTab=document.querySelector('.tab[data-view="projects"]'); if(projectsTab) projectsTab.classList.add('active'); ['projects','agents','requests','activity','calendar','tokens'].forEach(v=>$('#'+v+'View').classList.toggle('hidden', v!=='projects')); render(); });
document.querySelectorAll('.tab').forEach(btn=>btn.onclick=()=>{ document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active')); btn.classList.add('active'); ['projects','agents','requests','activity','calendar','tokens'].forEach(v=>$('#'+v+'View').classList.toggle('hidden', v!==btn.dataset.view)); });
$('#projectForm').onsubmit=async e=>{ e.preventDefault(); const id=$('#projectId').value; try{ await api(id?`/api/projects/${id}`:'/api/projects', {method:id?'PUT':'POST', body:JSON.stringify(readForm())}); editor.close(); await load(); }catch(err){ alert(err.message); } };
$('#deleteBtn').onclick=async()=>{ const id=$('#projectId').value; if(id && confirm('Archive this project?')){ await api(`/api/projects/${id}`, {method:'DELETE'}); editor.close(); await load(); } };
$('#requestOwnerBtn').onclick=async()=>{ const id=$('#projectId').value; const target=prompt('Requested new owner agent id (admin, research, operations):','research'); if(!target) return; const reason=prompt('Reason for owner change request:','This project fits that agent better.'); await api(`/api/projects/${id}/requests`, {method:'POST', body:JSON.stringify({request_type:'owner_change', target_agent_id:target, requested_value:target, reason})}); alert('Request submitted'); editor.close(); await load(); };
$('#requestProdBtn').onclick=async()=>{ const id=$('#projectId').value; const reason=prompt('Reason/evidence for Production request:','Verified live and actively used.'); if(!reason) return; await api(`/api/projects/${id}/requests`, {method:'POST', body:JSON.stringify({request_type:'mark_production', requested_value:'Production', reason})}); alert('Production request submitted'); editor.close(); await load(); };
$('#search').oninput=render; $('#priorityFilter').onchange=render; $('#ownerFilter').onchange=render; if($('#pipelineAgent')) $('#pipelineAgent').onchange=render; if($('#calendarRefresh')) $('#calendarRefresh').onclick=refreshCalendar; if($('#calendarMode')) $('#calendarMode').onchange=refreshCalendar; if($('#calendarPrev')) $('#calendarPrev').onclick=()=>{ const r=calendarRange(); const delta=r.mode==='month'?-30:r.mode==='week'?-7:-1; $('#calendarDate').value=dateStr(addDays(dateObj($('#calendarDate').value), delta)); refreshCalendar(); }; if($('#calendarNext')) $('#calendarNext').onclick=()=>{ const r=calendarRange(); const delta=r.mode==='month'?30:r.mode==='week'?7:1; $('#calendarDate').value=dateStr(addDays(dateObj($('#calendarDate').value), delta)); refreshCalendar(); }; if($('#calendarToday')) $('#calendarToday').onclick=()=>{ $('#calendarDate').value=todayLocal(); refreshCalendar(); }; if($('#tokenRefresh')) $('#tokenRefresh').onclick=refreshTokens; if($('#calendarDate')) $('#calendarDate').onchange=refreshCalendar; if($('#tokenDate')) $('#tokenDate').onchange=refreshTokens; if($('#pipelineAgent')) $('#pipelineAgent').onchange=render;
load().catch(err=>{ board.innerHTML=`<pre>${esc(err.message)}</pre>`; });
</script>
</body>
</html>
""".replace("__STAGES__", json.dumps(STAGES))


CSS_ONLY = r'''
    :root { --bg:#0b1120; --panel:#111827; --card:#172033; --muted:#94a3b8; --text:#e5e7eb; --accent:#38bdf8; --green:#22c55e; --yellow:#facc15; --red:#fb7185; --border:#2b3548; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background: radial-gradient(circle at top left,#1e3a8a55,transparent 35%), var(--bg); color: var(--text); }
    header { padding: 24px clamp(16px, 4vw, 44px); border-bottom: 1px solid var(--border); background: rgba(15,23,42,.86); position: sticky; top:0; backdrop-filter: blur(10px); z-index:5; }
    h1 { margin:0 0 8px; font-size: clamp(28px, 4vw, 44px); letter-spacing:-.04em; }
    .sub { color: var(--muted); }
    main { padding: 24px clamp(16px, 4vw, 44px) 60px; }
    .toolbar { display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin-bottom:20px; }
    input, select, textarea { background:#0f172a; color:var(--text); border:1px solid var(--border); border-radius:12px; padding:11px 12px; font:inherit; }
    input[type="search"] { min-width:min(360px,100%); flex:1; }
    button, .button { border:0; color:#06111f; background:var(--accent); border-radius:12px; padding:11px 14px; font-weight:800; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; gap:8px; }
    button.secondary, .button.secondary { color:var(--text); background:#233047; border:1px solid var(--border); }
    button.danger { background:var(--red); color:#28010a; }
    button.warn { background:var(--yellow); color:#291e00; }
    button:disabled { opacity:.48; cursor:not-allowed; }
    .tabs { display:flex; flex-wrap:wrap; gap:8px; margin: 16px 0 20px; align-items:center; }
    .tab { color:var(--text); background:#111827; border:1px solid var(--border); }
    .tab.active, .sort-tab.active { background:var(--accent); color:#06111f; }
    .sort-tabs { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-left:4px; }
    .sort-tab { color:var(--text); background:#111827; border:1px solid var(--border); }
    .stats { display:grid; grid-template-columns: repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin: 12px 0 24px; }
    .stat { background:rgba(17,24,39,.82); border:1px solid var(--border); border-radius:16px; padding:15px; }
    .stat strong { font-size:28px; display:block; }
    .board { display:grid; grid-template-columns: repeat(auto-fit,minmax(295px,1fr)); gap:16px; align-items:start; }
    .col { background:rgba(15,23,42,.68); border:1px solid var(--border); border-radius:18px; padding:12px; min-height:180px; }
    .col h2 { margin:2px 4px 12px; font-size:17px; display:flex; justify-content:space-between; align-items:center; color:#dbeafe; }
    .count { color:var(--muted); font-size:13px; }
    .card { background:linear-gradient(180deg,rgba(30,41,59,.96),rgba(17,24,39,.96)); border:1px solid var(--border); border-radius:16px; padding:14px; margin-bottom:12px; box-shadow:0 12px 30px #0005; }
    .card h3 { margin:0 0 6px; font-size:18px; }
    .meta { display:flex; flex-wrap:wrap; gap:7px; margin:8px 0; }
    .pill { border:1px solid var(--border); color:#cbd5e1; border-radius:999px; padding:4px 8px; font-size:12px; background:#0f172a; }
    .priority-High { border-color:#fb7185; color:#fecdd3; }
    .priority-Medium { border-color:#facc15; color:#fef08a; }
    .priority-Low { border-color:#22c55e; color:#bbf7d0; }
    .admin-pill { border-color:#38bdf8; color:#bae6fd; }
    p { color:#cbd5e1; line-height:1.45; }
    ul { padding-left:20px; margin:8px 0; color:#dbeafe; }
    li.done { color:#bbf7d0; }
    li.blocker { color:#fecdd3; }
    .label { color:var(--muted); font-size:12px; text-transform:uppercase; font-weight:900; letter-spacing:.07em; margin-top:10px; }
    .actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
    .card-summary { display:flex; justify-content:space-between; gap:10px; align-items:flex-start; }
    .card-title { min-width:0; }
    .card-title h3 { margin-bottom:8px; }
    .expand-btn { min-width:42px; justify-content:center; padding:8px 10px; font-size:18px; line-height:1; }
    .project-details { border-top:1px solid var(--border); margin-top:12px; padding-top:12px; }
    .project-details[hidden] { display:none; }
    dialog { width:min(820px, calc(100vw - 28px)); border:1px solid var(--border); background:#111827; color:var(--text); border-radius:18px; padding:0; box-shadow:0 24px 80px #000b; }
    dialog::backdrop { background:#020617aa; backdrop-filter:blur(4px); }
    form { padding:20px; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
    .grid .full { grid-column:1/-1; }
    label { display:flex; flex-direction:column; gap:6px; color:#cbd5e1; font-size:14px; font-weight:700; }
    textarea { min-height:86px; resize:vertical; }
    .form-actions { display:flex; justify-content:space-between; gap:12px; margin-top:16px; }
    .empty { color:var(--muted); border:1px dashed var(--border); border-radius:14px; padding:16px; text-align:center; }
    .agent-grid, .request-grid { display:grid; grid-template-columns: repeat(auto-fit,minmax(280px,1fr)); gap:14px; }
    .notice { border:1px solid var(--border); background:#0f172a; color:#cbd5e1; padding:12px 14px; border-radius:14px; margin: 12px 0; }
    .hidden { display:none; }
    @media (max-width:720px){ .grid{grid-template-columns:1fr;} header{position:static;} }
  
    .cost-hero { margin-bottom:20px; }
    .cost-hero .card { border-color:#38bdf866; }
    .calendar-shell { display:grid; grid-template-columns:minmax(0,1fr) 380px; gap:16px; align-items:start; }
    .calendar-grid { display:grid; gap:8px; }
    .calendar-grid.week { grid-template-columns:repeat(7,minmax(120px,1fr)); }
    .calendar-grid.month { grid-template-columns:repeat(7,minmax(110px,1fr)); }
    .calendar-day { min-height:150px; background:rgba(15,23,42,.72); border:1px solid var(--border); border-radius:14px; padding:8px; }
    .calendar-day.today { border-color:var(--accent); box-shadow:0 0 0 1px #38bdf855 inset; }
    .calendar-day.muted { opacity:.55; }
    .calendar-date { display:flex; justify-content:space-between; align-items:center; color:#dbeafe; font-weight:800; font-size:13px; margin-bottom:7px; }
    .appt { width:100%; text-align:left; border:1px solid #2563eb88; background:#1d4ed888; color:#e0f2fe; border-radius:8px; padding:6px 7px; margin:4px 0; font-size:12px; line-height:1.25; cursor:pointer; }
    .appt:hover { background:#2563eb; }
    .appt small { display:block; color:#bfdbfe; font-size:10px; margin-top:2px; }
    .calendar-detail { position:sticky; top:120px; }
    .project-link { color:#e0f2fe; text-decoration:none; }
    .project-link:hover { color:white; text-decoration:underline; }
    @media (max-width: 980px) { .calendar-shell { grid-template-columns:1fr; } .calendar-grid.week,.calendar-grid.month { grid-template-columns:1fr; } .calendar-detail { position:static; } }

  '''

def h(s):
    return html.escape(str(s or ''), quote=True)


def project_page(project_id: int) -> bytes:
    with connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            return b"", HTTPStatus.NOT_FOUND
        p = project_from_row(row)
        events = [event_from_row(r) for r in conn.execute("""
            SELECT e.*, p.name AS project_name, p.project_code AS project_code FROM project_events e
            LEFT JOIN projects p ON p.id=e.project_id
            WHERE e.project_id=? ORDER BY e.created_at DESC, e.id DESC
        """, (project_id,)).fetchall()]
    def items(title, vals, cls=''):
        if not vals: return f"<section class='card'><h3>{h(title)}</h3><p class='sub'>None recorded.</p></section>"
        return f"<section class='card'><h3>{h(title)}</h3><ul>" + ''.join(f"<li class='{cls}'>{h(v)}</li>" for v in vals) + "</ul></section>"
    parts = []
    for e in events:
        token_pill = f"<span class='pill'>{h(e.get('total_tokens'))} tokens</span>" if e.get('total_tokens') else ''
        commits = ''
        if e.get('commit_refs'):
            commits = "<div class='label'>Commits</div><ul>" + ''.join(f"<li>{h(c)}</li>" for c in e.get('commit_refs', [])) + "</ul>"
        parts.append(f"<article class='card'><div class='meta'><span class='pill'>{h(e.get('created_at'))}</span><span class='pill'>{h(e.get('agent_id'))}</span><span class='pill'>{h(e.get('event_type'))}</span>{token_pill}</div><h3>{h(e.get('summary'))}</h3>{commits}</article>")
    ev_html = ''.join(parts) or "<div class='empty'>No changelog entries.</div>"
    html_doc = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{h(p.get('project_code'))} {h(p.get('name'))}</title><style>{CSS_ONLY}</style></head><body><header><h1>{h(p.get('project_code'))} {h(p.get('name'))}</h1><p class='sub'><a class='project-link' href='/'>← Back to tracker</a></p><div class='meta'><span class='pill'>{h(p.get('stage'))}</span><span class='pill priority-{h(p.get('priority'))}'>{h(p.get('priority'))}</span><span class='pill'>Responsible: {h(p.get('responsible_agent_id'))}</span><span class='pill'>Verified: {h(p.get('last_verified_at'))}</span></div></header><main><section class='card'><h3>Summary</h3><p>{h(p.get('summary'))}</p><p><strong>Next owner:</strong> {h(p.get('next_action_owner'))}</p></section><div class='board'>{items('Complete', p.get('completed_items'), 'done')}{items('Next steps', p.get('next_steps'))}{items('Blockers', p.get('blockers'), 'blocker')}</div><h2>Changelog</h2>{ev_html}</main></body></html>"""
    return html_doc.encode(), HTTPStatus.OK


# The HTTP handler serves both the browser UI and the JSON API. Keeping it
# in one file makes this easy to deploy and inspect.
class Handler(BaseHTTPRequestHandler):
    server_version = "ProjectTracker/2.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - - [{self.log_date_time_string()}] {fmt % args}")

    def send_body(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, obj, status: int = 200):
        self.send_body(json.dumps(obj, indent=2).encode(), "application/json; charset=utf-8", int(status))

    def read_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0") or "0")
        if size == 0:
            return {}
        return json.loads(self.rfile.read(size).decode())

    # Resolve the caller. Automation should use X-API-Key; X-Agent-Id/query
    # are convenience fallbacks for local/manual testing only.
    def current_agent(self, conn: sqlite3.Connection) -> sqlite3.Row:
        # Browser/UI can choose X-Agent-Id. Automation should use X-API-Key;
        # token lookup maps the request to the owning agent. Legacy scripts without
        # either header run as Admin/default for backward compatibility.
        api_key = (self.headers.get("X-API-Key") or "").strip()
        if api_key:
            row = conn.execute("SELECT * FROM agents WHERE api_token=? AND status='active'", (api_key,)).fetchone()
            if not row:
                raise PermissionError("invalid project tracker API key")
            return row
        agent_id = self.headers.get("X-Agent-Id") or parse_qs(urlparse(self.path).query).get("agent", ["admin"])[0]
        return get_agent(conn, agent_id)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self.send_body(HTML.encode(), "text/html; charset=utf-8")
        if path.startswith("/project/"):
            try:
                body, status = project_page(int(path.rsplit("/", 1)[-1]))
                if status != HTTPStatus.OK:
                    return self.json_response({"error": "project not found"}, status)
                return self.send_body(body, "text/html; charset=utf-8")
            except Exception as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if path == "/health":
            return self.json_response({"ok": True, "version": "2.0", "db": str(DB_PATH), "time": now_iso()})
        if path == "/api/agents":
            with connect() as conn:
                rows = conn.execute("SELECT * FROM agents WHERE status='active' ORDER BY is_admin DESC, display_name").fetchall()
            return self.json_response([agent_from_row(r) for r in rows])
        if path == "/api/projects":
            include_archived = parse_qs(parsed.query).get("include_archived", ["0"])[0] in ("1", "true", "yes")
            sql = "SELECT * FROM projects"
            if not include_archived:
                sql += " WHERE archived_at=''"
            sql += " ORDER BY CASE priority WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END, updated_at DESC"
            with connect() as conn:
                rows = conn.execute(sql).fetchall()
            return self.json_response([project_from_row(r) for r in rows])
        if path.startswith("/api/projects/") and path.endswith("/events"):
            try:
                project_id = int(path.split("/")[3])
            except Exception:
                return self.json_response({"error": "bad project id"}, HTTPStatus.BAD_REQUEST)
            with connect() as conn:
                rows = conn.execute("""
                    SELECT e.*, p.name AS project_name, p.project_code AS project_code FROM project_events e
                    LEFT JOIN projects p ON p.id=e.project_id
                    WHERE e.project_id=? ORDER BY e.created_at DESC, e.id DESC
                """, (project_id,)).fetchall()
            return self.json_response([event_from_row(r) for r in rows])
        if path == "/api/requests":
            with connect() as conn:
                rows = conn.execute("""
                    SELECT r.*, p.name AS project_name FROM project_requests r
                    LEFT JOIN projects p ON p.id=r.project_id
                    ORDER BY CASE r.status WHEN 'pending' THEN 1 ELSE 2 END, r.created_at DESC LIMIT 100
                """).fetchall()
            return self.json_response([dict(r) for r in rows])
        if path == "/api/activity":
            with connect() as conn:
                rows = conn.execute("""
                    SELECT e.*, p.name AS project_name, p.project_code AS project_code FROM project_events e
                    LEFT JOIN projects p ON p.id=e.project_id
                    ORDER BY e.created_at DESC, e.id DESC LIMIT 100
                """).fetchall()
            return self.json_response([event_from_row(r) for r in rows])
        if path == "/api/calendar":
            q = parse_qs(parsed.query)
            day = q.get("date", [now_iso()[:10]])[0]
            start = q.get("start", [day])[0]
            end = q.get("end", [start])[0]
            with connect() as conn:
                rows = conn.execute("""
                    SELECT e.*, p.name AS project_name, p.project_code AS project_code FROM project_events e
                    LEFT JOIN projects p ON p.id=e.project_id
                    WHERE substr(e.created_at, 1, 10) BETWEEN ? AND ?
                    ORDER BY e.created_at ASC, e.id ASC
                """, (start, end)).fetchall()
            return self.json_response([event_from_row(r) for r in rows])
        if path == "/api/token-cost-summary":
            input_rate = float(os.environ.get("PROJECT_TRACKER_TOKEN_INPUT_RATE_PER_M", os.environ.get("PROJECT_TRACKER_INPUT_RATE", "5.00")))
            cached_input_rate = float(os.environ.get("PROJECT_TRACKER_TOKEN_CACHED_INPUT_RATE_PER_M", "0.50"))
            output_rate = float(os.environ.get("PROJECT_TRACKER_TOKEN_OUTPUT_RATE_PER_M", os.environ.get("PROJECT_TRACKER_OUTPUT_RATE", "30.00")))
            def add_cost(row):
                row["cached_input_tokens"] = min(int(row.get("cached_input_tokens") or 0), int(row.get("input_tokens") or 0))
                row["uncached_input_tokens"] = max(0, int(row.get("input_tokens") or 0) - row["cached_input_tokens"])
                row["estimated_cost_usd"] = round(
                    (row["uncached_input_tokens"] / 1000000.0 * input_rate)
                    + (row["cached_input_tokens"] / 1000000.0 * cached_input_rate)
                    + (int(row.get("output_tokens") or 0) / 1000000.0 * output_rate),
                    6,
                )
                return row
            def period_summary(conn, modifier: str):
                row = dict(conn.execute("""
                    SELECT COALESCE(SUM(input_tokens),0) input_tokens,
                           COALESCE(SUM(cached_input_tokens),0) cached_input_tokens,
                           COALESCE(SUM(output_tokens),0) output_tokens,
                           COALESCE(SUM(reasoning_tokens),0) reasoning_tokens,
                           COALESCE(SUM(total_tokens),0) total_tokens,
                           COUNT(*) updates
                    FROM project_events
                    WHERE total_tokens>0 AND datetime(created_at) >= datetime('now', ?)
                """, (modifier,)).fetchone())
                return add_cost(row)
            with connect() as conn:
                periods = {"last_24h": period_summary(conn, "-24 hours"), "last_7d": period_summary(conn, "-7 days"), "last_30d": period_summary(conn, "-30 days")}
            return self.json_response({"input_rate_per_million": input_rate, "cached_input_rate_per_million": cached_input_rate, "output_rate_per_million": output_rate, "periods": periods})
        if path == "/api/token-summary":
            day = parse_qs(parsed.query).get("date", [now_iso()[:10]])[0]
            with connect() as conn:
                totals = dict(conn.execute("SELECT COALESCE(SUM(input_tokens),0) input_tokens, COALESCE(SUM(cached_input_tokens),0) cached_input_tokens, COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(reasoning_tokens),0) reasoning_tokens, COALESCE(SUM(total_tokens),0) total_tokens, COUNT(*) updates FROM project_events WHERE substr(created_at,1,10)=? AND total_tokens>0", (day,)).fetchone())
                by_agent = [dict(r) for r in conn.execute("SELECT agent_id, COALESCE(SUM(input_tokens),0) input_tokens, COALESCE(SUM(cached_input_tokens),0) cached_input_tokens, COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(reasoning_tokens),0) reasoning_tokens, COALESCE(SUM(total_tokens),0) total_tokens, COUNT(*) updates FROM project_events WHERE substr(created_at,1,10)=? AND total_tokens>0 GROUP BY agent_id ORDER BY total_tokens DESC", (day,)).fetchall()]
                by_project = [dict(r) for r in conn.execute("""
                    SELECT e.project_id, COALESCE(p.name,'Tracker') project_name, COALESCE(SUM(e.input_tokens),0) input_tokens, COALESCE(SUM(e.cached_input_tokens),0) cached_input_tokens, COALESCE(SUM(e.output_tokens),0) output_tokens, COALESCE(SUM(e.reasoning_tokens),0) reasoning_tokens, COALESCE(SUM(e.total_tokens),0) total_tokens, COUNT(*) updates
                    FROM project_events e LEFT JOIN projects p ON p.id=e.project_id
                    WHERE substr(e.created_at,1,10)=? AND e.total_tokens>0
                    GROUP BY e.project_id, p.name ORDER BY total_tokens DESC
                """, (day,)).fetchall()]
            return self.json_response({"date": day, "totals": totals, "by_agent": by_agent, "by_project": by_project})
        if path == "/api/export":
            with connect() as conn:
                projects = [project_from_row(r) for r in conn.execute("SELECT * FROM projects ORDER BY id").fetchall()]
                agents = [agent_from_row(r) for r in conn.execute("SELECT * FROM agents ORDER BY id").fetchall()]
                requests = [dict(r) for r in conn.execute("SELECT * FROM project_requests ORDER BY id").fetchall()]
                events = [event_from_row(r) for r in conn.execute("SELECT * FROM project_events ORDER BY id").fetchall()]
            return self.json_response({"exported_at": now_iso(), "projects": projects, "agents": agents, "requests": requests, "events": events})
        return self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/projects":
            try:
                payload = self.read_json()
                stamp = now_iso()
                with connect() as conn:
                    agent = self.current_agent(conn)
                    p = validate_project(payload, agent=agent, creating=True)
                    if p["stage"] == "Production" and not agent["can_mark_production"]:
                        return self.json_response({"error": "Only Admin can directly mark a project Production. Submit a mark_production request instead."}, HTTPStatus.FORBIDDEN)
                    cur = conn.execute(
                        """
                        INSERT INTO projects (name, stage, priority, owner, due_date, summary, completed_items, next_steps, blockers, responsible_agent_id, created_by_agent_id, backup_agent_id, human_owner, next_action_owner, visibility, automation_policy, tags, last_verified_by, last_verified_at, production_approved_by, production_approved_at, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (p["name"], p["stage"], p["priority"], p["owner"], p["due_date"], p["summary"], json.dumps(p["completed_items"]), json.dumps(p["next_steps"]), json.dumps(p["blockers"]), p["responsible_agent_id"], p["created_by_agent_id"], p["backup_agent_id"], p["human_owner"], p["next_action_owner"] or p["responsible_agent_id"], p["visibility"], p["automation_policy"], json.dumps(p["tags"]), agent["id"], stamp, agent["id"] if p["stage"] == "Production" else "", stamp if p["stage"] == "Production" else "", stamp, stamp),
                    )
                    project_code = make_project_code(conn, p["responsible_agent_id"], p["name"], cur.lastrowid)
                    conn.execute("UPDATE projects SET project_code=? WHERE id=?", (project_code, cur.lastrowid))
                    row = conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone()
                    event(conn, cur.lastrowid, agent["id"], "project_created", f"Created project {project_code} {p['name']}", None, project_from_row(row))
                return self.json_response(project_from_row(row), HTTPStatus.CREATED)
            except PermissionError as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.FORBIDDEN)
            except Exception as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/requests"):
            try:
                project_id = int(parsed.path.split("/")[3])
                payload = self.read_json()
                req_type = str(payload.get("request_type", "")).strip()
                if req_type not in REQUEST_TYPES:
                    raise ValueError("invalid request_type")
                with connect() as conn:
                    agent = self.current_agent(conn)
                    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                    if not row:
                        return self.json_response({"error": "project not found"}, HTTPStatus.NOT_FOUND)
                    conn.execute(
                        """
                        INSERT INTO project_requests (project_id, request_type, requested_by_agent_id, target_agent_id, requested_value, reason, evidence_url, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                        """,
                        (project_id, req_type, agent["id"], str(payload.get("target_agent_id", "")).strip(), str(payload.get("requested_value", "")).strip(), str(payload.get("reason", "")).strip(), str(payload.get("evidence_url", "")).strip(), now_iso()),
                    )
                    event(conn, project_id, agent["id"], "request_submitted", f"Submitted {req_type} request: {str(payload.get('reason', '')).strip()}")
                return self.json_response({"ok": True}, HTTPStatus.CREATED)
            except Exception as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parsed.path.startswith("/api/requests/"):
            try:
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 4 and parts[0] == "api" and parts[1] == "requests" and parts[3] in ("approve", "reject"):
                    return self.review_request(int(parts[2]), parts[3])
            except Exception as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/updates"):
            try:
                project_id = int(parsed.path.split("/")[3])
                payload = self.read_json()
                with connect() as conn:
                    agent = self.current_agent(conn)
                    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                    if not row:
                        return self.json_response({"error": "project not found"}, HTTPStatus.NOT_FOUND)
                    before = project_from_row(row)
                    if not agent_can_edit_project(agent, before):
                        return self.json_response({"error": "Read-only: submit a request for projects you do not own."}, HTTPStatus.FORBIDDEN)
                    summary = str(payload.get("summary") or payload.get("change_summary") or "Project update").strip()
                    updates = payload.get("project_updates") if isinstance(payload.get("project_updates"), dict) else {}
                    after = before.copy()
                    if updates:
                        if not agent["is_admin"]:
                            updates.pop("responsible_agent_id", None)
                            if updates.get("stage") == "Production" and before.get("stage") != "Production":
                                return self.json_response({"error": "Only Admin can directly mark Production. Submit a mark_production request instead."}, HTTPStatus.FORBIDDEN)
                        merged = validate_project(updates, before, agent=agent, creating=False)
                        stamp = now_iso()
                        conn.execute("""
                            UPDATE projects SET name=?, stage=?, priority=?, owner=?, due_date=?, summary=?, completed_items=?, next_steps=?, blockers=?, responsible_agent_id=?, backup_agent_id=?, human_owner=?, next_action_owner=?, visibility=?, automation_policy=?, tags=?, last_verified_by=?, last_verified_at=?, updated_at=?, project_code=? WHERE id=?
                        """, (merged["name"], merged["stage"], merged["priority"], merged["owner"], merged["due_date"], merged["summary"], json.dumps(merged["completed_items"]), json.dumps(merged["next_steps"]), json.dumps(merged["blockers"]), merged["responsible_agent_id"], merged["backup_agent_id"], merged["human_owner"], merged["next_action_owner"], merged["visibility"], merged["automation_policy"], json.dumps(merged["tags"]), agent["id"], stamp, stamp, make_project_code(conn, merged["responsible_agent_id"], merged["name"], project_id), project_id))
                        after = project_from_row(conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
                    event(conn, project_id, agent["id"], "project_update_report", summary, before, after, input_tokens=payload.get("input_tokens", 0), cached_input_tokens=payload.get("cached_input_tokens", 0), output_tokens=payload.get("output_tokens", 0), reasoning_tokens=payload.get("reasoning_tokens", 0), total_tokens=payload.get("total_tokens"), model=str(payload.get("model", "")).strip(), commit_refs=payload.get("commit_refs", []))
                    ev = event_from_row(conn.execute("SELECT e.*, p.name AS project_name FROM project_events e LEFT JOIN projects p ON p.id=e.project_id ORDER BY e.id DESC LIMIT 1").fetchone())
                return self.json_response(ev, HTTPStatus.CREATED)
            except PermissionError as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.FORBIDDEN)
            except Exception as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def review_request(self, request_id: int, action: str):
        if action not in ("approve", "reject"):
            return self.json_response({"error": "invalid review action"}, HTTPStatus.BAD_REQUEST)
        with connect() as conn:
            agent = self.current_agent(conn)
            if not agent["is_admin"]:
                return self.json_response({"error": "Only Admin can approve/reject tracker requests."}, HTTPStatus.FORBIDDEN)
            req = conn.execute("SELECT * FROM project_requests WHERE id=?", (request_id,)).fetchone()
            if not req:
                return self.json_response({"error": "request not found"}, HTTPStatus.NOT_FOUND)
            if req["status"] != "pending":
                return self.json_response({"error": "request already reviewed"}, HTTPStatus.BAD_REQUEST)
            project = conn.execute("SELECT * FROM projects WHERE id=?", (req["project_id"],)).fetchone()
            if not project:
                return self.json_response({"error": "project not found"}, HTTPStatus.NOT_FOUND)
            stamp = now_iso()
            before = project_from_row(project)
            if action == "approve":
                if req["request_type"] == "owner_change":
                    target = req["requested_value"] or req["target_agent_id"]
                    if not conn.execute("SELECT 1 FROM agents WHERE id=? AND status='active'", (target,)).fetchone():
                        return self.json_response({"error": f"target agent not found: {target}"}, HTTPStatus.BAD_REQUEST)
                    new_code = make_project_code(conn, target, before["name"], req["project_id"])
                    conn.execute("UPDATE projects SET responsible_agent_id=?, next_action_owner=?, project_code=?, updated_at=? WHERE id=?", (target, target, new_code, stamp, req["project_id"]))
                elif req["request_type"] == "mark_production":
                    conn.execute("UPDATE projects SET stage='Production', production_approved_by=?, production_approved_at=?, updated_at=? WHERE id=?", (agent["id"], stamp, stamp, req["project_id"]))
                elif req["request_type"] == "restore_archived":
                    conn.execute("UPDATE projects SET archived_at='', archived_by='', updated_at=? WHERE id=?", (stamp, req["project_id"]))
                conn.execute("UPDATE project_requests SET status='approved', reviewed_by_agent_id=?, reviewed_at=? WHERE id=?", (agent["id"], stamp, request_id))
                after = project_from_row(conn.execute("SELECT * FROM projects WHERE id=?", (req["project_id"],)).fetchone())
                event(conn, req["project_id"], agent["id"], "request_approved", f"Approved {req['request_type']} request from {req['requested_by_agent_id']}", before, after)
            else:
                conn.execute("UPDATE project_requests SET status='rejected', reviewed_by_agent_id=?, reviewed_at=? WHERE id=?", (agent["id"], stamp, request_id))
                event(conn, req["project_id"], agent["id"], "request_rejected", f"Rejected {req['request_type']} request from {req['requested_by_agent_id']}", before, before)
        return self.json_response({"ok": True, "status": "approved" if action == "approve" else "rejected"})

    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/projects/"):
            try:
                project_id = int(parsed.path.rsplit("/", 1)[-1])
                payload = self.read_json()
                with connect() as conn:
                    agent = self.current_agent(conn)
                    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                    if not row:
                        return self.json_response({"error": "project not found"}, HTTPStatus.NOT_FOUND)
                    before = project_from_row(row)
                    if not agent_can_edit_project(agent, before):
                        return self.json_response({"error": "Read-only: non-Admin agents can only edit their own/responsible projects. Submit a request instead."}, HTTPStatus.FORBIDDEN)
                    if not agent["is_admin"]:
                        payload.pop("responsible_agent_id", None)
                        if payload.get("stage") == "Production" and before.get("stage") != "Production":
                            return self.json_response({"error": "Only Admin can directly mark Production. Submit a mark_production request instead."}, HTTPStatus.FORBIDDEN)
                    p = validate_project(payload, before, agent=agent, creating=False)
                    stamp = now_iso()
                    production_by = before.get("production_approved_by", "")
                    production_at = before.get("production_approved_at", "")
                    if before.get("stage") != "Production" and p["stage"] == "Production":
                        production_by = agent["id"]
                        production_at = stamp
                    conn.execute(
                        """
                        UPDATE projects SET name=?, stage=?, priority=?, owner=?, due_date=?, summary=?, completed_items=?, next_steps=?, blockers=?, responsible_agent_id=?, backup_agent_id=?, human_owner=?, next_action_owner=?, visibility=?, automation_policy=?, tags=?, last_verified_by=?, last_verified_at=?, production_approved_by=?, production_approved_at=?, updated_at=?, project_code=? WHERE id=?
                        """,
                        (p["name"], p["stage"], p["priority"], p["owner"], p["due_date"], p["summary"], json.dumps(p["completed_items"]), json.dumps(p["next_steps"]), json.dumps(p["blockers"]), p["responsible_agent_id"], p["backup_agent_id"], p["human_owner"], p["next_action_owner"], p["visibility"], p["automation_policy"], json.dumps(p["tags"]), agent["id"], stamp, production_by, production_at, stamp, make_project_code(conn, p["responsible_agent_id"], p["name"], project_id), project_id),
                    )
                    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                    after = project_from_row(row)
                    event(conn, project_id, agent["id"], "project_updated", f"Updated project {after['name']}", before, after)
                return self.json_response(after)
            except Exception as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/projects/"):
            try:
                project_id = int(parsed.path.rsplit("/", 1)[-1])
                with connect() as conn:
                    agent = self.current_agent(conn)
                    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                    if not row:
                        return self.json_response({"error": "project not found"}, HTTPStatus.NOT_FOUND)
                    before = project_from_row(row)
                    if not agent_can_edit_project(agent, before):
                        return self.json_response({"error": "Read-only: non-Admin agents can only archive their own/responsible projects."}, HTTPStatus.FORBIDDEN)
                    stamp = now_iso()
                    conn.execute("UPDATE projects SET archived_at=?, archived_by=?, updated_at=? WHERE id=?", (stamp, agent["id"], stamp, project_id))
                    event(conn, project_id, agent["id"], "project_archived", f"Archived project {before['name']}", before, None)
                return self.json_response({"ok": True, "archived": True})
            except Exception as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)


def main() -> None:
    init_db()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Project Tracker listening on http://{HOST}:{PORT} using {DB_PATH}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
