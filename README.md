# AI Multi-Agent Project Tracker

A self-contained web dashboard and API for coordinating work across multiple AI agents.

It is designed for setups where several assistants/agents all need one shared source of truth for:

- project ownership
- project status/stage
- next actions and blockers
- cross-agent handoff requests
- production approval requests
- changelog/activity history
- calendar-style work history
- per-project changelogs
- token usage and estimated cost reporting

The app is intentionally simple: **one Python file + SQLite + vanilla HTML/CSS/JS**. No framework, no build step, no external service required.

## Features

- **Projects tab** — grouped/sorted project cards with collapsible details.
- **Agents tab** — agent registry plus each agent's owned projects as collapsible cards.
- **Requests tab** — queue for owner changes, production approvals, and other cross-agent requests.
- **Activity tab** — append-only changelog of tracker/project events.
- **Calendar tab** — Outlook-style Day/Week/Month view where each work update appears as an appointment.
- **Tokens tab** — daily token totals by agent and project.
- **Main-page cost calculator** — all-agent estimated cost for last 24h, 7d, and 30d.
- **Per-project pages** — `/project/{id}` pages with project status and full changelog.
- **Agent API keys** — each agent gets its own token in `agent_tokens.json`.
- **Safe ACL model** — all agents can read all projects, but non-admin agents can modify only their own projects.

## Quick start

```bash
git clone https://github.com/YOUR_USERNAME/ai-multi-agent-project-tracker.git
cd ai-multi-agent-project-tracker
python3 app.py
```

Open:

```text
http://127.0.0.1:5055/
```

On first run the app creates:

```text
project_tracker.db
agent_tokens.json
```

`agent_tokens.json` contains API keys. Keep it private and do not commit it.

## Configuration

All configuration is via environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PROJECT_TRACKER_DB` | `./project_tracker.db` | SQLite database path |
| `PROJECT_TRACKER_TOKEN_FILE` | `./agent_tokens.json` | Generated per-agent API-key file |
| `PROJECT_TRACKER_HOST` | `0.0.0.0` | Bind host |
| `PROJECT_TRACKER_PORT` | `5055` | HTTP port |
| `PROJECT_TRACKER_TOKEN_INPUT_RATE_PER_M` | `5.00` | USD per 1M uncached input tokens |
| `PROJECT_TRACKER_TOKEN_CACHED_INPUT_RATE_PER_M` | `0.50` | USD per 1M cached input / cache-read tokens |
| `PROJECT_TRACKER_TOKEN_OUTPUT_RATE_PER_M` | `30.00` | USD per 1M output tokens |

Legacy `PROJECT_TRACKER_INPUT_RATE` and `PROJECT_TRACKER_OUTPUT_RATE` are still accepted as fallbacks for older deployments.

Example:

```bash
PROJECT_TRACKER_HOST=127.0.0.1 \
PROJECT_TRACKER_PORT=5055 \
PROJECT_TRACKER_DB=$PWD/data/project_tracker.db \
PROJECT_TRACKER_TOKEN_FILE=$PWD/data/agent_tokens.json \
python3 app.py
```

## Agent permissions

Default ACL:

| Capability | Admin agent | Other agents |
|---|---:|---:|
| Read all projects | yes | yes |
| Create projects | yes | yes |
| Edit own projects | yes | yes |
| Edit other agents' projects | yes | no |
| Archive own projects | yes | yes |
| Archive others' projects | yes | no |
| Reassign owner | yes | request only |
| Mark Production | yes | request only |
| Change permissions | yes | no |

## API basics

Read projects:

```bash
curl -sS http://127.0.0.1:5055/api/projects
```

Use an agent API key:

```bash
curl -sS \
  -H "X-API-Key: YOUR_AGENT_API_KEY" \
  http://127.0.0.1:5055/api/projects
```

Create a project:

```bash
curl -sS -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_AGENT_API_KEY" \
  http://127.0.0.1:5055/api/projects \
  -d '{
    "name": "Example Integration Work",
    "stage": "Planning",
    "priority": "Medium",
    "summary": "Set up an example integration project.",
    "next_steps": ["Define requirements", "Assign owner"]
  }'
```

Record a work update with tokens and commits:

```bash
curl -sS -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_AGENT_API_KEY" \
  http://127.0.0.1:5055/api/projects/PROJECT_ID/updates \
  -d '{
    "summary": "Implemented the first version of the integration.",
    "input_tokens": 12000,
    "cached_input_tokens": 8000,
    "output_tokens": 2500,
    "reasoning_tokens": 500,
    "total_tokens": 14500,
    "model": "model/provider used",
    "commit_refs": ["abc1234 Initial implementation"],
    "project_updates": {
      "completed_items": ["Created API client", "Added tests"],
      "next_steps": ["Review edge cases"],
      "blockers": []
    }
  }'
```

If an agent cannot access exact token usage, it should report `0` rather than guessing. `cached_input_tokens` is treated as a subset of `input_tokens` for cost calculations; `reasoning_tokens` is tracked separately for visibility and is not added on top of output cost when providers already include reasoning in output tokens.

## Project shorthand IDs

The tracker generates project codes in this format:

```text
AGENT-ACRONYM
```

Examples:

```text
ADMI-PTS   = Admin Agent / Project Tracker Setup
AGEN-AADS  = Agent Alpha / Agent Alpha Daily Sync
```

Codes are generated from the responsible agent and project title. If there is a collision, the tracker adds a numeric suffix.

## Suggested cron prompt for agents

Each agent can run a recurring job similar to:

```text
Read the AI Multi-Agent Project Tracker. Review only projects assigned to this agent. For each project, update status, blockers, next steps, token usage if available, model name, and commit refs using /api/projects/{id}/updates. For projects owned by other agents, submit a request instead of editing directly. Do not fabricate token counts.
```

Stagger jobs so agents do not all write at once, for example:

```text
Admin:       0 20 * * *
Agent Alpha: 5 20 * * *
Agent Beta: 10 20 * * *
Agent Gamma:15 20 * * *
```

## Deployment notes

For a small LAN/internal deployment, run behind a reverse proxy or systemd service. The built-in server is intentionally simple.

Minimum systemd-style command:

```bash
PROJECT_TRACKER_HOST=127.0.0.1 PROJECT_TRACKER_PORT=5055 python3 /opt/ai-multi-agent-project-tracker/app.py
```

Put a reverse proxy such as Nginx, Caddy, Traefik, or Tailscale Funnel in front if exposing beyond localhost/LAN.

## Security notes

- Do not commit `agent_tokens.json`.
- Do not commit `project_tracker.db` if it contains private project data.
- Do not expose this directly to the public Internet without authentication/reverse-proxy controls.
- API keys are bearer secrets. Rotate them by deleting or editing the token file and restarting.
- Use HTTPS at the proxy layer if traffic leaves the host.

## License

MIT
