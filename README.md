# AgentMemorySync

Here's the annoying thing this project fixes: if you run Claude Code and Codex CLI on the same repo, they have absolutely no idea the other one exists. Every new session starts from zero, same files get re-read, same decisions get re-explained, same ground gets re-covered. The two agents can even edit the same file five minutes apart and neither one has a clue the other was just in there.

AgentMemorySync is a shared timeline plus a local dashboard that closes that gap. Every turn either agent takes gets recorded, the next session (from *either* agent) gets a compact summary of what already happened, file edits get cross-checked for conflicts, and you get one place to watch real measured token savings and fire off new prompts to either agent.

It's self-hosted and single-user on purpose. This runs on your own machine, talks to your local Claude Code / Codex install, and stores everything locally. No account on some central server, nothing leaves your machine unless you explicitly dispatch a prompt.

## How it works

- **`store.py`**: basically one SQLite file
  (`%LOCALAPPDATA%\AgentMemorySync\store.db`) holding the cross-agent
  timeline, file-touch history (so it can catch conflicts), the one admin
  account, dispatch job history, and pending agent questions. It's keyed by
  git repo root, so both agents' work on the same repo lands in one timeline
  even if they're working from different subdirectories.
- **Claude Code integration**: `hooks/session_start.py` and `hooks/stop.py`
  inject and log the same canonical context that Codex uses. Which agent
  produced something is just a label for where it came from, it's never
  used to hide anything from the other agent. The app doesn't keep a
  separate Claude-only memory copy anymore.
- **Codex integration (two paths, pick whichever matches your install)**:
  - `hooks/*.py`: if your Codex CLI has a documented hook system
    (SessionStart/Stop/PostToolUse), same mechanism as Claude Code.
  - `mcp_server.py`: if you've got the newer MCP-capable Codex (no hooks,
    but it supports `[mcp_servers.*]` in `config.toml`). This exposes
    shared-memory tools plus repository search, symbol explanation,
    references, architecture maps, git-aware related changes, and index
    status.
  - Not sure which one you've got? See "Which Codex do I have?" below.
- **`history.py` + `backfill.py`**: pool the history you already have.
  Reads Claude Code's transcripts and Codex's own session rollouts
  (`~/.codex/sessions/**/rollout-*.jsonl`) for a repo and merges them into
  one shared corpus of user/assistant messages, tool calls, and tool
  results. Hidden reasoning and provider system instructions get filtered
  out. This runs automatically when a project opens and you can re-run it
  any time with "Import history"; it won't duplicate anything, and it
  upgrades old summary-only rows in place instead of adding new ones.
- **`app.py` + `static/`**: the dashboard (FastAPI), built to feel dense
  with info and alive:
  - **Deploy an agent**: send a task to Claude Code or Codex; it runs
    headless against the repo with the pooled context already loaded.
  - **Live agent logs**: each run streams its steps in real time as a
    terminal-style log parsed straight from the CLI's own JSONL event
    stream: the agent's messages, its reasoning, the exact shell commands
    it runs **with their output and exit codes**, and file edits, each
    one styled differently so it actually reads like watching the agent
    work.
  - **Prompting presets**: Research / Plan / Execute / Review buttons that
    set the agent, edit permission, and a starting prompt for the common
    modes.
  - **Handoff workflow**: turn a finished run (say, Codex doing research)
    into a structured `handoff-*.md` in the repo, then one click prefills a
    review-and-execute prompt for Claude Code. This is the hybrid loop: one
    agent researches or plans, the other verifies it against the codebase
    and integrates it.
  - **Connection status**: shows whether each CLI is even found on this
    machine, with a hint if Claude Code needs a one-time headless login.
  - **Parallel agents**: deploy more than one at once; a "Live agents"
    strip at the top shows every running agent across every project.
  - **Coordinated follow-ups**: pick any settled deployment and split its
    leftover work into 2 to 8 bounded tasks. Each child can use Claude Code or
    Codex, its own model override, and its own edit permission. Children
    start in parallel off the selected deployment's frozen context
    snapshot, and stay independently watchable, redirectable, and
    cancelable.
  - **Agent questions in the dashboard** show up in one prominent global
    panel where you can answer with a choice or free text and the
    deployment just continues. Pending questions stay visible even while
    you've got a different project selected.
  - **Compute & efficiency**: real measured token usage for BOTH agents
    (Claude transcripts + Codex rollouts + per-run dispatch usage).
  - **File upload**: attach a file and it gets saved into the repo and
    referenced in the prompt so the agent can read it.
  - Plus the per-agent activity chart, pooled timeline, and conflict
    alerts.
- **`telemetry.py`**: keeps native model usage, shared-corpus size, active
  context size, and delivery receipts as separate numbers. It never turns
  old usage into a made-up savings claim. See "Token efficiency" below.
- **`dispatch.py`**: send a prompt to Claude Code or Codex right from the
  dashboard, headlessly (`claude -p` / `codex exec` under the hood).
  **Read the safety note below before you use this.**
- **`auth.py`**: self-hosted, one admin account. First visit to the
  dashboard creates it, and every `/api/*` route needs you logged in after
  that.

## Canonical pooled context

Each repo you're tracking gets one canonical context preview. The
dashboard's **Shared agent briefing** workspace shows you exactly the
compact working set that hooks, MCP lookups, handoffs, and newly queued web
deployments all pull from. Internal `context_injected` telemetry never
shows up in it.

There are two layers here, and they're kept deliberately separate:

- The **shared corpus** holds all the shareable user/assistant messages and
  tool evidence pulled in from both providers. Hidden reasoning, provider
  system instructions, credentials, and encrypted payloads are excluded.
- The **active working set** is the compact session digests plus whatever's
  pinned or recent. It's identical for Claude Code and Codex. Full native
  history is still searchable by either agent through provider-neutral
  hybrid retrieval, SQLite FTS5/BM25 lexical ranking fused (via reciprocal
  rank fusion) with hashed n-gram embedding similarity, so a rephrased
  question can still pull up a passage that shares no exact words with it
 , via the `shared_context.py` command (and MCP clients get it through
  `search_project_context`); none of that gets copied into every prompt.
  Whenever a working set isn't empty, it includes the exact read-only
  command you'd run to dig deeper.

There's no provider-private context field and no per-agent visibility
filter hiding anything. Every entry that's included is available to both
agents; every excluded one goes to neither. Delivery receipts (in Context
economy) show which agent actually got which snapshot.

## Repository intelligence

Repository knowledge gets indexed separately from agent memory. The
incremental index keeps supported source and doc files with FTS5 search,
content hashes, deterministic summaries, and the latest git
commit/author/date. Python files also get AST-derived classes, functions,
methods, imports, calls, references, inheritance, and test links. Delete or
change a file and it's invalidated on the next refresh; anything unchanged
doesn't get reparsed for no reason.

MCP clients can use `index_repository`, `search_code`, `explain_symbol`,
`find_callers`, `find_references`, `related_changes`, `get_architecture`,
and `index_status`. Search/navigation tools refresh the index incrementally
before answering, so results follow your actual working tree instead of
just whatever the last commit looked like. Same stuff is available without
MCP too:

```powershell
.venv\Scripts\python.exe repository_context.py index --project C:\path\to\repo
.venv\Scripts\python.exe repository_context.py search --project C:\path\to\repo --query "dispatch interaction"
.venv\Scripts\python.exe repository_context.py symbol --project C:\path\to\repo --name run_dispatch_job
.venv\Scripts\python.exe repository_context.py references --project C:\path\to\repo --name run_dispatch_job --kind call
.venv\Scripts\python.exe repository_context.py map --project C:\path\to\repo --directory tests
```

### Pure-context invariant

1. Claude Code and Codex native histories are mirrored into one repository
   corpus; the producer label is provenance, not an access rule.
2. One deterministic working-set hash is rendered for every consumer.
   There's no Claude variant and no Codex variant.
3. Dashboard dispatches send that snapshot through stdin, hooks inject it
   at session start, and MCP retrieval returns the same text. Every
   successful delivery gets logged with agent, route, size, and hash.
4. Older raw evidence is kept once and searched through the shared corpus;
   it's not duplicated into provider-private memory or stuffed into every
   active prompt.

- **Keep on every run** keeps an included entry ahead of the rolling
  newest-entry window.
- **Remove from briefing** takes an entry out of future context without
  deleting its audit record.
- **Rewrite agent text** layers a context-only summary on top; the
  original event summary stays untouched underneath and can be brought
  back with **Restore captured text**.
- **Durable briefings** are context entries you wrote yourself, and
  they're the only kind you can actually delete.
- **Newest entries sent** controls how many unpinned included entries get
  picked (somewhere from 1 to 100).
- **Categories** sort entries into decisions, constraints, tasks,
  artifacts, insights, notes, or general activity. They're labeled
  conservatively by default, and you can override that from the category
  menu or by just dragging a card into a different section, none of this
  changes what actually gets rendered into the prompt.
- **Coverage** charts purpose, source, and briefing readiness, then graphs
  which categories are tied to entries currently selected for agents. Each
  card shows an imported task next to its result, explains when the record
  actually helps, shows its prompt cost and delivery state, and flags
  low-detail text that should probably get rewritten.

Pinned entries show up first, then recent ones, both sorted oldest-to-newest
within their own section. Content edits and notes are capped in size, and
the whole rendered context has a hard 200,000-character safety ceiling
(roughly 50,000 tokens). Native transcripts stay in the corpus even after
their compact digests age out under the same recent-entry limit as
everything else unpinned. Editing pooled context directly changes what
future agents receive, so treat agent-written entries as untrusted prompt
content and read them before pinning.

Web deployments snapshot this preview the moment the job gets queued. That
snapshot is stuck onto the front of the job's prompt, shown under **Context
used** on its deployment row, and later context edits don't touch it. Your
original prompt stays stored separately.

Authenticated context-management endpoints are:

- `GET /api/context?project=...`
- `PATCH /api/context/events/{event_id}?project=...`
- `POST /api/context/notes`
- `DELETE /api/context/notes/{event_id}?project=...`
- `PUT /api/context/settings`

Existing SQLite stores migrate automatically and idempotently the moment
you connect. Old event rows default to included, unpinned, and without an
overlay. Heads up: copying a preview or dispatching it does expose that
repo's context to whichever local agent CLI you picked, and whatever
service that CLI talks to.

## Enterprise controls

Whoever signs up first becomes the administrator. Admins can create active
or disabled member accounts and hand out `viewer`, `editor`, or `operator`
access on a per-repo basis. Viewers can look at repo data, editors can also
curate pooled context, and operators can dispatch and control agents on top
of that. The global user, permission, policy, agent-model, and audit APIs
are admin-only.

Secret redaction is on by default for anything newly saved, context,
prompts, agent results, logs, interaction replies. Each repo can opt out or
ask for a scan of existing data. It catches the common stuff: private keys,
provider tokens, bearer credentials, credential assignments, think of it
as a DLP guardrail, not a replacement for whatever secret scanning your
provider already does.

You can set retention per repo anywhere from 1 to 3650 days (`0` keeps
everything forever). The policy applies immediately, and admins can re-run
it any time for one repo or all of them. Any mutating admin or dashboard
action writes a secret-scrubbed, SHA-256 hash-chained audit record, and you
can export those as JSONL or CSV.

The administration endpoints live under `/api/admin`: `users`,
`permissions`, `policy`, `retention/run`, and `audit/export`.

## Install

1. Clone this repo, then:
   ```
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   .venv\Scripts\python.exe setup.py
   ```
   `setup.py` renders the hook/MCP config templates for *your* machine into
   `config/generated/` (gitignored), it never touches your global config
   directly.
2. Wire up Claude Code: merge `config/generated/claude_settings.json` into
   `%USERPROFILE%\.claude\settings.json`.
3. Wire up Codex, pick one (see below): merge `config/generated/codex_hooks.toml`
   or `config/generated/codex_mcp.toml` into `%USERPROFILE%\.codex\config.toml`.
4. Restart both CLIs.
5. Run the dashboard:
   ```
   .venv\Scripts\python.exe -m uvicorn app:app --port 8756
   ```
   Open `http://localhost:8756`, first visit creates your admin account.

### First run: the dashboard starts empty

Projects show up automatically once the hooks record some activity (i.e.
after you've actually run Claude Code / Codex in a git repo with the hooks
wired up). If you don't want to wait around for that:

- Use **"+ Add a project"** in the sidebar and paste a repo path (e.g.
  `C:\path\to\your\repo`). It's added and selected right away.
- Selecting a project reveals its per-project panels: **Token Efficiency**,
  **Dispatch a prompt**, and **Dispatched runs**.
  (The aggregate "All projects" view hides these on purpose, they only
  make sense per-project.)
- **Dispatch** shells out to your local `claude` / `codex`. On Windows
  their install locations get auto-detected; if that fails, set
  `AGENT_MEMORY_SYNC_CLAUDE_BIN` / `AGENT_MEMORY_SYNC_CODEX_BIN` to the
  full `.exe` paths before starting the dashboard.
- Each agent's connection chip (under the dispatch panel) has an editable
  model field next to it. Leave it blank to use that CLI's own default
  model, or type an exact model id (e.g. `claude-sonnet-5`, `gpt-5-codex`)
  to pin every future dispatch to it. Saved via `GET`/`PUT /api/agents/models`.
- A settled run's **Coordinate follow-up** action opens the batch builder.
  `POST /api/dispatch/{job_id}/coordinate` records parent/batch
  relationships and launches every task concurrently. Keep edit-enabled
  assignments non-overlapping, since coordinated agents share the same
  working tree.

### Which Codex do I have?

- Open `%USERPROFILE%\.codex\config.toml`. If it already has one or more
  `[mcp_servers.*]` entries, you've got the MCP-capable product, use
  `codex_mcp.toml`.
- If instead you've got documented hook support (a `/hooks` command, or a
  `hooks.json`), use `codex_hooks.toml`.
- Both configs can coexist in this repo; you only wire up the one that
  matches your install. (Honest caveat: the MCP path has been tested
  end-to-end with a real MCP client and a real Codex `exec` invocation on
  the machine this was built on, but it hasn't been checked yet against
  every possible Codex product variant's own tool-calling behavior.)

## Token efficiency

The dashboard only reports numbers it can actually back up: native usage
for each provider, shareable corpus text, active working-set size,
compression ratio, and context delivery receipts. If Claude and Codex show
similar usage totals, that just means both tools burned through similar
amounts of compute historically, that's not a pooling baseline and it's
never labeled as savings.

"Verified savings" compares two numbers that are actually measured and
stored by the app, not some simulated control run. Every pooled session
carries its real native usage (`source_tokens`, pulled straight from that
session's own transcript), basically what it cost to originally produce
that history. Every time shared context gets delivered to a session, that
delivery snapshots the active window's total native usage and logs the
size of the compact digest that actually got sent. Savings for that one
delivery is native usage at delivery time minus digest tokens delivered,
added up across every delivery ever recorded; efficiency gain is that total
divided by the total native usage snapshotted. Deliveries recorded before
this snapshot mechanism existed reconstruct the historical pool size from
the permanent, timestamped event log instead of getting credited with
today's (bigger) pool. Until at least one delivery reflects a pool with
nonzero native usage, the UI just says **awaiting baseline** and reports no
savings number. The corpus-to-working-set compression percentage is a
separate, always-available measured size ratio, and it's not presented as
token savings either, those are two different numbers.

## Dispatching prompts, read this first

The dashboard can send a prompt to Claude Code or Codex directly
(`claude -p` / `codex exec` under the hood), as a background job you can
watch complete. **Dispatching with "allow edits" checked (the default)
lets the agent modify files in that project with zero further
confirmation, same as if you ran the CLI non-interactively yourself.**
Uncheck it to run read-only/plan-only instead. Dispatch only works on
projects already tracked by AgentMemorySync (not any random path) and
requires being logged in, but the permission level itself is entirely
yours to control per request.

## Limitations

- Claude Code + Codex only, no Cursor, Windsurf, or Aider support.
- Repository lexical search covers common source/config/document formats;
  structural symbol and relationship resolution currently covers Python
  only.
- Token telemetry covers Claude Code transcripts and Codex rollouts
  through the provider registry; any other provider would need its own
  usage adapter written for it.
- Authentication and repository RBAC are local to one deployment, there's
  no SSO/SCIM integration or tenant boundary here.
- SQLite content isn't encrypted at rest. Use an encrypted volume or bolt
  on a managed SQLCipher/key-management setup yourself before storing
  anything regulated.
- Dispatch workers run inside the dashboard process, so they don't survive
  a process restart.
- Claude Code's native history path discovery has only been verified
  end-to-end on Windows. POSIX path encoding and data-directory handling
  are in there but still need real integration testing on macOS and Linux.
- The Codex MCP path has been verified against a generic MCP client and a
  real `codex exec` invocation, but not against every Codex product's own
  tool-calling behavior end-to-end.

## Retrieval benchmark

Run the versioned retrieval regression suite with:

```powershell
.\.venv\Scripts\python.exe benchmark.py validate
.\.venv\Scripts\python.exe benchmark.py export-dataset --output retrieval-v1.json
.\.venv\Scripts\python.exe benchmark.py run
.\.venv\Scripts\python.exe benchmark.py run --json --output benchmark-report.json
```

The built-in dataset has 180 labeled queries: 30 each for exact lookup,
paraphrased decision recall, code navigation, change localization,
dependency tracing, and architecture. It compares the legacy SQL `LIKE`
scan with the production FTS5/BM25 path and reports Recall@5, nDCG@10,
evidence precision, MRR, estimated evidence tokens, p50/p95 latency, and
corpus/indexing cost.

The suite is synthetic and exists to catch retrieval regressions. It isn't
evidence of real-world task completion or parity with another product. The
report leaves answer-level task completion and citation correctness unset,
and lists dense, hybrid, code-graph, and Bluebird comparisons as
unavailable until those systems can actually run against it. To score one
of those systems later, hand it a JSON object mapping query ids to ranked
document-id arrays:

```powershell
.\.venv\Scripts\python.exe benchmark.py run --predictions dense=predictions.json
```

No `CONTRIBUTING.md`/`SECURITY.md` yet, deliberately holding off given
how small this project still is (a handful of Python files, one
maintainer). Happy to add them once/if that changes.

## Contributing

Issues and PRs welcome. This is a small, opinionated tool built for a
specific workflow (Claude Code and Codex on the same repo), if you're
extending it to another agent or a different sync model, opening an issue
to discuss the approach first is appreciated.
