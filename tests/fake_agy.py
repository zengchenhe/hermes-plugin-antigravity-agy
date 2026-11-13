#!/usr/bin/env python3
"""Fake ``agy`` for offline tests. Behaviour is chosen by ``FAKE_AGY_SCENARIO``.

Every invocation records its argv, cwd, stdin and the relay agent definition into
``FAKE_AGY_RECORD`` (a directory) so tests can assert on exactly what the transport sent.
"""
import json
import os
from pathlib import Path
import sys
import time
import uuid

MODELS = [('gemini-3.8-flash-low', 'Gemini 3.8 Flash (Low)'), ('gemini-3.1-pro-high', 'Gemini 3.1 Pro (High)'),
          ('claude-sonnet-4-6', 'Claude Sonnet 4.6 (Thinking)')]
CONV = 'conv-fake-0001'


def emit(event):
    print(json.dumps(event), flush=True)


def step(index, step_type, state='DONE', **fields):
    emit({'event': 'step_update', 'step_update': {'conversation_id': CONV, 'step_index': index, 'state': state,
                                                  'step_type': step_type, **fields}})


def usage(inp, out, cached=0, thinking=0):
    return {'input_tokens': inp, 'output_tokens': out, 'thinking_tokens': thinking, 'cache_read_tokens': cached,
            'total_tokens': inp + out}


def result(status='SUCCESS', response='', error=None, use=None):
    body = {'conversation_id': CONV, 'status': status, 'response': response, 'duration_seconds': 0.1,
            'num_turns': 1, 'usage': use or usage(0, 0)}
    if error:
        body['error'] = error
    emit({'event': 'result', 'result': body})


def tool_step(index, name, parameters):
    step(index, 'tool', 'ACTIVE', tool_name=name, tool_info={'name': name, 'parameters': parameters})


def write_capture(name, arguments):
    """Same line inert_mcp.py writes: one tools/call, never executed here."""
    path = Path.cwd().parent / 'captured_calls.jsonl'
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({'name': name, 'arguments': arguments}, ensure_ascii=False) + '\n')
        handle.flush()


def hang(record, marker):
    # A real agy blocks here on the never-answered MCP call; the transport must kill us.
    time.sleep(30)
    if record:
        (record / marker).write_text('still alive after 30s', encoding='utf-8')


def main():
    argv = sys.argv[1:]
    scenario = os.environ.get('FAKE_AGY_SCENARIO', 'text')
    record = Path(os.environ['FAKE_AGY_RECORD']) if os.environ.get('FAKE_AGY_RECORD') else None
    if argv == ['models']:
        print('Fetching available models...', file=sys.stderr)
        for slug, label in MODELS:
            print(f'{slug}\t{label}')
        return 0
    if argv[:1] == ['-p=/quota']:
        if scenario == 'logged_out':
            print(json.dumps({'status': 'ERROR', 'error': 'You are not logged into Antigravity.'}))
            return 1
        print(json.dumps({'conversation_id': '', 'status': 'SUCCESS', 'response':
                          'Gemini Models\tWeekly Limit Remaining\t89%\t2026-09-30T03:11:23Z\n'
                          'Gemini Models\tFive Hour Limit Remaining\t99%\t2026-09-25T16:51:09Z\n'}))
        return 0

    flags = {}
    for i, arg in enumerate(argv):
        if arg.startswith('--') and i + 1 < len(argv) and not argv[i + 1].startswith('--'):
            flags[arg] = argv[i + 1]
    if scenario == 'bad_model':
        # Real agy validates --model before reading stdin and exits: the prompt write then hits a closed pipe.
        if record:
            record.mkdir(parents=True, exist_ok=True)
            (record / 'argv.json').write_text(json.dumps(argv), encoding='utf-8')
        print(f'error: invalid model selection (--model "{flags.get("--model")}" --effort ""): '
              f'--model {flags.get("--model")} requires --effort (available: low, medium, high)', file=sys.stderr, flush=True)
        os.close(0)
        return 1
    agent_md = Path.cwd() / '.agents' / 'agents' / 'hermes-relay' / 'agent.md'
    for flag, value in (('--input-format', 'stream-json'), ('--output-format', 'stream-json'), ('--agent', 'hermes-relay')):
        if flags.get(flag) != value:
            print(f'fake agy: expected {flag} {value}', file=sys.stderr)
            return 2
    if not agent_md.exists():
        print('fake agy: relay agent definition missing', file=sys.stderr)
        return 2
    if scenario == 'crash':
        print('boom: fake agy crashed', file=sys.stderr)
        return 2
    if scenario == 'hang':
        if record:
            record.mkdir(parents=True, exist_ok=True)
            (record / 'pid.txt').write_text(str(os.getpid()), encoding='utf-8')
        time.sleep(30)
        return 0
    if scenario == 'fallback':
        Path(flags['--log-file']).write_text(
            'W0925 19:56:54.968630       1 session.go:94] Agent "hermes-relay" not found, falling back to default\n', encoding='utf-8')
    if scenario == 'noise':
        print('some banner that is not json', flush=True)

    global CONV
    data_dir = os.environ.get('FAKE_AGY_DATA_DIR')
    if data_dir:
        # Like real agy: every run creates its own conversation record at startup.
        CONV = str(uuid.uuid4())
        data = Path(data_dir)
        for sub in ('conversations', 'brain', 'presence'):
            (data / sub).mkdir(parents=True, exist_ok=True)
        (data / 'conversations' / f'{CONV}.db').write_text('db', encoding='utf-8')
        (data / 'brain' / CONV).mkdir()
        (data / 'brain' / CONV / 'notes.md').write_text('brain', encoding='utf-8')
        (data / 'presence' / f'{CONV}.lock').write_text('', encoding='utf-8')
    # Real agy boots and emits init before it reads the prompt from stdin (pre-warming relies on it).
    emit({'event': 'init', 'conversation_id': CONV, 'init': {
        'model': flags.get('--model'), 'cwd': str(Path.cwd()), 'agent': 'hermes-relay',
        'tools': ['call_mcp_tool', 'run_command', 'view_file'], 'permission_mode': 'always-proceed'}})
    stdin = sys.stdin.read()
    if record:
        record.mkdir(parents=True, exist_ok=True)
        (record / 'argv.json').write_text(json.dumps(argv), encoding='utf-8')
        (record / 'cwd.txt').write_text(str(Path.cwd()), encoding='utf-8')
        (record / 'stdin.jsonl').write_text(stdin, encoding='utf-8')
        (record / 'pid.txt').write_text(str(os.getpid()), encoding='utf-8')
        (record / 'agent.md').write_text(agent_md.read_text(encoding='utf-8'), encoding='utf-8')
        if os.environ.get('FAKE_AGY_RECORD_ALL'):
            # Keep every invocation's agent definition (a Hermes run makes several concurrent requests).
            (record / f'agent-{os.getpid()}.md').write_text(agent_md.read_text(encoding='utf-8'), encoding='utf-8')
            (record / f'stdin-{os.getpid()}.jsonl').write_text(stdin, encoding='utf-8')
    if not stdin.strip():
        return 0  # closed without a prompt (an unused pre-warmed process)
    message = json.loads(stdin.splitlines()[0])
    assert message['event'] == 'user' and message['message']['role'] == 'user'

    step(0, 'user_input')

    if scenario == 'slow_exit':
        step(1, 'agent_response', 'DONE', text_delta='Bye', usage=usage(10, 1))
        result(response='Bye', use=usage(10, 1))
        time.sleep(3)  # agy's session shutdown after the answer
        if record:
            (record / 'slow_exit_done').write_text('exited normally', encoding='utf-8')
        return 0
    if scenario in ('text', 'noise', 'fallback'):
        step(1, 'agent_response', 'ACTIVE', text_delta='Hello')
        step(1, 'agent_response', 'ACTIVE', text_delta=' world')
        step(1, 'agent_response', 'DONE', text_delta='\n', usage=usage(1000, 5, cached=200, thinking=3))
        result(response='Hello world\n', use=usage(1000, 5, cached=200, thinking=3))
        return 0
    if scenario == 'tool':
        step(1, 'agent_response', 'DONE', usage=usage(1500, 40, thinking=10))
        write_capture('terminal', {'command': 'ls -la'})
        tool_step(2, 'call_mcp_tool', {'Arguments': {'command': 'ls -la'}, 'ServerName': 'hermes', 'ToolName': 'terminal'})
        hang(record, 'after_tool')
        return 0
    if scenario == 'tool_text':
        step(1, 'agent_response', 'ACTIVE', text_delta='Let me look.')
        step(1, 'agent_response', 'DONE', usage=usage(1500, 40))
        write_capture('read_file', {'path': 'notes.txt'})
        tool_step(2, 'call_mcp_tool', {'Arguments': '{"path": "notes.txt"}', 'ServerName': 'hermes', 'ToolName': 'read_file'})
        hang(record, 'after_tool')
        return 0
    if scenario == 'authority':
        spec = json.loads(os.environ.get('FAKE_AGY_AUTHORITY') or '{}')

        def emit_capture():
            write_capture(spec.get('capture_name', spec['tool']), spec.get('arguments'))
            extra = spec.get('extra')
            if isinstance(extra, dict):
                write_capture(extra.get('name'), extra.get('arguments'))

        step(1, 'agent_response', 'DONE', usage=usage(1500, 40))
        if spec.get('capture_first') and not spec.get('skip_capture'):
            emit_capture()
        tool_step(2, 'call_mcp_tool', {
            'Arguments': spec.get('summary'),
            'ServerName': spec.get('server', 'hermes'),
            'ToolName': spec['tool'],
        })
        if not spec.get('capture_first') and not spec.get('skip_capture'):
            delay = float(spec.get('delay') or 0)
            if delay:
                time.sleep(delay)
            emit_capture()
        hang(record, 'after_tool')
        return 0
    if scenario == 'native_tool':
        step(1, 'agent_response', 'DONE', usage=usage(24000, 30))
        tool_step(2, 'run_command', {'CommandLine': 'rm -rf /tmp/should-never-run'})
        hang(record, 'after_native')
        return 0
    if scenario == 'foreign_mcp':
        step(1, 'agent_response', 'DONE', usage=usage(1500, 30))
        tool_step(2, 'call_mcp_tool', {'Arguments': {}, 'ServerName': 'codegraph', 'ToolName': 'explore'})
        hang(record, 'after_foreign')
        return 0
    if scenario == 'unknown_hermes_tool':
        step(1, 'agent_response', 'DONE', usage=usage(1500, 30))
        tool_step(2, 'call_mcp_tool', {'Arguments': {}, 'ServerName': 'hermes', 'ToolName': 'not_offered'})
        hang(record, 'after_unknown')
        return 0
    if scenario == 'quota':
        step(1, 'error_message')
        print('AGY_ERROR: ' + json.dumps({'short_error': 'RESOURCE_EXHAUSTED: You have exhausted your capacity on this model',
                                          'status': 'RESOURCE_EXHAUSTED', 'error_code': 429, 'code_kind': 'http',
                                          'retryable': False}), file=sys.stderr, flush=True)
        result(status='ERROR', error='You have exhausted your capacity on this model')
        return 3
    if scenario == 'logged_out':
        result(status='ERROR', error='You are not logged into Antigravity. Run `agy` to sign in.')
        return 1
    print(f'fake agy: unknown scenario {scenario}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
