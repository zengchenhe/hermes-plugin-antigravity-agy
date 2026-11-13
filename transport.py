"""Request-scoped agy transport: agy is only the model, Hermes owns every tool.

One Hermes chat-completions request = one fresh ``agy`` print-mode run:

1. A private temp workspace holds a Markdown custom agent (``.agents/agents/hermes-relay``) with
   ``excludeDefaultComponents: true`` (no built-in tools, no default prompt sections),
   ``inheritMcp: false`` (no user MCP servers) and exactly one MCP server: ``inert_mcp.py``,
   which advertises Hermes' tools but never executes them.
2. Hermes' system prompt plus a rendered tool catalog become the agent's system prompt; the full
   message history is flattened into a single stream-json user message on stdin.
3. The NDJSON stream is relayed as OpenAI-style chunks. The first ``call_mcp_tool`` step aimed at
   the ``hermes`` server ends the run only after the inert MCP ``tools/call`` capture is correlated.
   Those captured arguments are what Hermes receives. The ``step_update`` display text is never
   executed. A missing or uncorrelated capture fails the request. Any other tool step also kills
   the run, as an error.

Performance (docs/perf.md): agy spends ~5 s booting (sign-in and model-config round trips) before
it reads its prompt. A request that carries tools therefore boots up to ``ANTIGRAVITY_AGY_PREWARM``
(default 2) spare agy processes of the same shape for the follow-up requests of Hermes' tool loop;
they receive nothing until a request adopts one, so each still runs exactly one prompt. Final
answers return without waiting for agy's session shutdown, which finishes in the background.
"""
from __future__ import annotations

import asyncio
import atexit
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import weakref
from types import SimpleNamespace

try:
    from .agy_setup import COMMAND_ENV, INSTALL_HINT, LOGIN_HINT, _resolve as resolve_agy
    from .model_catalog import native_model
except ImportError:
    from agy_setup import COMMAND_ENV, INSTALL_HINT, LOGIN_HINT, _resolve as resolve_agy
    from model_catalog import native_model

PROVIDER = 'antigravity-agy'
AGENT_NAME = 'hermes-relay'
SERVER = 'hermes'
DISPATCH_TOOL = 'call_mcp_tool'
FALLBACK_MARKER = 'not found, falling back to default'
TRACE_ENV = 'ANTIGRAVITY_AGY_TRACE_DIR'
IMAGE_PLACEHOLDER = '[image omitted: the antigravity-agy provider forwards text only]'


class AgyError(RuntimeError):
    """agy ended the request with an error."""


class AgyMissing(AgyError):
    """The official agy CLI this transport drives is not installed (or not on PATH)."""


class AgyLoggedOut(AgyError):
    """agy has no usable sign-in where Hermes runs it."""


class AgyQuotaExhausted(AgyError):
    """The subscription quota window for this model group is used up."""


class AgyNativeToolViolation(AgyError):
    """agy tried to run one of its own tools; the run was killed before it could continue."""


class AgyToolCaptureError(AgyError):
    """A Hermes tool step had no single correlated ``tools/call`` capture.

    The ``step_update`` display text is not an executable argument, with or without an ellipsis.
    """


class AgyAgentNotLoaded(AgyError):
    """agy did not load the relay agent and fell back to its default (tool-owning) agent."""


class Object(SimpleNamespace):
    def model_dump(self, **_):
        def unpack(v):
            if isinstance(v, Object):
                return {k: unpack(x) for k, x in vars(v).items()}
            if isinstance(v, list):
                return [unpack(x) for x in v]
            return copy.deepcopy(v)
        return unpack(self)


def obj(value):
    if isinstance(value, dict):
        return Object(**{k: obj(v) for k, v in value.items()})
    if isinstance(value, list):
        return [obj(v) for v in value]
    return value


# ── Request shaping ────────────────────────────────────────────────────────────────────────────

def render_content(content):
    """OpenAI message content -> plain text (agy stream-json input is text-only)."""
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        kind = block.get('type')
        if kind in ('text', 'input_text', 'output_text'):
            parts.append(block.get('text') or '')
        elif kind in ('image_url', 'image', 'input_image'):
            parts.append(IMAGE_PLACEHOLDER)
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    return '\n'.join(p for p in parts if p)


def _arguments(value):
    if isinstance(value, str):
        try:
            return json.loads(value) if value.strip() else {}
        except ValueError:
            return value
    return value if value is not None else {}


CAPTURE_WAIT_ENV = 'ANTIGRAVITY_AGY_CAPTURE_WAIT'
DEFAULT_CAPTURE_WAIT = 2.0


def capture_wait(env):
    """Bounded wait for the inert MCP capture. A bad value keeps the default."""
    try:
        value = float(env.get(CAPTURE_WAIT_ENV, DEFAULT_CAPTURE_WAIT))
    except (TypeError, ValueError):
        return DEFAULT_CAPTURE_WAIT
    return value if value >= 0 else DEFAULT_CAPTURE_WAIT


def correlated_capture(path, tool_name):
    """Authoritative arguments for ``tool_name``, or ``None`` if no complete line exists yet.

    One complete JSON line must name ``tool_name`` and carry an object ``arguments``. Any other
    complete record is a correlation failure. This function does not read ``step_update``.
    """
    try:
        text = Path(path).read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AgyToolCaptureError('authoritative tools/call capture is unreadable') from exc
    if not text:
        return None
    if not text.endswith('\n'):
        # The only record is still being written. Wait. A complete line followed by an
        # unfinished later line is not "one call": executing the earlier line would drop
        # the in-progress record.
        if '\n' not in text:
            return None
        raise AgyToolCaptureError(
            'incomplete trailing tools/call capture; refusing to execute an earlier line')
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        rows = [json.loads(line) for line in lines]
    except ValueError as exc:
        raise AgyToolCaptureError('authoritative tools/call capture is not JSON') from exc
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise AgyToolCaptureError('authoritative tools/call capture is missing or ambiguous')
    row = rows[0]
    arguments = row.get('arguments')
    if row.get('name') != tool_name or not isinstance(arguments, dict):
        raise AgyToolCaptureError('authoritative tools/call capture does not correlate with the tool step')
    return arguments


def await_authoritative_call(path, tool_name, timeout):
    """Wait until the capture correlates, or fail closed. Never returns display text."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        found = correlated_capture(path, tool_name)
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            raise AgyToolCaptureError(
                'authoritative tools/call capture did not arrive; refusing to execute the step_update display summary')
        time.sleep(0.02)


def prepare_conversation(messages):
    """``(system_text, prompt_text)``: leading system/developer text, and the flattened history."""
    system, turns, names_by_id = [], [], {}
    for message in messages or []:
        role = message.get('role')
        text = render_content(message.get('content'))
        if role in ('system', 'developer'):
            if turns:
                turns.append(('system', text, {}))
            else:
                system.append(text)
        elif role == 'user':
            turns.append(('user', text, {}))
        elif role == 'assistant':
            calls = []
            for call in message.get('tool_calls') or []:
                function = call.get('function') or {}
                names_by_id[call.get('id')] = function.get('name')
                calls.append({'id': call.get('id'), 'name': function.get('name'), 'arguments': _arguments(function.get('arguments'))})
            turns.append(('assistant', text, {'calls': calls}))
        elif role == 'tool':
            call_id = message.get('tool_call_id')
            turns.append(('tool', text, {'id': call_id, 'name': message.get('name') or names_by_id.get(call_id) or ''}))
        else:
            raise ValueError(f'Unsupported message role: {role}')
    if not turns or turns[-1][0] not in ('user', 'tool'):
        raise ValueError('History must end in a user or tool message; assistant prefill is unsupported')
    if len(turns) == 1:
        return '\n\n'.join(s for s in system if s), turns[0][1]
    lines = ['<conversation>']
    for role, text, meta in turns:
        if role == 'assistant':
            lines.append('<message role="assistant">')
            if text:
                lines.append(text)
            for call in meta['calls']:
                lines.append(f'<tool_call id="{call["id"]}" name="{call["name"]}">'
                             + json.dumps(call['arguments'], ensure_ascii=False) + '</tool_call>')
            lines.append('</message>')
        elif role == 'tool':
            lines.append(f'<message role="tool" tool_call_id="{meta["id"]}" name="{meta["name"]}">')
            lines.append(text)
            lines.append('</message>')
        else:
            lines.append(f'<message role="{role}">')
            lines.append(text)
            lines.append('</message>')
    lines.append('</conversation>')
    lines.append('')
    lines.append('The conversation above is the complete history so far and you are the assistant in it. '
                 'Continue from its end: respond to the latest user message, or use the latest tool results. '
                 'To use a tool, make a real call_mcp_tool function call; never write <tool_call> markup as text.')
    return '\n\n'.join(s for s in system if s), '\n'.join(lines)


def normalize_schema(schema):
    """agy rejects MCP tools whose input schema is not an object schema."""
    if not isinstance(schema, dict):
        schema = {}
    schema = copy.deepcopy(schema)
    try:
        from tools.schema_sanitizer import strip_nullable_unions
        schema = strip_nullable_unions(schema, keep_nullable_hint=False)
    except Exception:
        pass
    for key in ('oneOf', 'allOf', 'anyOf'):
        schema.pop(key, None)
    schema['type'] = 'object'
    if not isinstance(schema.get('properties'), dict):
        schema['properties'] = {}
    return schema


def tool_manifest(tools, tool_choice=None):
    """``(mcp_manifest, names)`` for Hermes' function tools."""
    if tool_choice == 'none':
        return [], set()
    manifest, names = [], set()
    for tool in tools or []:
        if tool.get('type', 'function') != 'function':
            raise ValueError('Only function tools are supported')
        function = tool.get('function') or {}
        name = function.get('name')
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f'Tool names must be unique nonempty strings: {name!r}')
        names.add(name)
        manifest.append({'name': name, 'description': str(function.get('description') or ''),
                         'inputSchema': normalize_schema(function.get('parameters'))})
    return manifest, names


def system_prompt(hermes_system, manifest):
    """Hermes' system prompt, then the tool protocol and catalog. Schemas are compact JSON (no
    indentation): same content, ~11 % fewer input tokens per Hermes request (docs/perf.md)."""
    parts = [hermes_system.strip()] if hermes_system.strip() else []
    if manifest:
        catalog = []
        for tool in manifest:
            catalog.append(f'### {tool["name"]}\n{tool["description"]}\n\nParameters (JSON Schema):\n```json\n'
                           + json.dumps(tool['inputSchema'], ensure_ascii=False, separators=(',', ':')) + '\n```')
        parts.append(
            '# Tool protocol\n'
            'You run as the model inside Hermes Agent. Hermes, not you, executes tools. The only way to use a tool is '
            f'the `{DISPATCH_TOOL}` function with `ServerName` set to "{SERVER}", `ToolName` set to one of the tool names '
            'listed below, and `Arguments` set to a JSON object matching that tool\'s parameter schema.\n'
            '- Make at most one tool call per response. After calling, stop: the result arrives in the next turn.\n'
            '- Never call list_resources, read_resource, manage_task or any other function, and never write tool calls as text.\n'
            '- When no tool is needed, answer directly.\n\n'
            '# Available tools\n\n' + '\n\n'.join(catalog))
    else:
        parts.append('# Tool protocol\nNo tools are available in this conversation. Do not call any function; answer directly.')
    return '\n\n'.join(parts) + '\n'


def agent_markdown(body, mcp_command):
    lines = ['---', f'name: {AGENT_NAME}',
             'description: "Hermes Agent relay: model only, Hermes executes every tool"',
             'excludeDefaultComponents: true', 'inheritMcp: false', 'subagent: false', 'mainAgent: true']
    if mcp_command:
        # JSON strings are valid YAML flow scalars, so paths need no further escaping.
        lines += ['mcpServers:', f'  - name: {SERVER}', f'    command: {json.dumps(mcp_command[0])}',
                  '    args: ' + json.dumps(mcp_command[1:])]
    lines.append('---')
    return '\n'.join(lines) + '\n' + body


# ── Process plumbing ───────────────────────────────────────────────────────────────────────────

def kill_process_tree(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class Request:
    def __init__(self, client):
        self.client, self.process, self.stream = client, None, None
        self.cancelled = threading.Event()
        self.lock = threading.Lock()

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            if self.process is not None:
                kill_process_tree(self.process)

    def attach(self, proc):
        with self.lock:
            if self.cancelled.is_set():
                raise AgyError('agy request cancelled')
            self.process = proc.p

    def detach(self, proc):
        with self.lock:
            if self.process is proc.p:
                self.process = None


class Stream:
    def __init__(self, iterator, request):
        self.iterator, self.request = iterator, request
        self._advancing = threading.Lock()
        request.stream = weakref.ref(self)

    def __iter__(self):
        return self

    def __next__(self):
        with self._advancing:
            return next(self.iterator)

    def close(self):
        self.request.cancel()
        if self._advancing.acquire(blocking=False):
            try:
                self.iterator.close()
                with self.request.client._lock:
                    self.request.client._requests.discard(self.request)
            finally:
                self._advancing.release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class AsyncStream:
    def __init__(self, stream):
        self.stream = stream

    def __aiter__(self):
        return self

    async def __anext__(self):
        def advance():
            try:
                return True, next(self.stream)
            except StopIteration:
                return False, None
        try:
            present, item = await asyncio.to_thread(advance)
        except asyncio.CancelledError:
            self.stream.close()
            raise
        if not present:
            raise StopAsyncIteration
        return item

    async def aclose(self):
        self.stream.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()


def classify_error(text):
    """Map agy's error text (result ``error`` / ``AGY_ERROR`` line / stderr) onto a typed error."""
    lowered = (text or '').lower()
    if any(k in lowered for k in ('resource_exhausted', 'quota', 'rate limit', '429', 'limit reached', 'out of credits')):
        return AgyQuotaExhausted(f'Antigravity subscription quota exhausted or rate limited: {text.strip()[:500]} '
                                 '(check `agy -p=/quota`; limits refresh on a 5-hour and a weekly window)')
    if any(k in lowered for k in ('not logged', 'not signed', 'sign in', 'log in', 'login', 'unauthenticated', 'authentication')):
        return AgyLoggedOut(f'{LOGIN_HINT} (agy: {text.strip()[:500]})')
    return AgyError(f'agy request failed: {text.strip()[:800]}')


def _agy_error_line(stderr_text):
    for line in reversed(stderr_text.splitlines()):
        if line.startswith('AGY_ERROR:'):
            raw = line[len('AGY_ERROR:'):].strip()
            try:
                data = json.loads(raw)
            except ValueError:
                return raw
            return ' '.join(str(data.get(k)) for k in ('short_error', 'status', 'error_code') if data.get(k) not in (None, ''))
    return ''


class Trace:
    """Opt-in audit/bench trail (``ANTIGRAVITY_AGY_TRACE_DIR``): agy's raw NDJSON lines, each
    prefixed with ``_ts`` (seconds since the Hermes request began), framed by ``_request`` and
    ``_done`` bookkeeping lines. No credentials ever pass through it."""

    def __init__(self, trace_dir, started, header):
        trace_dir = Path(trace_dir).expanduser()
        trace_dir.mkdir(parents=True, exist_ok=True)
        self.started, self.lock = started, threading.Lock()
        self.handle = open(trace_dir / f'{time.strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:8]}.ndjson', 'w', encoding='utf-8')
        self.meta('_request', wall=time.time(), **header)

    def ts(self, at=None):
        return round((time.monotonic() if at is None else at) - self.started, 3)

    def raw(self, line, at=None):
        if line.startswith('{') and line[1:].lstrip()[:1] == '"':
            line = '{"_ts": %s, ' % self.ts(at) + line[1:]
        self._write(line)

    def meta(self, event, **fields):
        self._write(json.dumps({'event': event, '_ts': self.ts(), **fields}, ensure_ascii=False))

    def _write(self, line):
        with self.lock:
            if not self.handle.closed:
                self.handle.write(line + '\n')
                self.handle.flush()

    def close(self):
        with self.lock:
            self.handle.close()


# ── agy processes, pre-warming and reaping ─────────────────────────────────────────────────────

PREWARM_ENV = 'ANTIGRAVITY_AGY_PREWARM'          # agy processes kept booted per request shape; "0" turns it off
PREWARM_TTL_ENV = 'ANTIGRAVITY_AGY_PREWARM_TTL'  # seconds an unused pre-warmed agy may idle (default 300)
CLEANUP_ENV = 'ANTIGRAVITY_AGY_CLEANUP'          # which of this plugin's agy conversation records go to the trash
DATA_DIR_ENV = 'ANTIGRAVITY_AGY_DATA_DIR'        # agy's app data dir (default ~/.gemini/antigravity-cli)
DEFAULT_PREWARM_TTL = 300.0
SHUTDOWN_GRACE = 15.0
DEFAULT_PREWARM_DEPTH = 2
MAX_PREWARM_DEPTH = 4
POOL_MAX = 4  # pre-warmed processes across all request shapes (each idles at ~400 MB RSS)
CONVERSATION_ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def key_digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest()[:16]


def pool_key(resolved, native, system, manifest, env):
    """Everything that shapes an agy process before it sees a prompt: argv, model, agent, tools, env."""
    return key_digest(resolved, native, system, manifest, sorted(env.items()))


def prewarm_depth(env):
    """How many booted agy processes to keep ready for the next requests of the same shape."""
    raw = str(env.get(PREWARM_ENV, DEFAULT_PREWARM_DEPTH)).strip().lower()
    if raw in ('', '0', 'false', 'no', 'off'):
        return 0
    if raw in ('true', 'yes', 'on'):
        return DEFAULT_PREWARM_DEPTH
    try:
        return max(0, min(int(raw), MAX_PREWARM_DEPTH))
    except ValueError:
        return DEFAULT_PREWARM_DEPTH


def cleanup_mode(env):
    """``unused`` (default): only records of pre-warmed processes that never got a prompt (empty, pure
    plugin artifacts); ``all``/``trash``: every record this plugin's agy runs create; ``off``: none."""
    raw = str(env.get(CLEANUP_ENV, 'unused')).strip().lower()
    if raw in ('all', 'trash', '1', 'true', 'yes', 'on'):
        return 'all'
    if raw in ('off', '0', 'false', 'no', 'none'):
        return 'off'
    return 'unused'


def prewarm_ttl(env):
    try:
        value = float(env.get(PREWARM_TTL_ENV, DEFAULT_PREWARM_TTL))
    except (TypeError, ValueError):
        return DEFAULT_PREWARM_TTL
    return value if value > 0 else DEFAULT_PREWARM_TTL


class AgyProcess:
    """One agy print-mode run in its own private workspace. It may be spawned before the request that
    uses it exists: until ``send()`` writes the (single) user message, agy only boots — signs in,
    loads the relay agent, starts the inert MCP server and emits ``init`` — and makes no model call."""

    def __init__(self, resolved, native, system, manifest, env):
        self.env = env
        self.root = Path(tempfile.mkdtemp(prefix='agy-hermes-'))
        self.events = queue.Queue()
        self.trace, self._pending, self._trace_lock = None, [], threading.Lock()
        self.conversation_id, self.p, self.stderr, self.used = None, None, None, False
        self._disposed, self._dispose_lock, self.urgent = False, threading.Lock(), threading.Event()
        try:
            workspace = self.root / 'workspace'
            agent_dir = workspace / '.agents' / 'agents' / AGENT_NAME
            agent_dir.mkdir(parents=True)
            mcp_command = None
            if manifest:
                (self.root / 'tools.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
                mcp_command = [sys.executable, str(Path(__file__).with_name('inert_mcp.py')), str(self.root / 'tools.json'),
                               str(self.root / 'captured_calls.jsonl')]
            (agent_dir / 'agent.md').write_text(agent_markdown(system, mcp_command), encoding='utf-8')
            self.log_file = self.root / 'agy.log'
            command = resolved + ['--input-format', 'stream-json', '--output-format', 'stream-json', '--model', native,
                                  '--agent', AGENT_NAME, '--log-file', str(self.log_file), '--disable-slash-commands']
            self.stderr = open(self.root / 'stderr.txt', 'w+', encoding='utf-8')
            self.spawned_at = time.monotonic()
            # Own process group: agy, its language server and the MCP child die together.
            self.p = subprocess.Popen(command, start_new_session=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=self.stderr, text=True, encoding='utf-8', errors='replace',
                                      cwd=str(workspace), env=env)
        except BaseException:
            self.dispose(0)
            raise
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.p.stdout:
                line = line.strip()
                if not line:
                    continue
                with self._trace_lock:
                    if self.trace is not None:
                        self.trace.raw(line)
                    else:
                        self._pending.append((time.monotonic(), line))
                try:
                    event = json.loads(line)
                except ValueError:
                    event = {'event': '_noise', 'line': line[:300]}
                if event.get('event') == 'init' and self.conversation_id is None:
                    self.conversation_id = event.get('conversation_id')
                self.events.put(event)
        except Exception as error:  # pragma: no cover - pipe torn down under us
            self.events.put(error)
        finally:
            self.p.wait()
            self.events.put(None)

    def attach_trace(self, trace):
        """Lines agy printed before the request adopted this process keep their arrival time (negative ``_ts``)."""
        with self._trace_lock:
            for at, line in self._pending:
                trace.raw(line, at)
            self._pending, self.trace = [], trace

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def send(self, prompt):
        self.used = True
        try:
            self.p.stdin.write(json.dumps({'event': 'user', 'message': {'role': 'user', 'content': prompt}}, ensure_ascii=False) + '\n')
            self.p.stdin.close()  # exactly one turn; agy exits after it
        except (BrokenPipeError, ValueError):
            # agy already exited (e.g. it rejected --model before reading stdin). Its own result/stderr says
            # why; the event loop reports that instead of a bare, retryable BrokenPipeError.
            pass

    def fell_back(self):
        try:
            return FALLBACK_MARKER in self.log_file.read_text(encoding='utf-8', errors='replace')
        except (OSError, AttributeError):
            return False

    def kill(self):
        if self.p is not None:
            kill_process_tree(self.p)

    def wait(self, timeout):
        try:
            self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

    def stderr_text(self):
        try:
            self.stderr.flush()
            self.stderr.seek(0)
            return self.stderr.read()
        except (OSError, ValueError):
            return ''

    def dispose(self, grace=0):
        """Let agy exit by itself for up to ``grace`` s, then kill it; release pipes, trace and workspace;
        optionally trash this run's own agy conversation record. Idempotent."""
        with self._dispose_lock:
            if self._disposed:
                return
            self._disposed = True
        if self.p is not None:
            deadline = time.monotonic() + grace
            while grace and self.p.poll() is None and not self.urgent.is_set() and time.monotonic() < deadline:
                self.wait(0.1)
            kill_process_tree(self.p)
            self.wait(5)
            reader = getattr(self, 'reader', None)
            if reader is not None:
                reader.join(timeout=5)
            for pipe in (self.p.stdin, self.p.stdout):
                if pipe and not pipe.closed:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        with self._trace_lock:
            if self.trace is not None:
                self.trace.close()
        if self.stderr is not None:
            try:
                self.stderr.close()
            except OSError:
                pass
        shutil.rmtree(self.root, ignore_errors=True)
        mode = cleanup_mode(self.env)
        if self.conversation_id and (mode == 'all' or (mode == 'unused' and not self.used)):
            trash_conversation(self.conversation_id, self.env)


def conversation_paths(conversation_id, data_dir):
    """The per-conversation files agy creates for one run (never shared stores such as summary databases)."""
    if not CONVERSATION_ID.match(conversation_id or ''):
        return []
    data_dir = Path(data_dir)
    candidates = [data_dir / 'conversations' / f'{conversation_id}{suffix}' for suffix in ('.db', '.db-shm', '.db-wal', '.pb')]
    candidates += [data_dir / 'brain' / conversation_id, data_dir / 'presence' / f'{conversation_id}.lock',
                   data_dir / 'annotations' / f'{conversation_id}.pbtxt']
    return [path for path in candidates if os.path.lexists(path)]


def trash_conversation(conversation_id, env):
    """Move one plugin-created agy conversation record to the desktop trash with ``trash-put``; never deletes."""
    data_dir = Path(env.get(DATA_DIR_ENV) or Path.home() / '.gemini' / 'antigravity-cli').expanduser()
    paths = conversation_paths(conversation_id, data_dir)
    trash = shutil.which('trash-put', path=env.get('PATH') or os.defpath)
    if not paths or trash is None:
        return False
    try:
        return subprocess.run([trash, '--', *map(str, paths)], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_REAPING, _REAPING_LOCK = {}, threading.Lock()


def reap(proc, grace=0):
    """Dispose of ``proc`` off the request path (``_shutdown`` finishes pending ones at exit)."""
    def run():
        try:
            proc.dispose(grace)
        finally:
            with _REAPING_LOCK:
                _REAPING.pop(proc, None)
    thread = threading.Thread(target=run, daemon=True, name='agy-reaper')
    with _REAPING_LOCK:
        _REAPING[proc] = thread
    thread.start()


class WarmPool:
    """Process-wide pool of pre-booted agy processes: per request shape (``pool_key``) a FIFO of up to
    ``depth`` processes, ``POOL_MAX`` in total.

    A pre-warmed process has received no prompt, so it holds no conversation state and cannot have
    seen a tool result: the invariant "only Hermes executes tools" is untouched. Unused processes are
    killed after their TTL and at interpreter exit."""

    def __init__(self):
        self.lock, self.entries = threading.Lock(), {}  # key -> [(proc, expires_at), ...] oldest first

    def __len__(self):
        with self.lock:
            return sum(len(v) for v in self.entries.values())

    def procs(self, key=None):
        with self.lock:
            return [proc for k, row in self.entries.items() if key in (None, k) for proc, _ in row]

    def take(self, key):
        """The longest-booted live process for ``key`` (or None); dead or expired ones are discarded."""
        stale, found = [], None
        with self.lock:
            row = self.entries.get(key) or []
            while row and found is None:
                proc, expires = row.pop(0)
                if proc.alive() and time.monotonic() < expires:
                    found = proc
                else:
                    stale.append(proc)
            if not row:
                self.entries.pop(key, None)
        for proc in stale:
            proc.kill()
            reap(proc)
        return found

    def prewarm(self, key, spec, ttl, depth):
        """Top ``key`` up to ``depth`` booting processes (best effort: failures just mean a cold start)."""
        with self.lock:
            missing = depth - len(self.entries.get(key) or [])
        for _ in range(max(0, missing)):
            try:
                proc = AgyProcess(*spec)
            except Exception:
                return
            evicted = []
            with self.lock:
                row = self.entries.setdefault(key, [])
                if len(row) >= depth:
                    evicted.append(proc)
                else:
                    row.append((proc, time.monotonic() + ttl))
                while sum(len(v) for v in self.entries.values()) > POOL_MAX:
                    # Evict the process that would expire first, preferring other request shapes.
                    others = [k for k in self.entries if k != key and self.entries[k]] or [key]
                    victim_key = min(others, key=lambda k: self.entries[k][0][1])
                    evicted.append(self.entries[victim_key].pop(0)[0])
                    if not self.entries[victim_key]:
                        del self.entries[victim_key]
            for old in evicted:
                old.kill()
                reap(old)
            if proc not in evicted:
                timer = threading.Timer(ttl, self.expire, (key, proc))
                timer.daemon = True
                timer.start()

    def expire(self, key, proc):
        with self.lock:
            row = self.entries.get(key) or []
            kept = [entry for entry in row if entry[0] is not proc]
            if len(kept) == len(row):
                return
            if kept:
                self.entries[key] = kept
            else:
                self.entries.pop(key, None)
        proc.kill()
        reap(proc)

    def drain(self):
        with self.lock:
            procs = [proc for row in self.entries.values() for proc, _ in row]
            self.entries.clear()
        for proc in procs:
            proc.kill()
            proc.dispose(0)


POOL = WarmPool()


@atexit.register
def _shutdown():
    """Interpreter exit (e.g. the end of ``hermes chat -q``): kill unused pre-warmed processes and cut
    short background shutdowns, so no agy outlives Hermes and every workspace is removed."""
    POOL.drain()
    with _REAPING_LOCK:
        pending = list(_REAPING.items())
    for proc, _ in pending:
        proc.urgent.set()
    for _, thread in pending:
        thread.join(timeout=10)


# ── Client ─────────────────────────────────────────────────────────────────────────────────────

class Client:
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(self, command=None, args=None, env=None, timeout=600, **_):
        # Hermes snapshots routing metadata from client-shaped objects; this is not a credential.
        self.api_key = 'external-process'
        self.base_url = f'process://{PROVIDER}'
        self.env = dict(env) if env is not None else None
        source_env = self.env if self.env is not None else os.environ
        command = command or source_env.get(COMMAND_ENV) or 'agy'
        self.command = ([command] if isinstance(command, str) else list(command)) + list(args or [])
        self.timeout = timeout if isinstance(timeout, (int, float)) and timeout > 0 else 600
        self._lock, self._requests, self._closed = threading.Lock(), set(), False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def cancel(self):
        with self._lock:
            requests = tuple(self._requests)
        for request in requests:
            request.cancel()

    def close(self):
        with self._lock:
            self._closed = True
            requests = tuple(self._requests)
        for request in requests:
            stream = request.stream() if request.stream else None
            if stream is not None:
                stream.close()
            else:
                request.cancel()

    def create(self, **kwargs):
        # Hermes' auxiliary seam awaits create() on this same object.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._create(**kwargs)
        return self._acreate(**kwargs)

    async def _acreate(self, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(self._create, **kwargs))
        try:
            result = await task
            return AsyncStream(result) if kwargs.get('stream') else result
        except asyncio.CancelledError:
            self.cancel()
            raise

    def _create(self, **kwargs):
        model = kwargs.get('model')
        if not isinstance(model, str) or not model.strip():
            raise ValueError('model is required')
        if kwargs.get('n', 1) != 1:
            raise ValueError('Only n=1 is supported')
        manifest, names = tool_manifest(kwargs.get('tools'), kwargs.get('tool_choice'))
        hermes_system, prompt = prepare_conversation(kwargs.get('messages'))
        timeout = kwargs.get('timeout') or self.timeout
        timeout = getattr(timeout, 'read', timeout)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            timeout = self.timeout
        request = Request(self)
        with self._lock:
            if self._closed:
                raise AgyError('agy client is closed')
            self._requests.add(request)
        stream = Stream(self._run(request, model, system_prompt(hermes_system, manifest), prompt, manifest, names, timeout), request)
        if kwargs.get('stream'):
            return stream
        try:
            for chunk in stream:
                if hasattr(chunk, '_response'):
                    return chunk._response
            raise AgyError('agy response missing')
        finally:
            stream.close()

    def _run(self, request, model, system, prompt, manifest, names, timeout):
        proc = trace = None
        handed_off = False
        started = time.monotonic()
        try:
            env = dict(self.env if self.env is not None else os.environ)
            resolved = resolve_agy(self.command, env)
            if resolved is None:
                raise AgyMissing(INSTALL_HINT)
            spec = (resolved, native_model(model), system, manifest, env)
            key = pool_key(*spec)
            depth = prewarm_depth(env)
            if env.get(TRACE_ENV):
                trace = Trace(env[TRACE_ENV], started, {'model': model, 'tools': len(manifest), 'system_chars': len(system),
                                                        'prompt_chars': len(prompt), 'system_sha': key_digest(system)})
            proc = POOL.take(key) if depth else None
            if trace is not None:
                trace.meta('_spawn', warm=proc is not None,
                           **({'warm_age': round(started - proc.spawned_at, 3)} if proc is not None else {}))
            if proc is None:
                proc = AgyProcess(*spec)
            if trace is not None:
                proc.attach_trace(trace)
            request.attach(proc)
            proc.send(prompt)
            if depth and manifest:
                # Hermes' next requests in this tool loop almost always carry the same system prompt and
                # tools: boot their agy now, while this one runs, so they skip agy's multi-second startup.
                POOL.prewarm(key, spec, prewarm_ttl(env), depth)
            deadline = time.monotonic() + timeout

            def receive():
                nonlocal deadline
                while True:
                    if request.cancelled.is_set():
                        raise AgyError('agy request cancelled')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f'agy request timed out after {timeout:g}s without output')
                    try:
                        event = proc.events.get(timeout=min(remaining, .2))
                    except queue.Empty:
                        continue
                    if isinstance(event, Exception):
                        raise AgyError(f'agy stream failed: {event}') from event
                    deadline = time.monotonic() + timeout
                    return event

            text, calls, usage_steps, final = '', [], [], None
            while True:
                event = receive()
                if event is None:
                    break
                kind = event.get('event')
                if kind == 'init':
                    init = event.get('init') or {}
                    if init.get('agent') not in (AGENT_NAME, None) or proc.fell_back():
                        proc.kill()
                        raise AgyAgentNotLoaded('agy did not load the Hermes relay agent and fell back to its default '
                                                'agent (which owns built-in tools); refusing to continue')
                elif kind == 'step_update':
                    step = event.get('step_update') or {}
                    step_type = step.get('step_type')
                    if step_type == 'agent_response':
                        delta = step.get('text_delta')
                        if delta and not calls:
                            text += delta
                            yield self._chunk(model, {'content': delta})
                        if step.get('state') == 'DONE' and isinstance(step.get('usage'), dict):
                            usage_steps.append(step['usage'])
                    elif step_type == 'tool':
                        info = step.get('tool_info') or {}
                        name = step.get('tool_name') or info.get('name')
                        params = info.get('parameters') or {}
                        target = params.get('ToolName')
                        if name == DISPATCH_TOOL and params.get('ServerName') == SERVER and target in names:
                            # agy dispatches calls one at a time. Hermes executes only the arguments
                            # recorded by inert_mcp's tools/call. step_update parameters are a display
                            # summary and are intentionally unread here, ellipsis or not.
                            try:
                                arguments = await_authoritative_call(
                                    proc.root / 'captured_calls.jsonl', target, capture_wait(env))
                            except AgyToolCaptureError:
                                proc.kill()
                                raise
                            calls.append({'id': 'call_' + uuid.uuid4().hex[:24], 'type': 'function',
                                          'function': {'name': target, 'arguments': json.dumps(
                                              arguments, ensure_ascii=False, separators=(',', ':'))}})
                            proc.kill()
                            break
                        else:
                            proc.kill()
                            shown = f'{name} -> {params.get("ServerName")}/{target}' if name == DISPATCH_TOOL else name
                            raise AgyNativeToolViolation(
                                f'agy attempted a tool outside the Hermes inventory ({shown!r}); the run was killed '
                                'so only Hermes executes tools')
                elif kind == 'result':
                    final = event.get('result') or {}
                    break
            if request.cancelled.is_set():
                raise AgyError('agy request cancelled')
            if proc.fell_back():
                raise AgyAgentNotLoaded('agy did not load the Hermes relay agent and fell back to its default agent')
            if not calls:
                if final is None:
                    stderr_text = proc.stderr_text()
                    detail = _agy_error_line(stderr_text) or stderr_text.strip()[-500:] or f'exit code {proc.p.returncode}'
                    raise classify_error(detail)
                if final.get('status') != 'SUCCESS':
                    proc.wait(15)  # agy prints its AGY_ERROR line around the result; let stderr complete
                    raise classify_error(' '.join(x for x in (str(final.get('error') or ''), _agy_error_line(proc.stderr_text())) if x)
                                         or f'status {final.get("status")}')
                response_text = final.get('response') or ''
                if response_text != text:
                    if response_text.startswith(text):
                        yield self._chunk(model, {'content': response_text[len(text):]})
                    text = response_text
                usage = final.get('usage') if isinstance(final.get('usage'), dict) else None
            else:
                usage = None
            if usage is None:
                usage = {}
                for step in usage_steps:
                    for key_, value in step.items():
                        if isinstance(value, (int, float)):
                            usage[key_] = usage.get(key_, 0) + value
            normalized = self._usage(usage)
            tool_calls = calls
            message = {'role': 'assistant', 'content': text or None, 'tool_calls': tool_calls or None}
            finish = 'tool_calls' if tool_calls else 'stop'
            response = obj({'id': 'agy-' + uuid.uuid4().hex[:16], 'model': model, 'object': 'chat.completion',
                            'choices': [{'index': 0, 'finish_reason': finish, 'message': message}], 'usage': normalized})
            chunk = self._chunk(model, {'content': None, 'tool_calls': [dict(tc, index=i) for i, tc in enumerate(tool_calls)] or None},
                                finish, normalized)
            chunk._response = response
            if trace is not None:
                trace.meta('_done', finish=finish)
            if not calls:
                # The answer is complete. agy's session shutdown (~1 s) happens in the background instead
                # of in front of Hermes; nothing can reach the process any more (its stdin is closed).
                request.detach(proc)
                reap(proc, grace=SHUTDOWN_GRACE)
                handed_off = True
            yield chunk
        finally:
            request.cancel()
            if proc is not None and not handed_off:
                proc.kill()
                reap(proc, grace=0)
            elif proc is None and trace is not None:
                trace.close()
            with self._lock:
                self._requests.discard(request)

    @staticmethod
    def _usage(usage):
        inp = int(usage.get('input_tokens', 0)) + int(usage.get('cache_read_tokens', 0))
        out = int(usage.get('output_tokens', 0))
        return {'prompt_tokens': inp, 'completion_tokens': out, 'total_tokens': inp + out,
                'prompt_tokens_details': {'cached_tokens': int(usage.get('cache_read_tokens', 0))},
                'completion_tokens_details': {'reasoning_tokens': int(usage.get('thinking_tokens', 0))},
                'native_usage': dict(usage)}

    @staticmethod
    def _chunk(model, delta, finish=None, usage=None):
        return obj({'id': 'agy-stream', 'model': model, 'object': 'chat.completion.chunk',
                    'choices': [{'index': 0, 'delta': {'content': None, 'tool_calls': None, **delta}, 'finish_reason': finish}],
                    'usage': usage})
