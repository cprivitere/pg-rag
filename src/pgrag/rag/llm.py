import json

import requests

LLM_URL = "http://localhost:8080/v1/chat/completions"


class LLMServerError(ConnectionError):
    pass


def _post(prompt, stream, temperature=0.2, seed=None):
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 8192,
        "stream": stream,
    }
    if seed is not None:
        payload["seed"] = seed
    return requests.post(
        LLM_URL,
        json=payload,
        timeout=300,
        stream=stream,
    )


def _server_error(exc):
    if isinstance(exc, requests.exceptions.ConnectionError):
        return LLMServerError(
            f"Cannot connect to LLM server at {LLM_URL}. Ensure llama.cpp is running on port 8080."
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return LLMServerError(f"LLM server at {LLM_URL} timed out after 300s.")
    return exc


def generate(prompt, temperature=0.2, seed=None):
    try:
        response = _post(prompt, stream=False, temperature=temperature, seed=seed)
        response.raise_for_status()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        raise _server_error(e) from e

    return response.json()["choices"][0]["message"]["content"]


def stream_generate(prompt, temperature=0.2, seed=None):
    """Stream an LLM completion as a generator of text deltas.

    Uses llama.cpp's OpenAI-compatible /v1/chat/completions endpoint
    (SSE). Yields incremental token strings; raises LLMServerError if
    the server is unreachable.
    """
    try:
        response = _post(prompt, stream=True, temperature=temperature, seed=seed)
        response.raise_for_status()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        raise _server_error(e) from e

    for line in response.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            break
        try:
            delta = json.loads(data)["choices"][0]["delta"].get("content")
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        if delta:
            yield delta
