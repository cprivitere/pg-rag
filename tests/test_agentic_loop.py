"""Contracts pgrag.agentic.loop.run_loop round protocol (monkeypatched
_post — never a live LLM): no-tool single generation, native tool_calls
round, fenced ```tool text-protocol round, and the max_rounds cap forcing
a final no-tools answer."""

import json

import pytest

from pgrag.agentic import loop


@pytest.fixture(scope="module")
def tiny_store(tmp_path_factory):
    """Minimal store so executed tool calls return real tool output."""
    import sqlite3

    from pgrag.agentic.store import _SCHEMA

    root = tmp_path_factory.mktemp("loopstore")
    cdn = root / "cdn"
    cdn.mkdir()
    (cdn / "items.json").write_text(
        json.dumps({"item_1": {"Name": "Empty Bottle", "Value": 10}}), encoding="utf-8"
    )
    db = root / "store.db"
    from pgrag.agentic.store import build_store

    build_store(
        db_path=str(db), cdn_dir=cdn, session_dir=root / "none", glogger_db=root / "none.db"
    )
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db


def _no_tools(messages, tools, generation, max_tokens):
    return {"choices": [{"message": {"role": "assistant", "content": "final answer"}}]}


def test_no_tool_rounds_returns_single_generation(tiny_store, monkeypatch):
    monkeypatch.setattr(loop, "_post", _no_tools)
    result = loop.run_loop("What is an Empty Bottle?", store_path=str(tiny_store))
    assert result["answer"] == "final answer"
    assert result["rounds"] == 0
    assert sorted(
        k for k in result if k in {"answer", "documents", "query_type", "rerank_used", "sources"}
    ) == [
        "answer",
        "documents",
        "query_type",
        "rerank_used",
        "sources",
    ]
    assert result["query_type"] == "agentic"
    assert result["rerank_used"] is False


def test_native_tool_call_round_appends_tool_message(tiny_store, monkeypatch):
    seen = {}

    def fake(messages, tools, generation, max_tokens):
        seen["tools"] = tools
        seen["messages"] = messages
        if len(messages) == 2:  # system + user: first model turn
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "sql_query",
                                        "arguments": json.dumps({"sql": "SELECT name FROM items"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert tool_msgs and tool_msgs[0]["tool_call_id"] == "call_1"
        assert "Empty Bottle" in tool_msgs[0]["content"]
        return {
            "choices": [{"message": {"role": "assistant", "content": "It is the Empty Bottle."}}]
        }

    monkeypatch.setattr(loop, "_post", fake)
    result = loop.run_loop("items?", store_path=str(tiny_store))
    assert result["answer"] == "It is the Empty Bottle."
    assert result["rounds"] == 1
    assert result["trace_rounds"][0]["tool"] == "sql_query"
    assert seen["tools"], "native tools= parameter must be passed"


def test_fenced_tool_block_round(tiny_store, monkeypatch):
    def fake(messages, tools, generation, max_tokens):
        if len(messages) == 2:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                'checking\n```tool\n{"name": "find_entities", '
                                '"args": {"query": "bottle"}}\n```'
                            ),
                        }
                    }
                ]
            }
        users = [m for m in messages if m.get("role") == "user"]
        assert "Tool results:" in users[-1]["content"]
        assert "Empty Bottle" in users[-1]["content"]
        return {"choices": [{"message": {"role": "assistant", "content": "Found: Empty Bottle."}}]}

    monkeypatch.setattr(loop, "_post", fake)
    result = loop.run_loop("bottle?", store_path=str(tiny_store))
    assert result["answer"] == "Found: Empty Bottle."
    assert result["rounds"] == 1
    assert result["trace_rounds"][0]["tool"] == "find_entities"


def test_invalid_tool_json_returns_parse_error_as_tool_result(tiny_store, monkeypatch):
    def fake(messages, tools, generation, max_tokens):
        if len(messages) == 2:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '```tool\n{"name": "sql_query", broken\n```',
                        }
                    }
                ]
            }
        users = [m for m in messages if m.get("role") == "user"]
        assert "Could not parse tool call" in users[-1]["content"]
        return {"choices": [{"message": {"role": "assistant", "content": "Giving a real answer."}}]}

    monkeypatch.setattr(loop, "_post", fake)
    result = loop.run_loop("q?", store_path=str(tiny_store))
    assert result["answer"] == "Giving a real answer."
    assert result["rounds"] == 1


def test_max_rounds_cap_forces_no_tools_final(tiny_store, monkeypatch):
    calls = []

    def fake(messages, tools, generation, max_tokens):
        calls.append(tools)
        if tools is None:
            return {
                "choices": [{"message": {"role": "assistant", "content": "Budget spent. Final."}}]
            }
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": 'more\n```tool\n{"name": "find_entities", "args": {"query": "x"}}\n```',
                    }
                }
            ]
        }

    monkeypatch.setattr(loop, "_post", fake)
    result = loop.run_loop("loop?", store_path=str(tiny_store), max_rounds=2)
    assert result["answer"] == "Budget spent. Final."
    assert result["rounds"] == 2
    # the final call must have tools=None (forced no-tools answer)
    assert calls[-1] is None
    assert all(c is not None for c in calls[:-1])


def test_history_is_threaded_into_messages(tiny_store, monkeypatch):
    history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]

    def fake(messages, tools, generation, max_tokens):
        assert messages[1]["role"] == "user" and messages[1]["content"] == "first question"
        assert messages[2]["role"] == "assistant" and messages[2]["content"] == "first answer"
        assert messages[3] == {"role": "user", "content": "second question"}
        return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

    monkeypatch.setattr(loop, "_post", fake)
    result = loop.run_loop("second question", history=history, store_path=str(tiny_store))
    assert result["answer"] == "done"


def test_empty_content_midloop_gets_digest_fallback_answer(tiny_store, monkeypatch):
    """Contract: a mid-loop turn that returns EMPTY content with NO tool calls
    (reasoning-model token-wall failure, finish_reason=length) must not be
    returned verbatim as the final answer — run_loop re-asks once with the
    gathered-data digest and returns that answer."""
    posts = []

    def fake(messages, tools, generation, max_tokens):
        posts.append({"n_msgs": len(messages), "tools": tools, "max_tokens": max_tokens})
        if tools is not None:
            if len(messages) == 2:  # first model turn: call a tool
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": 'researching\n```tool\n{"name": "find_entities", "args": {"query": "bottle"}}\n```',
                            }
                        }
                    ]
                }
            # post-tool-result turn: content empty, no tool calls (the bug)
            return {"choices": [{"message": {"role": "assistant", "content": ""}}]}
        # digest fallback turn: fresh 2-message conversation, no tools,
        # doubled max_tokens
        assert len(messages) == 2
        assert messages[0]["content"].startswith("You are a Project Gorgon game assistant.")
        assert "Empty Bottle" in messages[1]["content"]  # gathered data present
        assert "Original question" in messages[1]["content"]
        return {"choices": [{"message": {"role": "assistant", "content": "Digest answer."}}]}

    monkeypatch.setattr(loop, "_post", fake)
    result = loop.run_loop("What is an Empty Bottle?", store_path=str(tiny_store))
    assert result["answer"] == "Digest answer."
    assert result["rounds"] == 1  # one real tool round executed before the guard
    assert len(result["trace_rounds"]) == 1
    # the guard's re-ask must carry the doubled generation budget
    assert posts[-1]["max_tokens"] == loop.DEFAULT_MAX_TOKENS * 2
    assert posts[-1]["tools"] is None


def test_seed_context_embedded_in_system(tiny_store, monkeypatch):
    def fake(messages, tools, generation, max_tokens):
        system = messages[0]["content"]
        assert "Seed context (BM25" in system
        assert "sql_query(sql)" in system  # tool contract present
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    monkeypatch.setattr(loop, "_post", fake)
    loop.run_loop("Empty Bottle?", store_path=str(tiny_store))
