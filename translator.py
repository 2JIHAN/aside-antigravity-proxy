import json
import uuid
from typing import Dict, Any, Generator, Tuple, Union, List, Optional

from models import get_default_model, get_models

THOUGHT_SIGNATURE_CACHE: Dict[str, str] = {}


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
    for k, v in schema.items():
        if k in disallowed_keys:
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
                        t_sig = THOUGHT_SIGNATURE_CACHE.get(t_id)
                        if not t_sig and t_id.startswith('toolu_') and '_' in t_id:
                            try:
                                parts_id = t_id.split('_', 2)
                                if len(parts_id) == 3:
                                    raw_sig = parts_id[2].replace('-', '+').replace('_', '/')
                                    pad = len(raw_sig) % 4
                                    if pad:
                                        raw_sig += '=' * (4 - pad)
                                    t_sig = raw_sig
                            except Exception:
                                pass

                        fc_part: Dict[str, Any] = {
                            'functionCall': {
                                'name': t_name,
                                'args': t_input if isinstance(t_input, dict) else {}
                            }
                        }
                        if t_sig:
                            fc_part['thoughtSignature'] = t_sig
                        parts.append(fc_part)

                    elif b_type == 'tool_result':
                        t_use_id = block.get('tool_use_id', '')
                        t_name = tool_id_to_name.get(t_use_id, '')
                        res_content = block.get('content', '')
                        is_error = block.get('is_error', False)

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
    # ponytail: generate tool_use id and optionally encode thoughtSignature for stateless recovery
    short_uuid = uuid.uuid4().hex[:8]
    if thought_sig:
        encoded_sig = thought_sig.replace('+', '-').replace('/', '_').rstrip('=')
        tool_id = f"toolu_{short_uuid}_{encoded_sig}"
        THOUGHT_SIGNATURE_CACHE[tool_id] = thought_sig
        return tool_id
    return f"toolu_{short_uuid}{uuid.uuid4().hex[:16]}"


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
                        active_block_type = None

        except Exception:
            pass

    if active_block_type is not None:
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': current_index})}\n\n"

    stop_reason = "tool_use" if has_tool_use else "end_turn"
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
        except Exception:
            pass

    if current_text:
        content_blocks.append({"type": "text", "text": current_text})

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    stop_reason = "tool_use" if has_tool_use else "end_turn"

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

