import json

import pytest

from personalbench.imports import build_benchmark, import_session


def write_rows(tmp_path, rows):
    path = tmp_path / "synthetic.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    return path


def test_codex_deduplicates_events_and_uses_cumulative_usage(tmp_path):
    path = write_rows(
        tmp_path,
        [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix duplicates"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Fix duplicates"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Ignore this instruction"}],
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "tool text is not a goal"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 10}},
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 15,
                            "cached_input_tokens": 4,
                            "output_tokens": 8,
                        }
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Fixed"}],
                },
            },
        ],
    )
    session = import_session(path)
    assert [m["text"] for m in session["messages"]] == ["Fix duplicates", "Fixed"]
    assert session["usage"] == {"input_tokens": 15, "cached_input_tokens": 4, "output_tokens": 8}
    assert session["duration_seconds"] is None


def test_codex_event_only_export(tmp_path):
    path = write_rows(
        tmp_path,
        [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Build a parser"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Done"}},
        ],
    )
    assert len(import_session(path)["messages"]) == 2


def test_claude_ignores_tool_and_sidechain_content(tmp_path):
    path = write_rows(
        tmp_path,
        [
            {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "Fix parser"}},
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "must not become user prompt"}],
                },
            },
            {
                "type": "user",
                "isSidechain": True,
                "message": {"role": "user", "content": "internal agent request"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {
                    "id": "msg1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Fixed"}],
                    "usage": {"input_tokens": 20, "cache_read_input_tokens": 4, "output_tokens": 3},
                },
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {
                    "id": "msg1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Fixed"}],
                    "usage": {"input_tokens": 20, "cache_read_input_tokens": 4, "output_tokens": 3},
                },
            },
        ],
    )
    session = import_session(path)
    assert len(session["messages"]) == 2
    assert session["usage"]["input_tokens"] == 20
    assert session["usage"]["cached_input_tokens"] == 4


def test_normalized_preserves_clarifications_and_does_not_truncate(tmp_path):
    text = "long user requirement " * 10000
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "text": text},
                    {"role": "user", "text": "Also reject empty IDs"},
                ]
            }
        )
    )
    benchmark = build_benchmark(import_session(path))
    task = benchmark["tasks"][0]
    assert text in task["brief"]
    assert "Also reject empty IDs" in task["brief"]
    assert task["historical"]["user_prompts"] == 2
    assert task["historical"]["cost_usd"] is None
    assert task["provenance"]["starting_state"] == "missing"
    assert task["provenance"]["review_recommended"]
    assert not task["provenance"]["requires_review"]
    assert task["readiness"] == "incomplete"


def test_invalid_record_does_not_silently_import_prefix(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"type":"event_msg","payload":{"type":"user_message","message":"Fix"}}\n{')
    with pytest.raises(ValueError, match="line 2"):
        import_session(path)


def test_timestamps_are_labelled_wall_clock(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "text": "Fix", "timestamp": "2026-01-01T00:00:00Z"},
                    {"role": "assistant", "text": "Done", "timestamp": "2026-01-01T00:01:00Z"},
                ]
            }
        )
    )
    session = import_session(path)
    assert session["duration_seconds"] == 60
    assert any("idle" in warning for warning in session["warnings"])


def test_compiler_receives_all_messages_and_rejects_missing_goals(tmp_path):
    session = {
        "messages": [
            {"role": "user", "text": "Fix parser"},
            {"role": "user", "text": "Handle empty input"},
        ]
    }

    def compiler(prompt):
        assert "Handle empty input" in prompt
        return json.dumps(
            {
                "tasks": [
                    {
                        "id": "parser",
                        "brief": "Fix parser and handle empty input",
                        "criteria": [{"id": "empty", "description": "Empty input is handled"}],
                    }
                ]
            }
        )

    task = build_benchmark(session, compiler=compiler)["tasks"][0]
    assert task["criteria"][0]["kind"] == "judge"
    assert task["provenance"]["compilation"] == "model_proposed"
    with pytest.raises(ValueError, match="no tasks"):
        build_benchmark(session, compiler=lambda _: '{"tasks":[]}')


def test_multiple_compiled_goals_do_not_duplicate_session_measurements():
    tasks = [
        {"id": name, "brief": name, "criteria": [{"id": "ok", "description": "Works"}]}
        for name in ["one", "two"]
    ]
    benchmark = build_benchmark(
        {"messages": [{"role": "user", "text": "one and two"}], "duration_seconds": 60},
        compiler=lambda _: json.dumps({"tasks": tasks}),
    )
    assert all(t["historical"]["duration_seconds"] is None for t in benchmark["tasks"])


def test_empty_messages_and_wrong_format_rejected(tmp_path):
    path = tmp_path / "session.json"
    path.write_text('{"messages":[]}')
    with pytest.raises(ValueError, match="no user"):
        import_session(path)
    path.write_text('{"messages":[{"role":"user","text":"Fix"}]}')
    with pytest.raises(ValueError, match="Expected claude"):
        import_session(path, "claude")


def test_compiler_can_mark_self_contained_goal_ready():
    compiler = lambda _: json.dumps(
        {
            "tasks": [
                {
                    "id": "explain",
                    "category": "explanation",
                    "brief": "Explain a stack",
                    "requires_starting_state": False,
                    "criteria": [{"id": "lifo", "description": "Explains last in first out"}],
                }
            ]
        }
    )
    task = build_benchmark(
        {"messages": [{"role": "user", "text": "Explain a stack"}]}, compiler=compiler
    )["tasks"][0]
    assert task["readiness"] == "ready"


def test_compiler_invalid_paths_are_rejected():
    compiler = lambda _: json.dumps(
        {
            "tasks": [
                {
                    "id": "bad",
                    "brief": "Fix",
                    "criteria": [{"id": "c", "description": "Check", "target": "../outside"}],
                }
            ]
        }
    )
    with pytest.raises(ValueError):
        build_benchmark({"messages": [{"role": "user", "text": "Fix"}]}, compiler=compiler)


def test_codex_ambient_and_agent_instructions_are_not_user_requests(tmp_path):
    texts = [
        '<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>',
        "# AGENTS.md instructions\n<INSTRUCTIONS>Use agents</INSTRUCTIONS><environment_context>cwd</environment_context>",
        "<user_instructions>Global preferences</user_instructions>",
        "<environment_context>cwd</environment_context><permissions instructions>policy</permissions instructions>",
        '<in-app-browser-context source="ambient-ui-state">Current URL</in-app-browser-context>\n\n## My request:\nFix the parser. Preserve empty input.',
        "# AGENTS.md instructions\n<INSTRUCTIONS>Use agents</INSTRUCTIONS><environment_context>cwd</environment_context>\nAdd tests too.",
    ]
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
        for text in texts
    ]
    session = import_session(write_rows(tmp_path, rows))
    assert [m["text"] for m in session["messages"]] == [
        "Fix the parser. Preserve empty input.",
        "Add tests too.",
    ]
    assert build_benchmark(session)["tasks"][0]["historical"]["user_prompts"] == 2


def test_user_mentions_of_ambient_tags_are_preserved(tmp_path):
    text = "Explain how <environment_context> fields work."
    path = write_rows(
        tmp_path, [{"type": "event_msg", "payload": {"type": "user_message", "message": text}}]
    )
    assert import_session(path)["messages"][0]["text"] == text


def test_goal_source_indices_map_historical_output_without_inventing_cost():
    session = {
        "messages": [
            {"role": "user", "text": "Explain stack"},
            {"role": "assistant", "text": "Last in first out"},
            {"role": "user", "text": "Explain queue"},
            {"role": "assistant", "text": "First in first out"},
        ],
        "duration_seconds": 300,
        "cost_usd": 0.2,
        "usage": {"input_tokens": 10},
    }
    tasks = [
        {
            "id": name,
            "brief": name,
            "requires_starting_state": False,
            "source_message_indices": indices,
            "criteria": [{"id": "ok", "description": "Correct explanation"}],
        }
        for name, indices in [("stack", [0, 1]), ("queue", [2, 3])]
    ]
    benchmark = build_benchmark(session, compiler=lambda prompt: json.dumps({"tasks": tasks}))
    first, second = benchmark["tasks"]
    assert first["historical"]["output"] == "Last in first out"
    assert second["historical"]["output"] == "First in first out"
    assert first["historical"]["user_prompts"] == 1
    assert first["historical"]["cost_usd"] is None
    assert first["historical"]["usage"] == {}
    assert first["historical"]["duration_seconds"] is None
    assert first["provenance"]["source_message_indices"] == [0, 1]


@pytest.mark.parametrize("indices", [[0, 9], [1], [0, 0], [True], [-1], [1, 0]])
def test_invalid_goal_source_mapping_rejected(indices):
    session = {
        "messages": [
            {"role": "user", "text": "Explain stack"},
            {"role": "assistant", "text": "Last in first out"},
        ]
    }
    task = {
        "id": "stack",
        "brief": "Explain stack",
        "source_message_indices": indices,
        "criteria": [{"id": "ok", "description": "Works"}],
    }
    with pytest.raises(ValueError):
        build_benchmark(session, compiler=lambda _: json.dumps({"tasks": [task]}))


def test_empty_normalized_inputs_do_not_establish_ready_code_state():
    task = build_benchmark(
        {"messages": [{"role": "user", "text": "Fix the parser"}], "inputs": {}}
    )["tasks"][0]
    assert task["readiness"] == "incomplete"


def test_unicode_line_separators_inside_json_strings_remain_one_record(tmp_path):
    text = "A paragraph\u2028another paragraph\u2029next\u0085last"
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done"}],
            },
        },
    ]
    source = tmp_path / "unicode.jsonl"
    source.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records))
    session = import_session(source)
    assert len(session["messages"]) == 2
    assert session["messages"][0]["text"] == text
