from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_plugins.record.record import runtime


def _result(text: str = "Draft clarification") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        provider="test-provider",
        model="test-model",
        usage=SimpleNamespace(
            input_tokens=11,
            output_tokens=7,
            total_tokens=18,
        ),
    )


def _bind_runtime(tmp_path, monkeypatch, complete):
    context = SimpleNamespace(
        profile_name="default",
        llm=SimpleNamespace(complete_structured=complete),
    )
    runtime.set_plugin_context(context)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)


def test_structured_completion_uses_sanitized_request_and_one_terminal(
    tmp_path, monkeypatch
):
    from hermes_cli import lifecycle

    events = []

    def invoke(name, **payload):
        events.append((name, payload))

    monkeypatch.setattr(lifecycle, "invoke_hook", invoke)

    def complete(**_kwargs):
        assert [name for name, _ in events] == ["pre_api_request"]
        return _result()

    _bind_runtime(tmp_path, monkeypatch, complete)
    result = runtime.complete_structured(
        trace_scope="workflow-1",
        purpose="workflow.clarify",
        instructions="Clarify the workflow.",
        input=[
            {"type": "text", "text": "Which account?"},
            {
                "type": "image",
                "file_name": "screen.png",
                "data": "binary-evidence-must-not-reach-hooks",
            },
        ],
        max_tokens=200,
    )

    assert result.text == "Draft clarification"
    assert [name for name, _ in events] == [
        "pre_api_request",
        "post_api_request",
        "on_session_end",
    ]
    pre = events[0][1]
    assert "request_messages" not in pre
    assert pre["request"] == {
        "method": "POST",
        "body": {
            "messages": [
                {"role": "system", "content": "Clarify the workflow."},
                {
                    "role": "user",
                    "content": "Which account?\n\n[image input: screen.png]",
                },
            ],
            "max_tokens": 200,
        },
    }
    assert "binary-evidence-must-not-reach-hooks" not in str(events)
    terminal = events[2][1]
    assert terminal["completed"] is True
    assert terminal["failed"] is False
    assert terminal["turn_exit_reason"] == "plugin_llm_completed"
    assert terminal["final_response"] == "Draft clarification"
    assert "api_request_id" not in terminal
    for identity in ("task_id", "session_id", "turn_id"):
        assert events[0][1][identity] == events[1][1][identity]
        assert events[1][1][identity] == terminal[identity]
    assert sum(name == "on_session_end" for name, _ in events) == 1


def test_structured_completion_closes_open_trace_on_provider_error(
    tmp_path, monkeypatch
):
    from hermes_cli import lifecycle

    events = []
    monkeypatch.setattr(
        lifecycle,
        "invoke_hook",
        lambda name, **payload: events.append((name, payload)),
    )

    def fail(**_kwargs):
        assert [name for name, _ in events] == ["pre_api_request"]
        raise RuntimeError("private provider failure detail")

    _bind_runtime(tmp_path, monkeypatch, fail)
    with pytest.raises(RuntimeError, match="private provider failure detail"):
        runtime.complete_structured(
            trace_scope="workflow-2",
            instructions="Repair the draft.",
            input=[{"type": "text", "text": "Try again"}],
        )

    assert [name for name, _ in events] == [
        "pre_api_request",
        "api_request_error",
        "on_session_end",
    ]
    assert "private provider failure detail" not in str(events)
    terminal = events[2][1]
    assert terminal["completed"] is False
    assert terminal["failed"] is True
    assert terminal["final_response"] is None
    assert sum(name == "on_session_end" for name, _ in events) == 1


def test_structured_completion_does_not_close_a_trace_that_never_opened(
    tmp_path, monkeypatch
):
    from hermes_cli import lifecycle

    attempts = []

    def fail_pre(name, **_payload):
        attempts.append(name)
        if name == "pre_api_request":
            raise RuntimeError("observer unavailable")

    monkeypatch.setattr(lifecycle, "invoke_hook", fail_pre)

    def fail(**_kwargs):
        raise RuntimeError("provider unavailable")

    _bind_runtime(tmp_path, monkeypatch, fail)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        runtime.complete_structured(
            trace_scope="workflow-3",
            instructions="Clarify.",
            input=[{"type": "text", "text": "Question"}],
        )

    assert attempts == ["pre_api_request"]


def test_structured_completion_success_emits_nothing_after_failed_pre(
    tmp_path, monkeypatch
):
    from hermes_cli import lifecycle

    attempts = []

    def fail_pre(name, **_payload):
        attempts.append(name)
        if name == "pre_api_request":
            raise RuntimeError("observer unavailable")

    monkeypatch.setattr(lifecycle, "invoke_hook", fail_pre)
    _bind_runtime(tmp_path, monkeypatch, lambda **_kwargs: _result())

    result = runtime.complete_structured(
        trace_scope="workflow-4",
        instructions="Clarify.",
        input=[{"type": "text", "text": "Question"}],
    )

    assert result.text == "Draft clarification"
    assert attempts == ["pre_api_request"]


def test_structured_completion_emits_terminal_when_post_hook_fails(
    tmp_path, monkeypatch
):
    from hermes_cli import lifecycle

    attempts = []

    def fail_post(name, **_payload):
        attempts.append(name)
        if name == "post_api_request":
            raise RuntimeError("observer post failure")

    monkeypatch.setattr(lifecycle, "invoke_hook", fail_post)
    _bind_runtime(tmp_path, monkeypatch, lambda **_kwargs: _result())

    result = runtime.complete_structured(
        trace_scope="workflow-5",
        instructions="Clarify.",
        input=[{"type": "text", "text": "Question"}],
    )

    assert result.text == "Draft clarification"
    assert attempts == [
        "pre_api_request",
        "post_api_request",
        "on_session_end",
    ]
    assert attempts.count("on_session_end") == 1


def test_agent_run_forwards_the_host_enforced_tool_boundary():
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return {"completed": True, "failed": False}

    runtime.set_plugin_context(
        SimpleNamespace(agent=SimpleNamespace(run=run))
    )
    allowed_tools = [
        "skills_list",
        "skill_view",
        "tool_search",
        "tool_describe",
        "record_submit_draft",
    ]

    result = runtime.run_agent(
        user_message="Draft the workflow.",
        system_prompt="Submit one draft.",
        task_id="record:planner:test",
        required_tools=["record_submit_draft"],
        allowed_tools=allowed_tools,
        full_capabilities=False,
    )

    assert result == {"completed": True, "failed": False}
    assert calls[0]["required_tools"] == ["record_submit_draft"]
    assert calls[0]["allowed_tools"] == allowed_tools
    assert calls[0]["full_capabilities"] is False
