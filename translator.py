import base64
import json
import sys
import uuid
from typing import Dict, Any, Generator, Tuple, Union, List, Optional

from models import get_default_model, get_models

THOUGHT_SIGNATURE_CACHE: Dict[str, str] = {}

# Gemini returns STOP even when it stopped for a reason worth reporting, so the
# absent case and the ordinary case both land on end_turn.
_FINISH_REASON_TO_STOP_REASON = {
    'MAX_TOKENS': 'max_tokens',
}


def _log(message: str) -> None:
    sys.stderr.write(f"[translator] {message}\n")


def _recover_thought_signature(tool_id: str) -> Optional[str]:
    """Recover a thoughtSignature for a tool_use id coming back from the client.

    The cache is authoritative. Older ids carry the signature in their own tail
    (see `make_tool_id`), and those are still in saved sessions, so the tail is
    kept as a fallback — but only when it decodes. A tail that arrives truncated
    re-pads to something no decoder accepts, and the gateway answers the whole
    request with `Invalid value at ... thought_signature (TYPE_BYTES), Base64
    decoding failed`, which kills the turn. Dropping the signature only costs
    the model its reasoning continuity for that one call, so an unusable tail is
    worth strictly less than no tail at all.
    """
    cached = THOUGHT_SIGNATURE_CACHE.get(tool_id)
    if cached:
        return cached

    if not tool_id.startswith('toolu_'):
        return None
    segments = tool_id.split('_', 2)
    if len(segments) != 3 or not segments[2]:
        return None

    raw_sig = segments[2].replace('-', '+').replace('_', '/')
    remainder = len(raw_sig) % 4
    if remainder == 1:
        # No base64 string is ever 1 mod 4 — this tail lost characters in transit.
        _log(f"dropping truncated thoughtSignature from tool id (tail {len(raw_sig)} chars)")
        return None
    if remainder:
        raw_sig += '=' * (4 - remainder)

    try:
        base64.b64decode(raw_sig, validate=True)
    except Exception:
        _log("dropping undecodable thoughtSignature from tool id")
        return None
    return raw_sig


def _stringify_tool_result(res_content: Any) -> str:
    if isinstance(res_content, str):
        return res_content
    if isinstance(res_content, list):
        collected = []
        for sub in res_content:
            if isinstance(sub, dict) and sub.get('type') == 'text':
                collected.append(sub.get('text', ''))
            elif isinstance(sub, str):
                collected.append(sub)
        return "\n".join(collected)
    if res_content is None:
        return ""
    try:
        return json.dumps(res_content, ensure_ascii=False)
    except Exception:
        return str(res_content)


def _describe_unsigned_call(name: str, args: Any) -> str:
    try:
        rendered = json.dumps(args, ensure_ascii=False)
    except Exception:
        rendered = str(args)
    if len(rendered) > 2000:
        rendered = rendered[:2000] + '…'
    return f"[earlier tool call] {name or 'unknown tool'}({rendered})"


def _describe_unsigned_result(name: str, res_content: Any, is_error: bool) -> str:
    body = _stringify_tool_result(res_content)
    if len(body) > 8000:
        body = body[:8000] + '…'
    label = 'failed' if is_error else 'returned'
    return f"[earlier tool result] {name or 'unknown tool'} {label}: {body}"


def _stop_reason_for(finish_reason: Optional[str], has_tool_use: bool) -> str:
    mapped = _FINISH_REASON_TO_STOP_REASON.get(finish_reason or '')
    if mapped:
        return mapped
    return 'tool_use' if has_tool_use else 'end_turn'


def _empty_content_notice(finish_reason: Optional[str]) -> str:
    """Text to stand in for a reply that arrived with nothing in it.

    Returning an empty string here is indistinguishable, from the user's side,
    from the agent ignoring them: the turn ends, the transcript records an
    assistant message, and the chat shows nothing at all. Say what happened
    instead.
    """
    if finish_reason == 'MAX_TOKENS':
        return ("[proxy] The model hit its output limit before writing any reply. "
                "Raise max_tokens or shorten the conversation, then try again.")
    if finish_reason in ('SAFETY', 'RECITATION', 'PROHIBITED_CONTENT', 'BLOCKLIST', 'SPII', 'IMAGE_SAFETY'):
        return f"[proxy] The model stopped without replying (finishReason: {finish_reason})."
    if finish_reason:
        return f"[proxy] The model returned no content (finishReason: {finish_reason})."
    return "[proxy] The model returned no content and gave no reason."


def _collapse_any_of(branches: List[Any]) -> Dict[str, Any]:
    """Fold an `anyOf` union down to a single schema.

    Gemini's schema dialect has no `anyOf`, and the gateway does not just ignore
    it: for the Claude models it round-trips the declaration back into an
    Anthropic tool and emits an `input_schema` the API rejects outright with
    "must match JSON Schema draft 2020-12". Every tool in the request dies with
    it, so one union in one parameter takes down the whole turn. `oneOf`,
    `allOf` and `not` survive the round-trip untouched — it is `anyOf`
    specifically.

    A union of literals is what the union nearly always is in practice (it is
    what `z.enum()` and friends compile to), and that folds exactly onto an
    enum. Anything richer falls back to the first branch, which keeps the
    parameter usable rather than dropping it.
    """
    subs = [b for b in branches if isinstance(b, dict)]
    if not subs:
        return {}

    # `.optional()` shows up as a bare null branch beside the real one.
    non_null = [b for b in subs if b.get('type') != 'null']
    nullable = len(non_null) != len(subs)
    if not non_null:
        return {'type': 'string', 'nullable': True}

    types = {b.get('type') for b in non_null}
    if len(types) == 1 and all(isinstance(b.get('enum'), list) for b in non_null):
        values: List[Any] = []
        for b in non_null:
            for val in b['enum']:
                if val not in values:
                    values.append(val)
        merged: Dict[str, Any] = {'enum': values}
        if non_null[0].get('type'):
            merged['type'] = non_null[0]['type']
    else:
        merged = dict(non_null[0])

    if nullable:
        merged['nullable'] = True
    return merged


def sanitize_schema(schema: Any) -> Any:
    # ponytail: strip/convert JSON schema keys not supported by Gemini parameters, handles deep recursion
    if not isinstance(schema, dict):
        if isinstance(schema, list):
            return [sanitize_schema(x) for x in schema]
        return schema

    disallowed_keys = {
        '$schema', '$id', '$comment', 'title', 'default',
        'additionalProperties', 'examples', 'definitions', '$defs', '$ref',
        'patternProperties', 'propertyNames'
    }

    # Keys under these are property NAMES chosen by the tool author, not schema
    # keywords. Filtering them dropped a real `title` property from aside's repl
    # tool, so the model never sent it and aside rejected the call.
    name_keyed = {'properties', 'patternProperties', '$defs', 'definitions'}

    cleaned: Dict[str, Any] = {}
    pending_any_of: Optional[List[Any]] = None
    for k, v in schema.items():
        if k in disallowed_keys:
            continue
        if k == 'anyOf' and isinstance(v, list):
            pending_any_of = [sanitize_schema(x) for x in v]
            continue
        if k == 'const':
            cleaned['enum'] = [v]
            continue
        if k == 'type' and isinstance(v, list):
            non_null = [t for t in v if t != 'null']
            cleaned['type'] = non_null[0] if non_null else 'string'
            cleaned['nullable'] = True
            continue

        if k in name_keyed and isinstance(v, dict):
            cleaned[k] = {name: sanitize_schema(sub) for name, sub in v.items()}
        elif isinstance(v, dict):
            cleaned[k] = sanitize_schema(v)
        elif isinstance(v, list):
            cleaned[k] = [sanitize_schema(x) for x in v]
        else:
            cleaned[k] = v

    if pending_any_of is not None:
        # Sibling keys were written by the tool author about the union as a
        # whole, so they outrank anything the branches carry.
        for k, v in _collapse_any_of(pending_any_of).items():
            cleaned.setdefault(k, v)

    if 'required' in cleaned and isinstance(cleaned['required'], list):
        props = cleaned.get('properties', {})
        if isinstance(props, dict):
            valid_req = [r for r in cleaned['required'] if r in props]
            if valid_req:
                cleaned['required'] = valid_req
            else:
                del cleaned['required']

    return cleaned


def anthropic_to_antigravity(anthropic_req: Dict[str, Any]) -> Dict[str, Any]:
    default_mod = get_default_model()
    model = anthropic_req.get('model', default_mod)
    active_ids = [m['id'] for m in get_models()]
    if not model or (model not in active_ids and ('claude' in model or 'anthropic' in model)):
        model = default_mod

    # ponytail: map Anthropic tools -> Gemini functionDeclarations
    tools_config = None
    tools = anthropic_req.get('tools')
    if isinstance(tools, list) and tools:
        fn_decls = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            name = t.get('name')
            if not name:
                continue
            decl: Dict[str, Any] = {'name': name}
            if t.get('description'):
                decl['description'] = t['description']
            schema = sanitize_schema(t.get('input_schema', {}))
            if not isinstance(schema, dict):
                schema = {'type': 'object', 'properties': {}}
            elif 'type' not in schema:
                schema['type'] = 'object'
            decl['parameters'] = schema
            fn_decls.append(decl)
        if fn_decls:
            tools_config = [{'functionDeclarations': fn_decls}]

    # ponytail: map tool_choice -> Gemini toolConfig
    tool_config = None
    tool_choice = anthropic_req.get('tool_choice')
    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get('type')
        if tc_type == 'auto':
            tool_config = {'functionCallingConfig': {'mode': 'AUTO'}}
        elif tc_type == 'any':
            tool_config = {'functionCallingConfig': {'mode': 'ANY'}}
        elif tc_type == 'tool' and tool_choice.get('name'):
            tool_config = {'functionCallingConfig': {'mode': 'ANY', 'allowedFunctionNames': [tool_choice['name']]}}
        elif tc_type == 'none':
            tool_config = {'functionCallingConfig': {'mode': 'NONE'}}

    contents: List[Dict[str, Any]] = []
    messages = anthropic_req.get('messages', [])
    tool_id_to_name: Dict[str, str] = {}
    unsigned_tool_ids: set = set()

    for msg in messages:
        role = msg.get('role', 'user')
        g_role = 'model' if role == 'assistant' else 'user'
        raw_content = msg.get('content', '')
        parts: List[Dict[str, Any]] = []

        if isinstance(raw_content, str):
            if raw_content:
                parts.append({'text': raw_content})
        elif isinstance(raw_content, list):
            for block in raw_content:
                if isinstance(block, dict):
                    b_type = block.get('type')
                    if b_type == 'text':
                        txt = block.get('text', '')
                        if txt:
                            parts.append({'text': txt})
                    elif b_type == 'image':
                        src = block.get('source', {})
                        if src.get('type') == 'base64':
                            parts.append({
                                'inline_data': {
                                    'mime_type': src.get('media_type', 'image/png'),
                                    'data': src.get('data', '')
                                }
                            })
                    elif b_type == 'tool_use':
                        t_id = block.get('id', '')
                        t_name = block.get('name', '')
                        t_input = block.get('input', {})
                        if t_id and t_name:
                            tool_id_to_name[t_id] = t_name

                        # ponytail: extract thoughtSignature required by Gemini 3.5/3.6 for multi-turn function calls
                        t_sig = _recover_thought_signature(t_id)

                        if not t_sig:
                            # The gateway rejects a functionCall with no signature just
                            # as hard as one with a broken signature: "Function call is
                            # missing a thought_signature ... required for tools to work
                            # correctly". Either way the turn dies. Replaying the call as
                            # narration keeps the history honest about what happened while
                            # leaving no functionCall part for it to object to.
                            unsigned_tool_ids.add(t_id)
                            parts.append({'text': _describe_unsigned_call(t_name, t_input)})
                            continue

                        fc_part: Dict[str, Any] = {
                            'functionCall': {
                                'name': t_name,
                                'args': t_input if isinstance(t_input, dict) else {}
                            },
                            'thoughtSignature': t_sig,
                        }
                        parts.append(fc_part)

                    elif b_type == 'tool_result':
                        t_use_id = block.get('tool_use_id', '')
                        t_name = tool_id_to_name.get(t_use_id, '')
                        res_content = block.get('content', '')
                        is_error = block.get('is_error', False)

                        if t_use_id in unsigned_tool_ids:
                            # Its functionCall became narration above, and a
                            # functionResponse with nothing to answer is its own error.
                            parts.append({'text': _describe_unsigned_result(t_name, res_content, is_error)})
                            continue

                        if is_error:
                            if isinstance(res_content, str):
                                resp_dict = {'error': res_content}
                            elif isinstance(res_content, dict):
                                resp_dict = res_content
                            else:
                                resp_dict = {'error': str(res_content)}
                        else:
                            if isinstance(res_content, dict):
                                resp_dict = res_content
                            elif isinstance(res_content, str):
                                resp_dict = {'output': res_content}
                            elif isinstance(res_content, list):
                                txt_parts = []
                                for sub in res_content:
                                    if isinstance(sub, dict) and sub.get('type') == 'text':
                                        txt_parts.append(sub.get('text', ''))
                                    elif isinstance(sub, str):
                                        txt_parts.append(sub)
                                resp_dict = {'output': "\n".join(txt_parts)}
                            else:
                                resp_dict = {'output': str(res_content) if res_content is not None else ""}

                        parts.append({
                            'functionResponse': {
                                'name': t_name,
                                'response': resp_dict
                            }
                        })
                elif isinstance(block, str):
                    if block:
                        parts.append({'text': block})

        if parts:
            contents.append({
                'role': g_role,
                'parts': parts
            })

    system_instruction = None
    sys_prompt = anthropic_req.get('system')
    if sys_prompt:
        sys_text = ""
        if isinstance(sys_prompt, str):
            sys_text = sys_prompt
        elif isinstance(sys_prompt, list):
            sys_text = "\n".join([
                b.get('text', '') if isinstance(b, dict) else str(b)
                for b in sys_prompt
            ])
        if sys_text:
            system_instruction = {
                'parts': [{'text': sys_text}]
            }

    gen_config: Dict[str, Any] = {}
    if 'max_tokens' in anthropic_req:
        # ponytail: reasoning models consume tokens during thinking phase; ensure max_output_tokens has a minimum floor
        user_max = anthropic_req['max_tokens']
        gen_config['max_output_tokens'] = max(user_max, 4096)
    if 'temperature' in anthropic_req:
        gen_config['temperature'] = anthropic_req['temperature']
    if 'top_p' in anthropic_req:
        gen_config['top_p'] = anthropic_req['top_p']

    ag_request: Dict[str, Any] = {'contents': contents}
    if system_instruction:
        ag_request['system_instruction'] = system_instruction
    if gen_config:
        ag_request['generation_config'] = gen_config
    if tools_config:
        ag_request['tools'] = tools_config
    if tool_config:
        ag_request['tool_config'] = tool_config

    return {
        'model': model,
        'request': ag_request
    }


def make_tool_id(thought_sig: Optional[str] = None) -> str:
    """Mint a tool_use id, keeping any thoughtSignature in the cache beside it.

    The signature used to be packed into the id itself so it could survive a
    proxy restart. It does not survive the trip: something between here and the
    next request caps the id at 64 characters, and every signature we have seen
    is far longer than the 49 characters that leaves. What came back was a
    truncated tail that no longer decoded, and the gateway rejected the entire
    request rather than the one signature — which is how a lost signature turned
    into a dead turn.

    A short id always fits under the cap, so the id survives and the cache
    answers for it. The cost is that a proxy restart drops the signatures it was
    holding; those calls lose reasoning continuity, which is the same price the
    truncated ones already paid, without taking the turn down with them.
    """
    tool_id = f"toolu_{uuid.uuid4().hex[:24]}"
    if thought_sig:
        THOUGHT_SIGNATURE_CACHE[tool_id] = thought_sig
    return tool_id


def process_antigravity_stream(
    response_stream,
    model: str,
    stream: bool
) -> Union[Generator[str, None, None], Dict[str, Any]]:
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    if stream:
        return _stream_generator(response_stream, msg_id, model)
    else:
        return _non_stream_response(response_stream, msg_id, model)


def _stream_generator(response_stream, msg_id: str, model: str) -> Generator[str, None, None]:
    start_event = {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}
        }
    }
    yield f"event: message_start\ndata: {json.dumps(start_event)}\n\n"

    current_index = 0
    active_block_type = None
    has_tool_use = False
    emitted_any_block = False
    finish_reason = None
    input_tokens = 0
    output_tokens = 0

    for raw_line in response_stream:
        line = raw_line.decode('utf-8').strip()
        if not line.startswith('data: '):
            continue
        data_json = line[6:].strip()
        if not data_json:
            continue

        try:
            payload = json.loads(data_json)
            resp = payload.get('response', {})
            usage = resp.get('usageMetadata', {})
            if usage:
                input_tokens = usage.get('promptTokenCount', input_tokens)
                output_tokens = usage.get('candidatesTokenCount', output_tokens)

            candidates = resp.get('candidates', [])
            for cand in candidates:
                if cand.get('finishReason'):
                    finish_reason = cand['finishReason']
                content = cand.get('content', {})
                parts = content.get('parts', [])
                for part in parts:
                    text = part.get('text', '')
                    if text:
                        if active_block_type != 'text':
                            if active_block_type is not None:
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': current_index})}\n\n"
                                current_index += 1
                            block_start = {
                                "type": "content_block_start",
                                "index": current_index,
                                "content_block": {"type": "text", "text": ""}
                            }
                            yield f"event: content_block_start\ndata: {json.dumps(block_start)}\n\n"
                            active_block_type = 'text'
                            emitted_any_block = True

                        delta_event = {
                            "type": "content_block_delta",
                            "index": current_index,
                            "delta": {"type": "text_delta", "text": text}
                        }
                        yield f"event: content_block_delta\ndata: {json.dumps(delta_event)}\n\n"

                    func_call = part.get('functionCall')
                    if func_call:
                        if active_block_type is not None:
                            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': current_index})}\n\n"
                            current_index += 1
                            active_block_type = None

                        fn_name = func_call.get('name', '')
                        fn_args = func_call.get('args', {})
                        thought_sig = part.get('thoughtSignature') or part.get('thought_signature')
                        tool_id = make_tool_id(thought_sig)

                        tool_start = {
                            "type": "content_block_start",
                            "index": current_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": fn_name,
                                "input": {}
                            }
                        }
                        yield f"event: content_block_start\ndata: {json.dumps(tool_start)}\n\n"

                        tool_delta = {
                            "type": "content_block_delta",
                            "index": current_index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": json.dumps(fn_args)
                            }
                        }
                        yield f"event: content_block_delta\ndata: {json.dumps(tool_delta)}\n\n"

                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': current_index})}\n\n"
                        current_index += 1
                        has_tool_use = True
                        emitted_any_block = True
                        active_block_type = None

        except Exception as exc:
            # Swallowing this silently drops whatever the chunk carried and
            # leaves no trace of why the reply came up short.
            _log(f"discarded a malformed stream chunk: {exc.__class__.__name__}: {exc}")

    if active_block_type is not None:
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': current_index})}\n\n"
        current_index += 1

    if not emitted_any_block:
        notice = _empty_content_notice(finish_reason)
        _log(f"stream produced no content blocks (finishReason: {finish_reason}) — sending notice instead")
        yield ("event: content_block_start\ndata: "
               f"{json.dumps({'type': 'content_block_start', 'index': current_index, 'content_block': {'type': 'text', 'text': ''}})}\n\n")
        yield ("event: content_block_delta\ndata: "
               f"{json.dumps({'type': 'content_block_delta', 'index': current_index, 'delta': {'type': 'text_delta', 'text': notice}})}\n\n")
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': current_index})}\n\n"

    stop_reason = _stop_reason_for(finish_reason, has_tool_use)
    msg_delta_event = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens}
    }
    yield f"event: message_delta\ndata: {json.dumps(msg_delta_event)}\n\n"

    msg_stop_event = {"type": "message_stop"}
    yield f"event: message_stop\ndata: {json.dumps(msg_stop_event)}\n\n"


def _non_stream_response(response_stream, msg_id: str, model: str) -> Dict[str, Any]:
    content_blocks: List[Dict[str, Any]] = []
    current_text = ""
    has_tool_use = False
    finish_reason = None
    input_tokens = 0
    output_tokens = 0

    for raw_line in response_stream:
        line = raw_line.decode('utf-8').strip()
        if not line.startswith('data: '):
            continue
        data_json = line[6:].strip()
        if not data_json:
            continue

        try:
            payload = json.loads(data_json)
            resp = payload.get('response', {})
            usage = resp.get('usageMetadata', {})
            if usage:
                input_tokens = usage.get('promptTokenCount', input_tokens)
                output_tokens = usage.get('candidatesTokenCount', output_tokens)

            candidates = resp.get('candidates', [])
            for cand in candidates:
                if cand.get('finishReason'):
                    finish_reason = cand['finishReason']
                content = cand.get('content', {})
                parts = content.get('parts', [])
                for part in parts:
                    text = part.get('text', '')
                    if text:
                        current_text += text
                    func_call = part.get('functionCall')
                    if func_call:
                        if current_text:
                            content_blocks.append({"type": "text", "text": current_text})
                            current_text = ""
                        thought_sig = part.get('thoughtSignature') or part.get('thought_signature')
                        tool_id = make_tool_id(thought_sig)
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tool_id,
                            "name": func_call.get('name', ''),
                            "input": func_call.get('args', {})
                        })
                        has_tool_use = True
        except Exception as exc:
            _log(f"discarded a malformed response chunk: {exc.__class__.__name__}: {exc}")

    if current_text:
        content_blocks.append({"type": "text", "text": current_text})

    if not content_blocks:
        _log(f"response had no content blocks (finishReason: {finish_reason}) — sending notice instead")
        content_blocks.append({"type": "text", "text": _empty_content_notice(finish_reason)})

    stop_reason = _stop_reason_for(finish_reason, has_tool_use)

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens
        }
    }

