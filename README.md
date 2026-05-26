# nanobot-brief

A strictly local, air-gapped AI daily briefing agent. Reads your Google Calendar and Logseq notes, generates a concise briefing using a local LLM via Ollama, and delivers it to your phone via [ntfy.sh](https://ntfy.sh). No cloud AI APIs are used.

## How it works

```
cron (05:00) → generate-briefing.sh
                  ├─ git pull  (logseq-graph)
                  └─ nanobot agent
                        ├─ get_todays_schedule   → fetches ICS calendar feeds
                        ├─ read_recent_notes     → reads Logseq journals/pages
                        ├─ [LLM: llama3.1:8b]   → writes the briefing
                        └─ send_briefing_to_cam  → POST to ntfy.sh
```

## Prerequisites

| Requirement | Notes |
|---|---|
| Python ≥ 3.11 | System Python or pyenv |
| [Ollama](https://ollama.com) | Running on `localhost:11434` |
| `llama3.1:8b` model | `ollama pull llama3.1:8b` (or `llama3.2:3b` for 3× faster CPU) |
| SSH key on GitHub | For `git pull` on the Logseq repo |
| ntfy.sh account | Free; create a topic at ntfy.sh |
| Google Calendar ICS URLs | Private ICS link from Calendar Settings |

## Setup

### 1. Clone this repo

```bash
git clone <this-repo-url> ~/nanobot-brief
cd ~/nanobot-brief
```

### 2. Create the Python virtual environment

```bash
python3 -m venv ~/agent-env
source ~/agent-env/bin/activate
pip install nanobot-ai icalendar requests mcp gitpython pyyaml recurring-ical-events
```

### 3. Clone your Logseq graph

```bash
git clone git@github.com:YOUR_USER/your-logseq-repo.git ~/logseq-graph
```

Make sure your SSH key is added to GitHub: `cat ~/.ssh/id_ed25519.pub` then paste into GitHub → Settings → SSH keys. If `~/.ssh/known_hosts` doesn't have GitHub yet: `ssh-keyscan github.com >> ~/.ssh/known_hosts`.

### 4. Configure the agent

```bash
cp config.sample.yaml config.yaml
# Edit config.yaml with your real ICS URLs, ntfy topic, etc.
nano config.yaml
```

`config.yaml` is gitignored — your credentials stay local.

### 5. Configure nanobot

```bash
mkdir -p ~/.nanobot
cp nanobot-config.sample.json ~/.nanobot/config.json
# Replace /home/YOUR_USER with your actual username in all paths
sed -i "s|YOUR_USER|$(whoami)|g" ~/.nanobot/config.json
```

### 6. Create required directories

```bash
mkdir -p ~/daily-briefings
```

### 7. Test the pipeline

```bash
chmod +x generate-briefing.sh
./generate-briefing.sh
tail -f ~/daily-briefings/cron.log
```

The briefing appears on your ntfy.sh topic. Budget ~20 min on CPU-only hardware for `llama3.1:8b`.

### 8. Schedule with cron

```bash
crontab -e
```

Add:
```
0 5 * * * /home/YOUR_USER/nanobot-brief/generate-briefing.sh
```

## Project structure

```
nanobot-brief/
├── config.yaml              # ← gitignored; your real config with credentials
├── config.sample.yaml       # template — copy to config.yaml
├── nanobot-config.sample.json  # template for ~/.nanobot/config.json
├── generate-briefing.sh     # cron entry point
├── README.md
└── mcp-servers/
    ├── calendar/
    │   └── calendar_mcp.py  # fetches ICS feeds, returns today's events
    ├── logseq/
    │   └── logseq_mcp.py    # reads recent Logseq markdown files
    └── notify/
        └── ntfy_mcp.py      # sends briefing to ntfy.sh
```

## System files (not in this repo)

| Path | Purpose |
|---|---|
| `~/.nanobot/config.json` | nanobot provider + MCP server config |
| `~/agent-env/` | Python virtual environment |
| `~/logseq-graph/` | Logseq git repo (separate) |
| `~/daily-briefings/cron.log` | Run logs |

## Security

- `config.yaml` is gitignored. It contains private Google Calendar ICS tokens and your ntfy topic. **Do not commit it.**
- The ntfy destination URL is resolved at MCP server startup — it is never exposed as a tool argument and cannot be redirected by the LLM.
- No external LLM API calls are made. All inference runs locally through Ollama.

## Performance notes

On CPU-only hardware (no GPU), `llama3.1:8b` takes roughly 3 minutes per LLM response. The full pipeline (3 tool calls + synthesis) takes **15–30 minutes**. For a 05:00 cron job this is fine.

For faster results, pull a smaller model:
```bash
ollama pull llama3.2:3b
```
Then update `llm_model` in `config.yaml` and `"model"` in `~/.nanobot/config.json`.
