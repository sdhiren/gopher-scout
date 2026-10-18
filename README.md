# gopher-scout

A multi-agent system that finds open-source **Go CLI tools** on GitHub, proposes deeply
analysed, evidence-backed improvements, tracks which ones you approve, and implements the
approved ones on **local feature branches**. You review, push and open the PR yourself.

```
 Scout ─▶ Profiler ─▶ Analyst ─▶ Critic ─▶ [you approve] ─▶ Implementer ⇄ Reviewer ─▶ local commit
   │          │          │          │                            │            │
   └──────────┴──────────┴──────────┴──── SQLite (data/) ◀── Web UI ─────────┘
```

## Agents

| Agent | Job | LLM? | Tools it gets |
|---|---|---|---|
| **Scout** | Search GitHub for active, mid-sized Go CLI repos (`config/discovery.yaml`) and rank them | no | GitHub REST API |
| **Profiler** | Blobless clone; metrics; `go vet` / `staticcheck` / `govulncheck`; open issues; merge activity; **detect AI-contribution bans** (those repos are skipped) | no | git, Go tooling |
| **Analyst** | Read the code and propose ≤ N improvements, each with a problem statement, verbatim evidence, root cause, solution, benefits, risks and acceptance criteria | yes | Read, Grep, Glob (read-only) |
| **Critic** | Verify every claim against the code, check related issues/PRs for duplicates, score impact / confidence / merge likelihood. Only accepted ideas reach you | yes | Read, Grep, Glob (read-only) |
| **Implementer** | Implement an approved idea plus tests on a fresh branch, following your notes | yes | Read, Edit, Write, Grep, Glob, Bash (allowlisted and sandboxed) |
| **Reviewer** | `gofmt`, `go build`, `go vet` and `go test` in a Docker sandbox, then an LLM review against the acceptance criteria. Failures go back to the Implementer (up to 3 attempts) | partly | Docker, read-only tools |

Agents never call each other. The **orchestrator** (`orchestrator/pipeline.py`) reads state
from the database, runs one agent, and writes the result back. That means any stage can be
re-run, inspected, or replaced on its own.

## Guarantees (enforced in code, not left to prompts)

- **Nothing is ever pushed.** Each clone's push URL is set to `PUSH_DISABLED_BY_GOPHER_SCOUT`.
  `git push` and `gh` are blocked in the agent's shell.
- **Commits carry only your identity.** The orchestrator commits (not the agent), using your
  git config. Any `Co-Authored-By` or "Generated with" trailer is stripped, and Claude Code's
  attribution setting is turned off for every agent session.
- **Locked-down shell.** The Implementer's Bash runs inside Claude Code's OS sandbox. Writes are
  limited to the repo, and network access to the Go module proxy only. A pre-tool hook also
  enforces an allowlist (`go build/test/vet/…`, `gofmt`, read-only git) and rejects command
  substitution and redirection.
- **Untrusted tests run in Docker** (`GS_SANDBOX=auto` uses Docker when it's available).
- **Your work is never clobbered.** If a clone has uncommitted changes, `implement` refuses
  to run.
- **Isolated from your personal Claude config.** Agent sessions load no `~/.claude` or
  repo `.claude` settings.
- **Every status change goes through a state machine** (`domain/state.py`). For example, an
  idea can't reach `in_progress` without your approval.

## Idea lifecycle

```
proposed ─▶ awaiting_review ─▶ approved ─▶ in_progress ─▶ ready_for_pr ─▶ pr_opened ─▶ merged
   │              │  ▲            │              │                                   └▶ closed
   ▼              ▼  │            ▼              ▼
critic_rejected  rejected/deferred            needs_attention (review failed; work kept on the branch)
```

## Setup

Requirements: macOS or Linux, Python 3.12+, [uv](https://docs.astral.sh/uv/), Go, git,
the `gh` CLI (logged in), the Claude Code CLI (logged in; the Agent SDK uses it), and
optionally Docker.

```bash
cd gopher-scout
uv sync
cp .env.example .env   # optional — defaults are sensible
```

Optional linters make profiles richer (`staticcheck` must match your CPU architecture):

```bash
go install honnef.co/go/tools/cmd/staticcheck@latest
go install golang.org/x/vuln/cmd/govulncheck@latest
```

## Usage

Everything is triggered by hand, either from the CLI or from the UI's buttons.

```bash
make serve                      # tracking UI → http://127.0.0.1:8420
```

```bash
make discover                   # Scout
make profile                    # Profiler: top 5 discovered projects
make analyse                    # Analyst: top 3 profiled projects (uses Claude)
make critique                   # Critic: all proposed ideas (uses Claude)
make research                   # all four in sequence
```

Then open the UI, read each idea in **Awaiting your review**, add notes for the Implementer,
and click **Approve**. Then:

```bash
PYTHONPATH=src uv run python -m gopher_scout.cli implement <idea-id>
```

The **Start implementation** button on the idea page does the same thing. When an idea
reaches *Ready for PR*, its page shows the branch, the diff stat, the check results, the
reviewer's notes, a PR summary, and the exact commands to push from your fork. After you open
the PR, paste its URL on the idea page so the tracking stays complete.

Other commands: `projects`, `ideas list [--status awaiting_review]`,
`ideas approve|reject|defer <id> --notes "…"`, `profile <id> --force`,
`analyse <id>`, `critique <id>`.

## Configuration

`config/discovery.yaml` sets the topics, star range, activity window and exclusions. Every
other setting is a `GS_*` environment variable (see `.env.example`): the model
(`claude-opus-5-5`), effort per agent, the per-run USD budget, ideas per project, the sandbox
mode, and the number of attempts.

Cost control: the Profiler is free. LLM agents have hard per-run budgets, unchanged commits
are never re-profiled, the Reviewer skips the LLM pass when the build or tests fail, and the
dashboard shows spend per agent.

## Project layout

```
src/gopher_scout/
  config.py              settings (GS_* env) + discovery config
  domain/                enums, state machine, Pydantic contracts between agents
  db/                    SQLAlchemy models, session, repository (all data access)
  services/              github, git (push-disabled clones), go_tools, sandbox, process
  agents/                scout, profiler, analyst, critic, implementer, reviewer
    base.py              LLMAgent: locked-down Agent SDK session → validated model
    guards.py            Bash allowlist hook + deny rules
    prompts/*.md         system prompts (edit these to tune behaviour)
  orchestrator/          pipeline: moves projects/ideas through the stages
  web/                   FastAPI + Jinja tracking UI
  cli.py                 Typer CLI
tests/                   unit + end-to-end tests (LLM agents faked)
```

Runtime state is git-ignored: `data/` holds the SQLite database, and `workspace/<owner>/<repo>`
holds the clones.

## Development

```bash
make check    # ruff + mypy --strict + pytest
make fmt
```

### Troubleshooting

`ModuleNotFoundError: gopher_scout` when running `uv run gopher-scout`: older versions of uv
mark `.venv` hidden on macOS, and Python 3.12.11+ skips hidden `.pth` files. The Makefile works
around this with `PYTHONPATH=src`. The permanent fix is `uv self update`, then `uv sync`.
