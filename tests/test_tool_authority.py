"""Offline checks that Hermes receives the tools/call capture, not the display summary."""
import hashlib
import json

import pytest

from transport import AgyToolCaptureError, Client

TOOLS = [
    {'type': 'function', 'function': {'name': 'terminal', 'description': 'Run a shell command.', 'parameters': {
        'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command']}}},
    {'type': 'function', 'function': {'name': 'write_file', 'description': 'Write a file.', 'parameters': {
        'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
        'required': ['path', 'content']}}},
]
USER = [{'role': 'system', 'content': 'You are Hermes.'}, {'role': 'user', 'content': 'hi'}]


def utf8_sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def display_summary(text, limit=512):
    """Test double of a rune (Unicode code point) display cap. Not used by the transport."""
    points = list(text)
    if len(points) <= limit:
        return text
    return ''.join(points[:limit]) + '…'


def run(fake_agy, spec, wait='2'):
    command, env, _record, set_scenario = fake_agy
    set_scenario('authority')
    env['ANTIGRAVITY_AGY_PREWARM'] = '0'
    env['ANTIGRAVITY_AGY_CAPTURE_WAIT'] = str(wait)
    env['FAKE_AGY_AUTHORITY'] = json.dumps(spec, ensure_ascii=False)
    return Client(command=command, env=env).chat.completions.create(
        model='gemini-3.8-flash-low', messages=USER, tools=TOOLS)


def executed(response, field):
    (call,) = response.choices[0].message.tool_calls
    return call.function.name, json.loads(call.function.arguments)[field]


def _case(field, tool, text):
    return {
        'field': field,
        'tool': tool,
        'text': text,
        'summary': display_summary(text),
    }


def _long_cases():
    cases = []
    for field, tool in (('command', 'terminal'), ('content', 'write_file')):
        for size in (511, 512, 513, 2048):
            cases.append(_case(field, tool, 'a' * size))
        cases.append(_case(field, tool, '测' * 513))
        cases.append(_case(field, tool, '😀' * 513))
        cases.append(_case(field, tool, 'echo ... ' + ('文' * 600) + '…tail'))
    cases.append(_case('command', 'terminal', 'echo ...'))
    return cases


@pytest.mark.parametrize('case', _long_cases(), ids=lambda case: f"{case['tool']}-{case['field']}-{len(list(case['text']))}")
def test_executed_argument_matches_capture_bytes(fake_agy, case):
    text = case['text']
    arguments = {case['field']: text}
    if case['tool'] == 'write_file':
        arguments = {'path': 'out.txt', 'content': text}
    response = run(fake_agy, {
        'tool': case['tool'],
        'summary': case['summary'] if case['field'] == 'command' else {'path': 'out.txt', 'content': case['summary']},
        'arguments': arguments,
        'capture_first': True,
    })
    name, got = executed(response, case['field'])
    assert name == case['tool']
    assert utf8_sha(got) == utf8_sha(text)
    assert got == text


def test_delayed_capture_is_accepted_and_summary_is_not(fake_agy):
    text = 'b' * 513
    summary = display_summary(text)
    assert summary != text and summary.endswith('…')
    response = run(fake_agy, {
        'tool': 'terminal',
        'summary': summary,
        'arguments': {'command': text},
        'capture_first': False,
        'delay': 0.15,
    })
    _name, got = executed(response, 'command')
    assert got == text
    assert got != summary


def test_missing_capture_fails_even_when_summary_is_complete(fake_agy):
    text = 'echo hello'
    with pytest.raises(AgyToolCaptureError, match='did not arrive'):
        run(fake_agy, {
            'tool': 'terminal',
            'summary': text,
            'arguments': {'command': text},
            'skip_capture': True,
        }, wait='0.2')


def test_mismatched_capture_fails_closed(fake_agy):
    with pytest.raises(AgyToolCaptureError, match='does not correlate'):
        run(fake_agy, {
            'tool': 'terminal',
            'summary': 'echo hi',
            'capture_name': 'write_file',
            'arguments': {'content': 'echo hi'},
            'capture_first': True,
        }, wait='0.3')


def test_ambiguous_capture_fails_closed(fake_agy):
    with pytest.raises(AgyToolCaptureError, match='ambiguous'):
        run(fake_agy, {
            'tool': 'terminal',
            'summary': 'echo hi',
            'arguments': {'command': 'echo hi'},
            'extra': {'name': 'terminal', 'arguments': {'command': 'echo other'}},
            'capture_first': True,
        }, wait='0.3')


def test_non_object_arguments_fail_closed(fake_agy):
    with pytest.raises(AgyToolCaptureError, match='does not correlate'):
        run(fake_agy, {
            'tool': 'terminal',
            'summary': 'echo hi',
            'arguments': ['echo hi'],
            'capture_first': True,
        }, wait='0.3')
