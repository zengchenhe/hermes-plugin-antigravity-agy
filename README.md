# antigravity-agy — Google Antigravity subscription provider for Hermes Agent

[![tests](https://github.com/Realtyxxx/hermes-plugin-antigravity-agy/actions/workflows/tests.yml/badge.svg)](https://github.com/Realtyxxx/hermes-plugin-antigravity-agy/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

English | [简体中文](README.zh-CN.md)

> [!WARNING]
> **Unmaintained — use [`antigravity-subscription-directsdk`](https://github.com/soyelmismo/hermes-antigravity-subscription) instead.**
> This plugin flattens the whole conversation into one agy user message, and `agy` silently truncates a single user message beyond roughly 65–69k tokens (keeping the beginning). Long sessions therefore lose their newest turns and the model can loop. `antigravity-subscription-directsdk` keeps a persistent agy worker and sends only new messages, which avoids this. This repository is archived and read-only.

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) model-provider plugin that runs Hermes on your **Google Antigravity subscription** by driving the official, already signed-in [`agy` CLI](https://antigravity.google/docs/cli/install).

**agy is only the model; Hermes executes every tool.** agy runs as a custom agent with all of its built-in tools switched off, Hermes' tools are exposed through an inventory-only MCP server, and each tool call is intercepted and handed back to Hermes, which runs it with its own approvals and sandbox.

> **Disclosure** — inference goes through Google's official `agy` binary and counts against the Antigravity/Gemini subscription of whatever account `agy` is signed in to. The plugin never reads, copies or links agy's credential files; sign-in stays entirely inside `agy`. Automating a subscription client may be subject to Google's terms for your account — review them before use. This is an independent community project, not affiliated with Google or Nous Research.

## Highlights

- **Official client, no credential handling** — spawns the `agy` binary in print mode; no OAuth, no token files, no reverse proxy.
- **Built-in tools off, not just denied** — a generated [custom agent](docs/design.md#31-turning-off-agys-built-in-tools-a-markdown-custom-agent) with `excludeDefaultComponents: true` removes agy's file/command tools from the model and cuts agy's base prompt from ~24.7k to ~1k tokens.
- **Native function calling** — Hermes tools are called through agy's MCP dispatcher with schema-validated arguments, not parsed out of free text.
- **Fail closed** — a run is killed and the request fails if agy tries any tool outside Hermes' inventory, or if agy silently falls back to its default agent.
- **Fast tool loops** — pre-warmed agy processes skip agy's ~5.5 s startup on the next request of a tool loop: the two-step tool benchmark went from 44.5 s to 30.2 s ([perf](docs/perf.md)).
- **Streaming, usage and cache stats** — text deltas stream as they arrive; usage (including cached and thinking tokens) is reported in OpenAI shape. agy's prompt cache hit ~89% of input tokens in real sessions.
- **Clear errors** — distinct errors for a missing CLI, signed-out CLI, exhausted quota, timeouts and tool violations; agy's own message is surfaced (for example the list of valid models).
- **`/model` and gateway ready** — works with `/model <slug> --provider antigravity-agy` and the `/model` picker, in the CLI, TUI and messaging gateway.

## Requirements

- Hermes Agent **≥ 0.21.4**.
- The `agy` CLI (tested with 1.2.10–1.2.11), signed in **as the user Hermes runs as**: run `agy` once interactively, then check `agy -p=/quota`.
- Linux or macOS (agy is managed as a POSIX process group).

## Install

```bash
hermes plugins install Realtyxxx/hermes-plugin-antigravity-agy --no-enable
```

**Installing with an AI agent?** Hand it a ready-made prompt from [docs/install.md](docs/install.md): prerequisites, install, validation, a smoke test, an optional tool-execution audit and troubleshooting, with guardrails (no credential access, no config change or gateway restart without asking).

Or clone it into your Hermes home:

```bash
git clone https://github.com/Realtyxxx/hermes-plugin-antigravity-agy ~/.hermes/plugins/antigravity-agy
```

Hermes discovers the plugin through its `plugin.yaml` (`kind: model-provider`); no `config.yaml` change is needed. A running gateway loads new or updated plugin code only after a restart.

## Use

One-off, without changing your default model:

```bash
hermes chat -q "Summarize README.md" --provider antigravity-agy --model gemini-3.8-flash-medium
```

Inside an interactive session (CLI, TUI or a messaging gateway), for the current session:

```text
/model gemini-3.8-flash-medium --provider antigravity-agy
/model pro --provider antigravity-agy        # alias
/model                                       # picker → "Antigravity (agy CLI)"
```

As the default, in `~/.hermes/config.yaml`:

```yaml
model:
  provider: antigravity-agy
  default: gemini-3.8-flash-medium
```

## Models

The model list comes live from `agy models` and is not filtered; the table below is the fallback used when `agy` cannot be queried. The reasoning effort is part of the slug.

| Slug                                 | Context window reported to Hermes |
| ------------------------------------ | --------------------------------- |
| `gemini-3.8-flash-{low,medium,high}` | 1,000,000                         |
| `gemini-3.7-flash-{low,medium,high}` | 1,000,000                         |
| `gemini-3.6-flash-{low,medium,high}` | 1,000,000                         |
| `gemini-3.1-pro-{low,high}`          | 1,000,000                         |
| `claude-sonnet-4-6`                  | 200,000                           |
| `claude-opus-4-6-thinking`           | 200,000                           |
| `gpt-oss-120b-medium`                | 128,000                           |

- Aliases: `flash` → `gemini-3.8-flash-medium`, `pro` → `gemini-3.1-pro-high`, `sonnet` → `claude-sonnet-4-6`, `opus` → `claude-opus-4-6-thinking`.
- A bare family such as `gemini-3.8-flash` maps to its `-medium` variant (else `-high`, else `-low`).
- Quota is tracked per model group (Gemini; Claude and GPT) with a 5-hour and a weekly window: `agy -p=/quota`.

## Configuration

All settings are optional environment variables of the Hermes process.

| Variable                      | Default                     | Purpose                                                                                    |
| ----------------------------- | --------------------------- | ------------------------------------------------------------------------------------------ |
| `ANTIGRAVITY_AGY_COMMAND`     | `agy` on `PATH`             | agy executable to run                                                                      |
| `ANTIGRAVITY_AGY_PREWARM`     | `2`                         | Spare agy processes per request shape (`0` disables pre-warming, max 4 in total)           |
| `ANTIGRAVITY_AGY_PREWARM_TTL` | `300`                       | Seconds an unused spare may idle                                                           |
| `ANTIGRAVITY_AGY_CAPTURE_WAIT` | `2`                        | Seconds to wait for one correlated `tools/call` capture before failing closed             |
| `ANTIGRAVITY_AGY_CLEANUP`     | `unused`                    | Which of the plugin's own agy conversation records go to the trash: `unused`, `all`, `off` |
| `ANTIGRAVITY_AGY_DATA_DIR`    | `~/.gemini/antigravity-cli` | agy data directory (used only to locate the plugin's own records for cleanup)              |
| `ANTIGRAVITY_AGY_TRACE_DIR`   | unset                       | Write every agy run's raw NDJSON event stream here, for auditing                           |

The process that runs Hermes (for example a systemd unit for the gateway) must also have whatever environment `agy` itself needs, such as proxy variables.

## How it works

1. For each request the plugin writes a private workspace containing a custom agent `.agents/agents/hermes-relay/agent.md`: `excludeDefaultComponents: true`, `inheritMcp: false`, one MCP server (`inert_mcp.py`), and Hermes' system prompt plus a rendered catalog of Hermes' tools as the agent's system prompt.
2. The conversation history is flattened into one stream-json user message on agy's stdin; agy's NDJSON output is relayed to Hermes as OpenAI-style chunks.
3. When the model calls a Hermes tool (`call_mcp_tool` → `hermes/<tool>`), `inert_mcp.py` records that `tools/call` and never answers it. The plugin waits for the one correlated capture, kills agy, and returns those arguments to Hermes. The `step_update` display text is not executed. A missing, unreadable, or uncorrelated capture kills the run and fails the request. Hermes executes the returned call and sends the next request.
4. Any other tool step, or an agent fallback detected in agy's log, kills the run and fails the request.

Full details, measurements and trade-offs: [docs/design.md](docs/design.md).

### Auditing that only Hermes runs tools

```bash
ANTIGRAVITY_AGY_TRACE_DIR=/tmp/agy-trace hermes chat -q "…" --provider antigravity-agy --model gemini-3.8-flash-low
python scripts/audit_trace.py /tmp/agy-trace    # exits 1 on any non-Hermes tool step; prints "violations: 0" otherwise
```

Traces contain conversation content; delete them when done.

## Compared with `antigravity-subscription-directsdk`

The Hermes plugin catalog already lists [`antigravity-subscription-directsdk`](https://github.com/soyelmismo/hermes-antigravity-subscription), which also drives `agy`. The approaches differ:

|                          | antigravity-subscription-directsdk                                       | antigravity-agy (this plugin)                                                   |
| ------------------------ | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------- |
| agy's built-in tools     | Present in the default agent; runs are killed if agy reports a tool step | Removed from the model with a custom agent; any non-Hermes tool step also kills |
| agy base prompt          | agy's default agent (its base prompt measured ~24.7k tokens here)        | Relay agent (~1k tokens)                                                        |
| Tool-call protocol       | `<tool_call>` tags in the model's text, parsed by the plugin             | Native function calling through MCP with schema-validated arguments             |
| Credentials              | Symlinks agy's OAuth token into a per-session temp `HOME`                | Never touches agy's credential files                                            |
| Agent-fallback detection | —                                                                        | Yes (agy silently falls back to its default agent on a bad agent definition)    |
| Pre-warming              | —                                                                        | Yes                                                                             |
| Effort selection         | Maps Hermes reasoning effort to the slug                                 | Effort is chosen by the slug; `reasoning_effort` is ignored                     |
| Platforms                | Linux, macOS, Windows                                                    | Linux, macOS                                                                    |

## Known limitations

- **One tool call per request.** agy dispatches tools one at a time, so parallel tool calls become successive requests.
- **Text only.** Images are replaced by a placeholder.
- **Three fundamental agy tools** (`list_resources`, `read_resource`, `manage_task`) cannot be hidden from the model; if it calls one, the request fails rather than the tool running.
- **Conversation records.** Runs that handled a prompt still leave agy conversation records under `~/.gemini/antigravity-cli/` unless `ANTIGRAVITY_AGY_CLEANUP=all`.
- **Memory.** Each idle spare uses ~416 MB; lower `ANTIGRAVITY_AGY_PREWARM` in memory-constrained gateways.
- **Ignored parameters.** Hermes' `reasoning_effort`, `temperature`, `max_tokens` and `response_format` have no effect.
- **`/model` forms.** `/model antigravity-agy/<slug>`, a bare slug without `--provider`, and `--provider antigravity-agy` without a model are limited by Hermes core routing.
- **Estimated context windows.** agy's real limits are not published.

More in [docs/design.md § 4](docs/design.md#4-known-limitations).

## Development

```bash
uv venv && uv pip install pytest pyyaml requests rich httpx ruamel.yaml
HERMES_AGENT_REPO=~/.hermes/hermes-agent .venv/bin/python -m pytest -q
hermes plugins validate .
```

The test suite drives a fake `agy` (`tests/fake_agy.py`) and never spends quota. Hermes core is imported from a hermes-agent checkout (`HERMES_AGENT_REPO`, default `~/.hermes/hermes-agent`). `scripts/bench.py` runs real end-to-end benchmarks ([docs/perf.md](docs/perf.md#6-reproducing)).

## Acknowledgements

The architecture follows Nous Research's [`claude-subscription-directsdk`](https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk) plugin, which drives the official Claude Code CLI with an inventory-only MCP server so that Hermes owns tool execution.

## License

[MIT](LICENSE)
