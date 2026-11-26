"""Synthetic source histories verify replay recovery without answer leakage."""

import json

import pytest

from personalbench.recovery import evidence_catalog, extract_evidence, recover_task_inputs


def row(kind, **payload):
    return {
        "type": "response_item",
        "timestamp": "2026-11-26T15:05:00Z",
        "payload": {"type": kind, **payload},
    }


def call(name, arguments, output, cid="source"):
    return [
        row("function_call", name=name, arguments=json.dumps(arguments), call_id=cid),
        row("function_call_output", call_id=cid, output=json.dumps(output)),
    ]


def write_log(tmp_path, rows):
    path = tmp_path / "synthetic.jsonl"
    path.write_text("\n".join(json.dumps(value) for value in rows))
    return path


def goal(**values):
    return {
        "id": "daily",
        "brief": "Prepare a morning brief",
        "readiness": "incomplete",
        "timezone": "America/Los_Angeles",
        "provenance": {"source_message_indices": [0, 1]},
        **values,
    }


MESSAGES = [
    {"role": "user", "text": "Prepare a morning brief", "timestamp": "2026-11-26T15:00:00Z"},
    {"role": "assistant", "text": "PRIVATE HISTORICAL ANSWER", "timestamp": "2026-11-26T16:00:00Z"},
]


def test_calendar_and_mail_sources_enable_replay_without_trajectory_code(tmp_path):
    rows = call(
        "mcp__codex_apps__gmail_search_emails",
        {"query": "is:unread"},
        {
            "emails": [{"subject": "A deadline", "snippet": "Respond today"}],
            "next_page_token": "more",
        },
        "mail",
    )
    rows += call(
        "mcp__codex_apps__google_calendar_search_events",
        {"time_min": "2026-11-26T00:00:00-08:00"},
        {"events": []},
        "calendar",
    )
    rows += [
        row(
            "message",
            role="assistant",
            content=[{"type": "output_text", "text": "PRIVATE HISTORICAL ANSWER"}],
        )
    ]
    evidence = extract_evidence(write_log(tmp_path, rows))
    task = recover_task_inputs(
        goal(source_evidence_ids=[r["id"] for r in evidence["records"]]), evidence, MESSAGES
    )
    assert task["readiness"] == "ready"
    sources = json.loads(task["inputs"]["sources/observations.json"])
    assert sources["target_date"] == "2026-11-26"
    assert sources["records"][1]["text"] == '{"events": []}'
    assert "PRIVATE HISTORICAL ANSWER" not in json.dumps(task["inputs"])
    assert "query_or_transform_as_recorded" not in json.dumps(task["inputs"])
    assert '"query": "is:unread"' in json.dumps(sources)


def test_missing_calendar_does_not_turn_empty_mail_page_into_ready_goal(tmp_path):
    evidence = extract_evidence(
        write_log(tmp_path, call("mcp__codex_apps__gmail_search_emails", {}, {"emails": []}))
    )
    task = recover_task_inputs(goal(source_evidence_ids=["source-0001"]), evidence, MESSAGES)
    assert task["readiness"] == "incomplete"
    assert task["provenance"]["recovery"]["missing_kinds"] == ["calendar"]


def test_later_day_and_after_goal_sources_are_excluded(tmp_path):
    rows = call("mcp__codex_apps__gmail_search_emails", {}, {"emails": []})
    rows[1]["timestamp"] = "2026-11-27T15:05:00Z"
    evidence = extract_evidence(write_log(tmp_path, rows))
    task = recover_task_inputs(goal(source_evidence_ids=["source-0001"]), evidence, MESSAGES)
    assert task["readiness"] == "incomplete"
    assert task["provenance"]["recovery"]["accepted_ids"] == []
    assert (
        "after" in task["provenance"]["recovery"]["excluded"][0]["reason"]
        or "follows" in task["provenance"]["recovery"]["excluded"][0]["reason"]
    )


def test_local_day_comes_from_request_timezone_not_utc_date(tmp_path):
    rows = call("mcp__codex_apps__gmail_search_emails", {}, {"emails": []}, "mail")
    rows += call("mcp__codex_apps__google_calendar_search_events", {}, {"events": []}, "cal")
    for value in rows:
        value["timestamp"] = "2026-11-27T06:00:00Z"  # still November26 in Los Angeles
    messages = [
        {"role": "user", "timestamp": "2026-11-27T05:00:00Z"},
        {"role": "assistant", "timestamp": "2026-11-27T07:00:00Z"},
    ]
    evidence = extract_evidence(write_log(tmp_path, rows))
    result = recover_task_inputs(
        goal(source_evidence_ids=["source-0001", "source-0002"]), evidence, messages
    )
    assert result["readiness"] == "ready"
    assert result["provenance"]["recovery"]["target_date"] == "2026-11-26"


def test_unknown_and_duplicate_source_ids_fail_explicitly(tmp_path):
    evidence = extract_evidence(
        write_log(
            tmp_path,
            call("web.run", {"search_query": [{"q": "a paper"}]}, {"text": "Paper source"}),
        )
    )
    for ids in (["invented"], ["source-0001", "source-0001"]):
        with pytest.raises(ValueError):
            recover_task_inputs(goal(source_evidence_ids=ids), evidence, MESSAGES)


def test_mutations_tool_docs_and_post_change_file_reads_never_become_inputs(tmp_path):
    rows = [
        row("custom_tool_call", name="exec", input="text(ALL_TOOLS)", call_id="docs"),
        row(
            "custom_tool_call_output", call_id="docs", output="exec tool declaration: secret hints"
        ),
        row(
            "custom_tool_call",
            name="exec",
            input='text(await tools.exec_command({cmd:"cat input.txt"}))',
            call_id="before",
        ),
        row(
            "custom_tool_call_output",
            call_id="before",
            output=json.dumps({"output": "Genuine starting input", "exit_code": 0}),
        ),
        row(
            "custom_tool_call",
            name="exec",
            input='await tools.apply_patch("*** Begin Patch")',
            call_id="write",
        ),
        row("custom_tool_call_output", call_id="write", output="Solution written"),
        row(
            "custom_tool_call",
            name="exec",
            input='text(await tools.exec_command({cmd:"cat input.txt"}))',
            call_id="after",
        ),
        row("custom_tool_call_output", call_id="after", output="PRIVATE SOLUTION"),
    ]
    evidence = extract_evidence(write_log(tmp_path, rows))
    assert len(evidence["records"]) == 1
    assert evidence["records"][0]["text"] == "Genuine starting input"
    assert "PRIVATE SOLUTION" not in json.dumps(evidence_catalog(evidence))


def test_wait_output_keeps_original_source_call_and_truncation(tmp_path):
    code = 'const r=await tools.mcp__codex_apps__gmail_search_emails({query:"is:unread"});text(r);'
    rows = [
        row("custom_tool_call", name="exec", input=code, call_id="read"),
        row("custom_tool_call_output", call_id="read", output="Script running with cell ID cell-1"),
        row("function_call", name="wait", arguments='{"cell_id":"cell-1"}', call_id="wait"),
        row(
            "function_call_output",
            call_id="wait",
            output=repr(
                [
                    {
                        "type": "input_text",
                        "text": 'Warning: truncated output\n{"emails": [{"subject":"Captured subject"}]}',
                    }
                ]
            ),
        ),
    ]
    evidence = extract_evidence(write_log(tmp_path, rows))
    source = evidence["records"][0]
    assert source["originating_call_id"] == "read"
    assert source["available_kinds"] == ["email"]
    assert source["limits"]
    assert source["output_hash"]


def test_credentials_withheld_from_compiler_catalog(tmp_path):
    output = {"text": "token " + "sk-" + "x" * 30}
    evidence = extract_evidence(write_log(tmp_path, call("web.run", {}, output)))
    assert evidence["records"] == []
    assert "credentials" in evidence["excluded"][0]["reason"]
    assert "sk-" not in json.dumps(evidence_catalog(evidence))


def test_source_selection_is_explicit_not_all_session_evidence(tmp_path):
    evidence = extract_evidence(
        write_log(tmp_path, call("web.run", {}, {"text": "A genuine paper"}))
    )
    result = recover_task_inputs(
        {
            "brief": "Research a method",
            "readiness": "incomplete",
            "required_evidence_kinds": ["web"],
        },
        evidence,
        MESSAGES,
    )
    assert result["readiness"] == "incomplete"
    assert not result.get("inputs")


def test_literal_answer_in_load_view_is_not_a_source(tmp_path):
    rows = [
        row(
            "custom_tool_call",
            name="exec",
            input='const r=await tools.mcp__codex_apps__gmail_search_emails({});store("mail",r);text(r);',
            call_id="read",
        ),
        row("custom_tool_call_output", call_id="read", output='{"emails": []}'),
        row(
            "custom_tool_call",
            name="exec",
            input='const r=load("mail");text("PRIVATE ANSWER");',
            call_id="answer",
        ),
        row("custom_tool_call_output", call_id="answer", output="PRIVATE ANSWER"),
    ]
    evidence = extract_evidence(write_log(tmp_path, rows))
    assert len(evidence["records"]) == 1


@pytest.mark.parametrize(
    "content",
    ["", "  first line\n\nlast line\n", '{ "output": "a literal data field", "exit_code": 0 }\n'],
)
def test_complete_prechange_file_read_is_materialized_byte_faithfully(tmp_path, content):
    metadata_kind = "session_meta"
    rows = [{"type": metadata_kind, "payload": {"cwd": str(tmp_path)}}]
    rows += [
        row(
            "custom_tool_call",
            name="exec",
            input='text(await tools.exec_command({cmd:"cat input.txt"}))',
            call_id="read",
        ),
        row(
            "custom_tool_call_output",
            call_id="read",
            output=json.dumps({"output": content, "exit_code": 0}),
        ),
    ]
    evidence = extract_evidence(write_log(tmp_path, rows))
    task = {
        "id": "file",
        "brief": "Edit input.txt",
        "readiness": "incomplete",
        "source_evidence_ids": ["source-0001"],
        "required_evidence_kinds": ["file"],
    }
    result = recover_task_inputs(task, evidence, MESSAGES)
    assert result["inputs"]["input.txt"] == content
    assert result["provenance"]["recovered_files"]["input.txt"]["source_id"] == "source-0001"
    assert evidence_catalog(evidence)[0]["file_coverage"] == "full_file"


def test_excerpt_and_unsafe_shell_path_are_not_materialized(tmp_path):
    rows = [
        row(
            "custom_tool_call",
            name="exec",
            input='text(await tools.exec_command({cmd:"head -2 input.txt"}))',
            call_id="read",
        ),
        row(
            "custom_tool_call_output",
            call_id="read",
            output=json.dumps({"output": "one\ntwo\n", "exit_code": 0}),
        ),
    ]
    evidence = extract_evidence(write_log(tmp_path, rows))
    assert evidence_catalog(evidence)[0]["file_coverage"] == "source_excerpt_only"
    result = recover_task_inputs(
        {"brief": "Edit a file", "readiness": "incomplete", "source_evidence_ids": ["source-0001"]},
        evidence,
        MESSAGES,
    )
    assert "input.txt" not in result.get("inputs", {})
    assert result["readiness"] == "incomplete"


@pytest.mark.parametrize("command", ["head -2 resume.tex", "ls -la"])
def test_workspace_requires_complete_files_not_excerpts_or_directory_listing(tmp_path, command):
    rows = [
        row(
            "custom_tool_call",
            name="exec",
            input="text(await tools.exec_command({cmd:" + json.dumps(command) + "}))",
            call_id="read",
        ),
        row(
            "custom_tool_call_output",
            call_id="read",
            output=json.dumps({"output": "A captured source excerpt", "exit_code": 0}),
        ),
    ]
    evidence = extract_evidence(write_log(tmp_path, rows))
    task = {
        "brief": "Update the resume project",
        "requires_starting_state": True,
        "readiness": "incomplete",
        "execution_requirements": ["workspace"],
        "required_evidence_kinds": ["file"],
        "source_evidence_ids": ["source-0001"],
    }
    result = recover_task_inputs(task, evidence, MESSAGES)
    assert result["readiness"] == "incomplete"
    assert result["provenance"]["recovery"]["workspace_files_missing"] is True
    assert "complete starting files" in result["provenance"]["readiness_reason"]


def test_declared_missing_inputs_remain_missing_despite_recovered_web_or_file_evidence(tmp_path):
    evidence = extract_evidence(
        write_log(tmp_path, call("web.run", {}, {"text": "An API documentation source"}))
    )
    task = {
        "brief": "Revise a resume using the supplied original source and template assets",
        "readiness": "incomplete",
        "required_evidence_kinds": ["web"],
        "source_evidence_ids": ["source-0001"],
        "missing_inputs": ["resume.tex", "Original template asset archive"],
    }
    result = recover_task_inputs(task, evidence, MESSAGES)
    assert result["readiness"] == "incomplete"
    assert result["missing_inputs"] == task["missing_inputs"]
    assert result["provenance"]["recovery"]["missing_kinds"] == []
    assert "resume.tex" in result["provenance"]["readiness_reason"]


def test_complete_starting_file_resolves_its_declared_missing_input(tmp_path):
    rows = [
        row(
            "custom_tool_call",
            name="exec",
            input='text(await tools.exec_command({cmd:"cat resume.tex"}))',
            call_id="read",
        ),
        row(
            "custom_tool_call_output",
            call_id="read",
            output=json.dumps({"output": "Original resume source\n", "exit_code": 0}),
        ),
    ]
    evidence = extract_evidence(write_log(tmp_path, rows))
    result = recover_task_inputs(
        {
            "brief": "Edit resume.tex",
            "readiness": "incomplete",
            "requires_starting_state": True,
            "execution_requirements": ["workspace"],
            "required_evidence_kinds": ["file"],
            "source_evidence_ids": ["source-0001"],
            "missing_inputs": ["resume.tex"],
        },
        evidence,
        MESSAGES,
    )
    assert result["readiness"] == "ready"
    assert result["missing_inputs"] == []
    assert result["provenance"]["recovery"]["complete_input_paths"] == ["resume.tex"]
    assert json.loads(result["inputs"]["sources/coverage.json"])["missing_inputs"] == []


def test_identical_empty_results_from_different_calendar_queries_remain_distinct(tmp_path):
    rows = call(
        "mcp__codex_apps__google_calendar_search_events",
        {"calendar_id": "work"},
        {"events": []},
        "work",
    )
    rows += call(
        "mcp__codex_apps__google_calendar_search_events",
        {"calendar_id": "personal"},
        {"events": []},
        "personal",
    )
    evidence = extract_evidence(write_log(tmp_path, rows))
    assert len(evidence["records"]) == 2
    assert {r["query"]["calendar_id"] for r in evidence["records"]} == {"work", "personal"}
    assert evidence["records"][0]["text_hash"] == evidence["records"][1]["text_hash"]


def test_identical_file_contents_preserve_every_starting_path(tmp_path):
    rows = []
    for name in ("first.txt", "second.txt"):
        rows += [
            row(
                "custom_tool_call",
                name="exec",
                input="text(await tools.exec_command({cmd:" + json.dumps("cat " + name) + "}))",
                call_id=name,
            ),
            row(
                "custom_tool_call_output",
                call_id=name,
                output=json.dumps({"output": "", "exit_code": 0}),
            ),
        ]
    evidence = extract_evidence(write_log(tmp_path, rows))
    assert len(evidence["records"]) == 2
    task = {
        "brief": "Edit both files",
        "readiness": "incomplete",
        "execution_requirements": ["workspace"],
        "required_evidence_kinds": ["file"],
        "source_evidence_ids": [r["id"] for r in evidence["records"]],
        "missing_inputs": ["first.txt", "second.txt"],
    }
    result = recover_task_inputs(task, evidence, MESSAGES)
    assert result["readiness"] == "ready"
    assert result["inputs"]["first.txt"] == result["inputs"]["second.txt"] == ""
    assert result["missing_inputs"] == []


def test_duplicate_observation_records_with_same_origin_and_timestamp_are_deduplicated(tmp_path):
    rows = call("mcp__codex_apps__gmail_search_emails", {}, {"emails": []}, "mail")
    rows.append(rows[-1])
    evidence = extract_evidence(write_log(tmp_path, rows))
    assert len(evidence["records"]) == 1


def _core_daily_evidence(tmp_path):
    rows = call("mcp__codex_apps__gmail_search_emails", {}, {"emails": []}, "mail")
    rows += call("mcp__codex_apps__google_calendar_search_events", {}, {"events": []}, "calendar")
    return extract_evidence(write_log(tmp_path, rows))


def test_archived_daily_sources_satisfy_lookup_capabilities_and_conditional_memory(tmp_path):
    evidence = _core_daily_evidence(tmp_path)
    messages = [
        dict(
            MESSAGES[0],
            text="Automation: Daily brief\nAutomation memory: /private/memory.md\nLast run: yesterday\n\nGive me a morning brief with my calendar and important unread emails.",
        ),
        MESSAGES[1],
    ]
    task = goal(
        source_evidence_ids=["source-0001", "source-0002"],
        required_evidence_kinds=["calendar", "email", "memory"],
        execution_requirements=["live_accounts", "live_web", "unavailable_tool"],
        criteria=[{"description": "Use prior memory if available; disclose missing memory."}],
    )
    result = recover_task_inputs(task, evidence, messages)
    recovery = result["provenance"]["recovery"]
    assert result["readiness"] == "ready"
    assert recovery["missing_kinds"] == []
    assert recovery["optional_missing_kinds"] == ["memory"]
    assert recovery["declared_required_kinds"] == ["calendar", "email", "memory"]
    assert recovery["satisfied_execution_requirements"] == [
        "live_accounts",
        "live_web",
        "unavailable_tool",
    ]
    assert result["criteria"] == task["criteria"]
    assert result["execution_requirements"] == []
    assert recovery["declared_execution_requirements"] == task["execution_requirements"]


def test_mandatory_memory_requirement_cannot_be_cleared_by_core_sources(tmp_path):
    evidence = _core_daily_evidence(tmp_path)
    task = goal(
        source_evidence_ids=["source-0001", "source-0002"],
        required_evidence_kinds=["calendar", "email", "memory"],
        execution_requirements=["live_accounts"],
        criteria=[
            {
                "description": "Compare against the prior brief memory and identify new versus carried-forward tasks."
            }
        ],
    )
    result = recover_task_inputs(task, evidence, MESSAGES)
    assert result["readiness"] == "incomplete"
    assert result["provenance"]["recovery"]["missing_kinds"] == ["memory"]
    assert result["provenance"]["recovery"].get("satisfied_execution_requirements", []) == []


def test_user_requested_memory_comparison_is_not_optional_even_with_conditional_rubric(tmp_path):
    evidence = _core_daily_evidence(tmp_path)
    messages = [
        dict(MESSAGES[0], text="Give me a morning brief and compare it with the prior brief."),
        MESSAGES[1],
    ]
    task = goal(
        source_evidence_ids=["source-0001", "source-0002"],
        required_evidence_kinds=["calendar", "email", "memory"],
        criteria=[{"description": "Disclose missing memory."}],
    )
    result = recover_task_inputs(task, evidence, messages)
    assert result["readiness"] == "incomplete"
    assert result["provenance"]["recovery"].get("optional_missing_kinds", []) == []


def test_source_replay_does_not_satisfy_real_account_mutations(tmp_path):
    evidence = _core_daily_evidence(tmp_path)
    messages = [
        dict(
            MESSAGES[0], text="Give me a morning brief and archive the emails after reading them."
        ),
        MESSAGES[1],
    ]
    task = goal(
        source_evidence_ids=["source-0001", "source-0002"],
        required_evidence_kinds=["calendar", "email"],
        execution_requirements=["live_accounts", "publication"],
    )
    result = recover_task_inputs(task, evidence, messages)
    assert result["provenance"]["recovery"].get("satisfied_execution_requirements", []) == []


def test_mixed_tool_metadata_and_real_web_observations_keep_only_sources(tmp_path):
    metadata = [
        {"name": "mcp__codex_apps__notion_search", "description": "Harness-only tool instructions"}
    ]
    output = repr(
        [
            {"type": "input_text", "text": json.dumps(metadata)},
            {
                "type": "input_text",
                "text": "Primary paper at https://example.test/paper: captured research evidence.",
            },
        ]
    )
    rows = [
        row(
            "custom_tool_call",
            name="exec",
            input="text(ALL_TOOLS);text(await tools.web__run({search_query:[]}));",
            call_id="mixed",
        ),
        row("custom_tool_call_output", call_id="mixed", output=output),
    ]
    evidence = extract_evidence(write_log(tmp_path, rows))
    assert len(evidence["records"]) == 1
    assert "captured research evidence" in evidence["records"][0]["text"]
    assert "Harness-only" not in evidence["records"][0]["text"]


@pytest.mark.parametrize(
    "metadata",
    [
        [{"name": "mcp__codex_apps__notion_search", "description": "Tool metadata"}],
        ["mcp__codex_apps__notion_search", "exec_command"],
    ],
)
def test_pure_tool_metadata_is_not_a_source(tmp_path, metadata):
    evidence = extract_evidence(write_log(tmp_path, call("web.run", {}, metadata)))
    assert evidence["records"] == []


@pytest.mark.parametrize(
    "name,data,kind",
    [
        (
            "mcp__codex_apps__gmail_search_emails",
            [{"subject": "Respond today", "id": "mail"}],
            "email",
        ),
        (
            "mcp__codex_apps__google_calendar_search_events",
            [
                {
                    "name": "mcp__example",
                    "description": "A calendar event",
                    "id": "event",
                    "start": "2026-11-26T10:00:00-08:00",
                }
            ],
            "calendar",
        ),
        ("mcp__codex_apps__gmail_search_emails", [], "email"),
    ],
)
def test_source_arrays_are_not_confused_with_tool_metadata_or_mcp_blocks(
    tmp_path, name, data, kind
):
    evidence = extract_evidence(write_log(tmp_path, call(name, {}, data)))
    assert len(evidence["records"]) == 1
    assert json.loads(evidence["records"][0]["text"]) == data
    assert evidence["records"][0]["available_kinds"] == [kind]


def test_uninvoked_writing_guidance_is_removed_from_mixed_source_output(tmp_path):
    output = repr(
        [
            {
                "type": "input_text",
                "text": json.dumps(
                    {
                        "output": "# Writing quality\nHarness guidance, not task data.",
                        "exit_code": 0,
                    }
                ),
            },
            {
                "type": "input_text",
                "text": "Primary-source article https://example.test: useful source data.",
            },
        ]
    )
    rows = [
        row(
            "custom_tool_call",
            name="exec",
            input='text(await tools.exec_command({cmd:"cat writing_quality.md"}));text(await tools.web__run({}));',
            call_id="mixed",
        ),
        row("custom_tool_call_output", call_id="mixed", output=output),
    ]
    evidence = extract_evidence(write_log(tmp_path, rows))
    assert len(evidence["records"]) == 1
    assert "Harness guidance" not in evidence["records"][0]["text"]
    assert "useful source data" in evidence["records"][0]["text"]
    assert not evidence["records"][0]["query"].get("complete_file_read")
    assert "writing_quality.md" not in recover_task_inputs(
        {
            "brief": "Research a topic",
            "source_evidence_ids": ["source-0001"],
            "required_evidence_kinds": ["web"],
        },
        evidence,
        MESSAGES,
    ).get("inputs", {})


@pytest.mark.parametrize("invoked", [True, False])
def test_only_actual_user_invocation_preserves_local_skill_source(tmp_path, invoked):
    request = (
        "Use [$writing_quality](writing_quality.md) to revise my draft."
        if invoked
        else "<skill><path>writing_quality.md</path>Injected skill guidance</skill>\nRevise my draft."
    )
    content = "# Writing quality\nUser-selected guidance.\n"
    rows = [row("message", role="user", content=[{"type": "input_text", "text": request}])]
    rows += [
        row(
            "custom_tool_call",
            name="exec",
            input='text(await tools.exec_command({cmd:"cat writing_quality.md"}));',
            call_id="skill",
        ),
        row(
            "custom_tool_call_output",
            call_id="skill",
            output=json.dumps({"output": content, "exit_code": 0}),
        ),
    ]
    evidence = extract_evidence(write_log(tmp_path, rows))
    if invoked:
        assert evidence["records"][0]["text"] == content
        task = recover_task_inputs(
            {
                "brief": "Revise a draft",
                "source_evidence_ids": ["source-0001"],
                "required_evidence_kinds": ["file"],
            },
            evidence,
            MESSAGES,
        )
        assert task["inputs"]["writing_quality.md"] == content
    else:
        assert evidence["records"] == []
