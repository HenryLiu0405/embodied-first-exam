"""Scripted Responses events for tests; never contacts a paid provider."""

import json
import time

MOCK_USAGE = {
    "input_tokens": 100,
    "output_tokens": 20,
    "total_tokens": 120,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens_details": {"reasoning_tokens": 5},
}


def stream_response(payload, index, call=None, text="Protocol check finished."):
    """Emit a minimal Responses stream for a scripted native SDK check."""
    response = {
        "id": f"resp_{index}",
        "object": "response",
        "created_at": int(time.time()),
        "model": payload["model"],
        "status": "in_progress",
        "output": [],
        "usage": None,
    }
    yield {"type": "response.created", "response": dict(response)}
    if call:
        name, arguments = call
        item = {
            "type": "function_call",
            "id": f"fc_{index}",
            "call_id": f"call_{index}",
            "name": "adaptive__" + name,
            "arguments": "",
            "status": "in_progress",
        }
        yield {"type": "response.output_item.added", "output_index": 0, "item": dict(item)}
        encoded = json.dumps(arguments)
        yield {
            "type": "response.function_call_arguments.delta",
            "item_id": item["id"],
            "output_index": 0,
            "delta": encoded,
        }
        item.update(arguments=encoded, status="completed")
        yield {
            "type": "response.function_call_arguments.done",
            "item_id": item["id"],
            "output_index": 0,
            "arguments": encoded,
        }
    else:
        item = {
            "type": "message",
            "id": f"msg_{index}",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        yield {"type": "response.output_item.added", "output_index": 0, "item": item}
    yield {"type": "response.output_item.done", "output_index": 0, "item": item}
    response.update(status="completed", output=[item], usage=MOCK_USAGE)
    yield {"type": "response.completed", "response": response}
