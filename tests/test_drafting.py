from __future__ import annotations

import json
import hashlib
import sys
import types
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from hermes_crunch import compiler, drafting


def _requirements():
    return {"commands": [], "network_origins": [], "apps": [], "files": []}


def _plan(execution, **extra):
    if (
        isinstance(execution, dict)
        and execution.get("no_agent") is False
        and "authorization" not in execution
    ):
        script_approval = (
            {
                "id": "execute-reviewed-script",
                "effect": "execute",
                "description": "Execute the exact reviewed script.",
            }
            if isinstance(execution.get("script"), dict)
            else None
        )
        execution = {
            **execution,
            "authorization": {
                "mode": "human_in_loop",
                "effects": [],
                "operations": [],
                "checkpoints": [],
                "script": script_approval,
            },
        }
    elif (
        isinstance(execution, dict)
        and execution.get("no_agent") is True
        and "authorization" not in execution
    ):
        execution = {
            **execution,
            "authorization": {
                "mode": "human_in_loop",
                "effects": [],
                "operations": [],
                "checkpoints": [],
                "script": {
                    "id": "execute-reviewed-script",
                    "effect": "execute",
                    "description": "Execute the exact reviewed script.",
                },
            },
        }
    authorization = (
        execution.get("authorization")
        if isinstance(execution, dict)
        and isinstance(execution.get("authorization"), dict)
        else {}
    )
    script_approval = authorization.get("script")
    if (
        "approval_steps" not in extra
        and isinstance(script_approval, dict)
    ):
        extra["approval_steps"] = [{
            "step": 1,
            "effect": "execute",
            "script_id": script_approval["id"],
        }]
    return {
        "selection_reason": "smallest reliable shape",
        "state_strategy": "not_required",
        "idempotency_strategy": "not_required",
        "steps": ["Create and verify the result."],
        "execution": execution,
        "warnings": [],
        **extra,
    }


def _protected_script_execution(
    path,
    body,
    *,
    checkpoint_id="delete-stale",
    effect="delete",
    description="Delete the stale row.",
):
    return {
        "no_agent": True,
        "script": {"path": path, "body": body},
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "operations": [],
            "checkpoints": [],
            "script": {
                "id": "execute-reviewed-script",
                "effect": "execute",
                "description": "Execute the exact reviewed script.",
            },
        },
    }


def _runtime(monkeypatch, response, captured, *, messages=None, repair_response=None):
    class FakeAgent:
        def __init__(self, **kwargs): captured["agent"] = kwargs
        def run_conversation(self, prompt):
            captured["prompt"] = prompt
            prompts = captured.setdefault("prompts", [])
            prompts.append(prompt)
            selected = repair_response if len(prompts) > 1 and repair_response is not None else response
            return {"final_response": json.dumps(selected), "messages": messages or []}
        def interrupt(self, _message): captured["interrupted"] = True

    modules = {
        "hermes_cli.config": types.SimpleNamespace(load_config=lambda: {"model": "test-model"}),
        "hermes_cli.runtime_provider": types.SimpleNamespace(resolve_runtime_provider=lambda **_: {"provider": "test"}),
        "hermes_cli.tools_config": types.SimpleNamespace(
            get_platform_tools=lambda *_: ["file", "terminal"]
        ),
        "run_agent": types.SimpleNamespace(AIAgent=FakeAgent),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def test_extract_json_accepts_one_object_with_harmless_suffix():
    assert drafting._extract_json('{"steps": []}.') == {"steps": []}


@pytest.mark.parametrize(
    "suffix",
    [
        " Hope this helps.",
        ' {"second": true}',
        " [1, 2]",
    ],
)
def test_extract_json_rejects_trailing_content(suffix):
    with pytest.raises(drafting.DraftingError, match="trailing content"):
        drafting._extract_json('{"steps": []}' + suffix)


def test_clarification_repairs_invalid_feedback_shape_once(tmp_path, monkeypatch):
    responses = [
        {
            "reply": "I will update the draft.",
            "state": "ready_to_redraft",
            "open_questions": [],
            "feedback_summary": {"change": "Keep both checks separate."},
        },
        {
            "reply": "I will update the draft.",
            "state": "ready_to_redraft",
            "open_questions": [],
            "feedback_summary": "Keep both checks separate.",
        },
    ]
    calls = []

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, prompt, **kwargs):
            calls.append({"prompt": prompt, **kwargs})
            return {"final_response": json.dumps(responses[len(calls) - 1])}

    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        types.SimpleNamespace(load_config=lambda: {"model": "test-model"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.runtime_provider",
        types.SimpleNamespace(
            resolve_runtime_provider=lambda **_: {"provider": "test"}
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "run_agent",
        types.SimpleNamespace(AIAgent=FakeAgent),
    )
    resets = []
    monkeypatch.setattr(
        drafting,
        "set_hermes_home_override",
        lambda _home: "profile-token",
    )
    monkeypatch.setattr(
        drafting,
        "reset_hermes_home_override",
        lambda token: resets.append((token, len(calls))),
    )

    result = drafting.clarify_workflow(
        workflow={"review": {"warnings": []}},
        draft_digest="digest",
        conversation_history=[],
        user_message="Keep the two checks separate.",
        home=tmp_path,
    )

    assert result["state"] == "ready_to_redraft"
    assert result["feedback_summary"] == "Keep both checks separate."
    assert len(calls) == 2
    assert resets == [("profile-token", 2)]
    assert "Clarification feedback is invalid" in calls[1]["prompt"]
    assert calls[1]["conversation_history"][-1]["role"] == "assistant"


@pytest.mark.parametrize(
    ("execution", "policy"),
    [
        ({"no_agent": True, "script": {"path": "job.py", "body": "print('ok')\n"}, "prompt": None, "skills": [], "enabled_toolsets": [], "requirements": _requirements()}, "never"),
        ({"no_agent": False, "script": {"path": "collect.py", "body": "print('data')\n"}, "prompt": "Judge and verify.", "skills": [], "enabled_toolsets": ["terminal"], "requirements": _requirements()}, "on_change"),
        ({"no_agent": False, "script": None, "prompt": "Inspect live state and verify.", "skills": ["existing-skill"], "enabled_toolsets": ["file"], "requirements": _requirements()}, "every_run"),
    ],
)
def test_author_workflow_uses_full_agent_and_supports_all_shapes(tmp_path, monkeypatch, execution, policy):
    recording = tmp_path / "recording"
    recording.mkdir()
    (recording / "trace.jsonl").write_text('{"secret":"raw"}\n', encoding="utf-8")
    (recording / "shot.png").write_bytes(b"png")
    captured = {}
    _runtime(monkeypatch, _plan(execution), captured)
    agents = []
    progress = lambda *_args, **_kwargs: None
    interim = lambda *_args, **_kwargs: None

    result = drafting.author_workflow(
        "Create the verified result.",
        {"steps": [{"action": "save"}], "schedule": "0 9 * * *"},
        recording_dir=recording,
        home=tmp_path,
        on_agent=agents.append,
        on_progress=progress,
        on_interim=interim,
    )

    assert result["llm_policy"] == policy
    assert agents and agents[0] is not None
    assert captured["agent"]["tool_progress_callback"] is progress
    assert captured["agent"]["interim_assistant_callback"] is interim
    assert captured["agent"]["enabled_toolsets"] == [
        "hermes-crunch-drafting",
        "vision",
    ]
    assert "Create the verified result." in captured["prompt"]
    assert '"schedule"' not in captured["prompt"]
    assert '"secret":"raw"' not in captured["prompt"]
    assert "shot.png" in captured["prompt"]
    assert "`authorization.effects` MUST be []" in captured["prompt"]
    assert "The agent does not call a separate approval tool" in captured["prompt"]
    assert "Only exact observational calls" in captured["prompt"]
    assert "setup navigation, clicks, typing, terminal commands, writes" in captured["prompt"]
    assert "Never emit `enabled:false`" in captured["prompt"]
    assert "Do not enable `delegation` or `delegate_task`" in captured["prompt"]
    assert "one enabled `authorization.script`" in captured["prompt"]
    assert "after the capability pass proved it unavailable" in captured["prompt"]
    assert "release-qualified computer_use" not in captured["prompt"]
    assert "never substitute open/cliclick" not in captured["prompt"]


def test_author_workflow_repairs_one_compiler_rejected_plan(tmp_path, monkeypatch):
    recording = tmp_path / "recording"
    recording.mkdir()
    captured = {}
    unsafe_execution = _protected_script_execution(
        "create_note.py",
        "create_note()\n",
        checkpoint_id="create-note",
        effect="create",
        description="Create the note.",
    )
    unsafe_execution["authorization"]["script"]["enabled"] = False
    repaired_execution = _protected_script_execution(
        "create_note.py",
        "create_note()\n",
        checkpoint_id="create-note",
        effect="create",
        description="Create the note.",
    )
    unsafe_plan = _plan(
        unsafe_execution,
        approval_steps=[{
            "step": 1,
            "effect": "execute",
            "script_id": "execute-reviewed-script",
        }],
    )
    repaired_plan = _plan(
        repaired_execution,
        approval_steps=[{
            "step": 1,
            "effect": "execute",
            "script_id": "execute-reviewed-script",
        }],
    )
    _runtime(
        monkeypatch,
        unsafe_plan,
        captured,
        repair_response=repaired_plan,
    )

    result = drafting.author_workflow(
        "Create the verified result.",
        {"steps": [{"action": "create"}]},
        recording_dir=recording,
        home=tmp_path,
    )

    assert len(captured["prompts"]) == 2
    assert "every planned non-observational agent call" in captured["prompts"][1]
    assert "Do not weaken, disable, catch, bypass, or remove" in captured["prompts"][1]
    assert result["execution"]["script"]["body"] == repaired_execution["script"]["body"]


def test_validator_accepts_agent_authored_native_script():
    execution = _protected_script_execution(
        "create_note.py",
        (
            "import subprocess\n"
            "subprocess.run([\n"
            "    '/usr/bin/osascript', '-e',\n"
            "    'tell application \"Notes\" to make new note with properties "
            "{name:\"Beta result\"}'\n"
            "], check=True)\n"
        ),
        checkpoint_id="create-note",
        effect="create",
        description="Create the reviewed note.",
    )
    execution["requirements"]["apps"] = ["Notes"]

    plan = compiler.validate_draft_plan(
        _plan(
            execution,
            approval_steps=[{
                "step": 1,
                "effect": "execute",
                "script_id": "execute-reviewed-script",
            }],
        ),
        available_toolsets={"file", "terminal"},
    )

    assert plan["execution"]["script"]["body"] == execution["script"]["body"]
    assert plan["execution"]["requirements"]["apps"] == ["Notes"]


def test_validator_and_saved_review_accept_agent_selected_native_tool_shape():
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Use remindctl to create the reviewed reminder.",
        "skills": ["apple-reminders"],
        "enabled_toolsets": ["terminal"],
        "requirements": {
            "commands": ["remindctl"],
            "network_origins": [],
            "apps": ["Reminders"],
            "files": [],
        },
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "operations": [{
                "id": "create-reminder",
                "effect": "create",
                "description": "Create the reviewed reminder.",
                "tool": "terminal",
                "arguments": {
                    "field": "command",
                    "contains": "remindctl",
                },
            }],
            "checkpoints": [],
        },
    }

    plan = compiler.validate_draft_plan(
        _plan(
            execution,
            approval_steps=[{
                "step": 1,
                "operation_id": "create-reminder",
                "effect": "create",
            }],
        ),
        available_toolsets={"terminal"},
    )
    workflow = {
        "execution": plan["execution"],
        "review": {"warnings": []},
    }

    assert compiler.review_blockers(workflow) == []
    normalized = compiler.normalize_workflow_execution(workflow)
    assert normalized["prompt"] == execution["prompt"]
    assert normalized["requirements"]["apps"] == ["Reminders"]


def test_validator_requires_default_deny_policy_for_agent_execution():
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Inspect the current state.",
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {"mode": "none"},
    }

    with pytest.raises(
        compiler.CompileError,
        match="requires a human_in_loop authorization policy",
    ):
        compiler.validate_draft_plan(
            _plan(execution),
            available_toolsets=set(),
        )


def test_observational_agent_retains_empty_default_deny_policy():
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Inspect the current state.",
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "operations": [],
            "checkpoints": [],
        },
    }

    plan = compiler.validate_draft_plan(
        _plan(execution),
        available_toolsets=set(),
    )

    assert plan["execution"]["authorization"]["mode"] == "human_in_loop"
    assert plan["execution"]["authorization"]["operations"] == []
    assert plan["approval_steps"] == []


def test_validator_requires_explicit_operation_for_planned_setup_click():
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": (
            "Click the reviewed setup control, then type the reviewed value "
            "and verify the result."
        ),
        "skills": [],
        "enabled_toolsets": ["computer_use"],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "operations": [
                {
                    "id": "open-reviewed-setup",
                    "effect": "execute",
                    "description": "Open the reviewed setup control.",
                    "tool": "computer_use",
                    "arguments": {"field": "action", "equals": "click"},
                },
                {
                    "id": "enter-reviewed-value",
                    "effect": "create",
                    "description": "Enter the reviewed value.",
                    "tool": "computer_use",
                    "arguments": {"field": "action", "equals": "type"},
                },
            ],
            "checkpoints": [],
        },
    }

    plan = compiler.validate_draft_plan(
        _plan(
            execution,
            approval_steps=[
                {
                    "step": 1,
                    "operation_id": "open-reviewed-setup",
                    "effect": "execute",
                },
                {
                    "step": 1,
                    "operation_id": "enter-reviewed-value",
                    "effect": "create",
                },
            ],
        ),
        available_toolsets={"computer_use"},
    )

    assert [item["id"] for item in plan["execution"]["authorization"]["operations"]] == [
        "open-reviewed-setup",
        "enter-reviewed-value",
    ]


def test_validator_rejects_model_disabled_operation():
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Click the reviewed control.",
        "skills": [],
        "enabled_toolsets": ["computer_use"],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "operations": [{
                "id": "click-reviewed",
                "effect": "execute",
                "description": "Click the reviewed control.",
                "tool": "computer_use",
                "arguments": {"field": "action", "equals": "click"},
                "enabled": False,
            }],
            "checkpoints": [],
        },
    }

    with pytest.raises(
        compiler.CompileError,
        match="Drafter-authored authorization operations must be enabled",
    ):
        compiler.validate_draft_plan(
            _plan(
                execution,
                approval_steps=[{
                    "step": 1,
                    "operation_id": "click-reviewed",
                    "effect": "execute",
                }],
            ),
            available_toolsets={"computer_use"},
        )


def test_validator_rejects_model_disabled_exact_script_approval():
    execution = _protected_script_execution("job.py", "write_result()\n")
    execution["authorization"]["script"]["enabled"] = False

    with pytest.raises(
        compiler.CompileError,
        match="Drafter-authored exact script approval must be enabled",
    ):
        compiler.validate_draft_plan(
            _plan(execution),
            available_toolsets={"file"},
        )


def test_validator_rejects_mode_none_for_no_agent_script():
    execution = {
        "no_agent": True,
        "script": {"path": "job.py", "body": "write_result()\n"},
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {"mode": "none"},
    }

    with pytest.raises(
        compiler.CompileError,
        match="No-agent execution requires one exact authorization.script",
    ):
        compiler.validate_draft_plan(
            _plan(execution),
            available_toolsets=set(),
        )


def test_validator_rejects_hybrid_script_without_exact_boundary():
    execution = {
        "no_agent": False,
        "script": {"path": "collect.py", "body": "print('data')\n"},
        "prompt": "Judge the collected data.",
        "skills": [],
        "enabled_toolsets": ["file"],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "operations": [],
            "checkpoints": [],
        },
    }

    with pytest.raises(
        compiler.CompileError,
        match="Every execution.script requires one exact authorization.script",
    ):
        compiler.validate_draft_plan(
            _plan(execution),
            available_toolsets={"file"},
        )


def test_validator_rejects_delegation_for_human_authorized_agent():
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Delegate the reviewed task.",
        "skills": [],
        "enabled_toolsets": ["delegation"],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "operations": [{
                "id": "delegate-reviewed-task",
                "effect": "execute",
                "description": "Delegate the reviewed task.",
                "tool": "delegate_task",
                "arguments": {"field": "task", "contains": "reviewed"},
            }],
            "checkpoints": [],
        },
    }

    with pytest.raises(
        compiler.CompileError,
        match="cannot enable delegation or delegate_task",
    ):
        compiler.validate_draft_plan(
            _plan(
                execution,
                approval_steps=[{
                    "step": 1,
                    "operation_id": "delegate-reviewed-task",
                    "effect": "execute",
                }],
            ),
            available_toolsets={"delegation"},
        )


def test_operator_authorization_override_rejects_script_approval_without_script():
    execution = {
        "no_agent": False,
        "script": None,
    }
    authorization = {
        "mode": "human_in_loop",
        "effects": [],
        "operations": [],
        "checkpoints": [],
        "script": {
            "id": "execute-reviewed-script",
            "effect": "execute",
            "description": "Execute the exact reviewed script.",
        },
    }

    with pytest.raises(
        compiler.CompileError,
        match="authorization.script requires execution.script",
    ):
        compiler.validate_execution_authorization(execution, authorization)


def test_compiler_rejects_blanket_effect_gates():
    execution = _protected_script_execution(
        "cleanup.py",
        "delete_stale_row(7)\n",
    )
    execution["authorization"]["effects"] = ["delete"]

    with pytest.raises(compiler.CompileError, match="blanket gates are unsupported"):
        compiler.validate_draft_plan(
            _plan(execution),
            available_toolsets={"file", "terminal"},
        )


def test_drafter_returns_valid_agent_plan_without_posthoc_discovery_veto(
    tmp_path, monkeypatch
):
    recording = tmp_path / "recording"
    recording.mkdir()
    captured = {}
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Use the selected skill to create the result.",
        "skills": ["example-skill"],
        "enabled_toolsets": ["terminal"],
        "requirements": _requirements(),
    }
    _runtime(monkeypatch, _plan(execution), captured, messages=[])

    plan = drafting.author_workflow(
        "Create the result.",
        {"steps": [{"action": "create", "app": "Example App"}]},
        recording_dir=recording,
        home=tmp_path,
    )

    assert plan["execution"]["skills"] == ["example-skill"]
    assert plan["execution"]["requirements"]["apps"] == []


def test_compiler_persists_exact_script_authorization_policy(tmp_path):
    execution = {
        "no_agent": True,
        "script": {
            "path": "cleanup.py",
            "body": "print('done')\n",
        },
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "target": "origin",
            "operations": [],
            "checkpoints": [],
            "script": {
                "id": "execute-reviewed-script",
                "effect": "execute",
                "description": "Execute the exact reviewed script.",
            },
        },
    }
    plan = compiler.validate_draft_plan(
        _plan(execution), available_toolsets={"file", "terminal"}
    )
    result = compiler.compile_workflow(
        {
            "id": "cleanup-workflow",
            "name": "Cleanup",
            "schedule": "every 5m",
            "deliver": "local",
            "execution": plan["execution"],
        },
        tmp_path / "home",
        tmp_path / "build",
    )

    assert result.payload["authorization"]["mode"] == "human_in_loop"
    assert result.payload["authorization"]["effects"] == []
    assert result.payload["authorization"]["script"]["effect"] == "execute"
    assert result.payload["authorization"]["script_sha256"]
    assert result.payload["authorization"]["execution_sha256"]
    assert result.payload["authorization"]["policy_id"]
    assert result.payload["no_agent"] is True
    review_markdown = (tmp_path / "build" / "review.md").read_text(
        encoding="utf-8"
    )
    assert "## Exact Script Source" in review_markdown
    assert "print('done')" in review_markdown
    assert "bound to the script and execution SHA-256 digests" in review_markdown
    summary = json.loads(
        (tmp_path / "build" / "compile-summary.json").read_text(encoding="utf-8")
    )
    assert set(summary) == {"payload", "payload_sha256", "provenance"}


def test_compiled_exact_script_runs_end_to_end(
    tmp_path, monkeypatch
):
    from cron.scheduler import _run_job_script

    hermes_home = tmp_path / "home"
    targets = [tmp_path / "first.txt", tmp_path / "second.txt"]
    execution = _protected_script_execution(
        "write_targets.py",
        (
            "from pathlib import Path\n"
            f"targets = [Path({str(targets[0])!r}), Path({str(targets[1])!r})]\n"
            "for target in targets:\n"
            "    target.write_text('done', encoding='utf-8')\n"
            "print('completed')\n"
        ),
        checkpoint_id="write-target",
        effect="update",
        description="Write one reviewed target.",
    )
    plan = compiler.validate_draft_plan(
        _plan(execution), available_toolsets={"file", "terminal"}
    )
    result = compiler.compile_workflow(
        {
            "id": "approval-prologue-workflow",
            "name": "Approval prologue workflow",
            "schedule": "every 5m",
            "deliver": "local",
            "execution": plan["execution"],
        },
        hermes_home,
        tmp_path / "build",
    )
    promotion = compiler.promote_compiled_artifacts(tmp_path / "build", hermes_home)
    promotion.commit()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    success, output = _run_job_script(result.payload["script"])

    assert success is True
    assert output == "completed"
    assert [target.read_text(encoding="utf-8") for target in targets] == [
        "done",
        "done",
    ]
    authorization = result.payload["authorization"]
    installed_script = hermes_home / "scripts" / result.payload["script"]
    assert authorization["script"]["id"] == "execute-reviewed-script"
    assert authorization["script_sha256"] == hashlib.sha256(
        installed_script.read_bytes()
    ).hexdigest()
    assert authorization["execution_sha256"]


def test_compiler_rejects_declared_checkpoint_missing_from_script():
    execution = {
        "no_agent": True,
        "script": {"path": "cleanup.py", "body": "print('no checkpoint')\n"},
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "delete-stale",
                    "effect": "delete",
                    "description": "Delete the stale row.",
                }
            ],
        },
    }
    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_requires_fail_fast_shell_checkpoint():
    execution = {
        "no_agent": True,
        "script": {
            "path": "cleanup.sh",
            "body": (
                '"$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
                "--checkpoint delete-stale --effect delete\n"
                "delete_stale_row 7\n"
            ),
        },
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "delete-stale",
                    "effect": "delete",
                    "description": "Delete the stale row.",
                }
            ],
        },
    }
    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


@pytest.mark.parametrize(
    "body",
    [
        (
            "# from hermes_crunch.approval_runtime import require_approval\n"
            "# require_approval('delete-stale', 'delete')\n"
            "print(\"require_approval('delete-stale', 'delete')\")\n"
        ),
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "def never_called():\n"
            "    require_approval('delete-stale', 'delete')\n"
            "print('done')\n"
        ),
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "if False:\n"
            "    require_approval('delete-stale', 'delete')\n"
            "print('done')\n"
        ),
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "False and require_approval('delete-stale', 'delete')\n"
        ),
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "True or require_approval('delete-stale', 'delete')\n"
        ),
    ],
)
def test_compiler_rejects_python_checkpoint_in_dead_text_or_code(body):
    execution = {
        "no_agent": True,
        "script": {"path": "cleanup.py", "body": body},
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "delete-stale",
                    "effect": "delete",
                    "description": "Delete the stale row.",
                }
            ],
        },
    }

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


@pytest.mark.parametrize(
    "body",
    [
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "try:\n"
            "    require_approval('delete-stale', 'delete')\n"
            "except Exception:\n"
            "    pass\n"
        ),
        (
            "from contextlib import suppress\n"
            "from hermes_crunch.approval_runtime import require_approval\n"
            "with suppress(Exception):\n"
            "    require_approval('delete-stale', 'delete')\n"
        ),
    ],
)
def test_compiler_rejects_python_checkpoint_that_can_suppress_denial(body):
    execution = {
        "no_agent": True,
        "script": {"path": "cleanup.py", "body": body},
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "delete-stale",
                    "effect": "delete",
                    "description": "Delete the stale row.",
                }
            ],
        },
    }

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


@pytest.mark.parametrize(
    "body",
    [
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "enabled = bool(input())\n"
            "enabled and require_approval('delete-stale', 'delete')\n"
        ),
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "async def gate():\n"
            "    require_approval('delete-stale', 'delete')\n"
            "gate()\n"
        ),
        (
            "import asyncio\n"
            "from hermes_crunch.approval_runtime import require_approval\n"
            "async def gate():\n"
            "    require_approval('delete-stale', 'delete')\n"
            "asyncio.run(gate())\n"
        ),
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "def gate():\n"
            "    yield require_approval('delete-stale', 'delete')\n"
            "gate()\n"
        ),
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "[require_approval('delete-stale', 'delete') for _ in []]\n"
        ),
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "def gate():\n"
            "    require_approval('delete-stale', 'delete')\n"
            "enabled = bool(input())\n"
            "enabled and gate()\n"
        ),
    ],
)
def test_compiler_rejects_deferred_python_checkpoint(body):
    execution = {
        "no_agent": True,
        "script": {"path": "cleanup.py", "body": body},
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "delete-stale",
                    "effect": "delete",
                    "description": "Delete the stale row.",
                }
            ],
        },
    }

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_rejects_checkpoint_inside_python_conditional():
    execution = _protected_script_execution(
        "cleanup.py",
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "enabled = bool(input())\n"
            "if enabled:\n"
            "    require_approval(\n"
            "        'delete-stale', 'delete', resource='row/7'\n"
            "    )\n"
            "    delete_stale_row(7)\n"
        ),
    )

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_rejects_per_item_checkpoint_inside_python_loop():
    execution = _protected_script_execution(
        "cleanup.py",
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "recipients = ['one@example.test', 'two@example.test']\n"
            "for recipient in recipients:\n"
            "    proposal = f'To: {recipient}\\nDraft: reviewed body'\n"
            "    require_approval(\n"
            "        'send-recipient', 'send', resource=proposal\n"
            "    )\n"
            "    send_reviewed_message(recipient)\n"
        ),
        checkpoint_id="send-recipient",
        effect="send",
        description="Send one reviewed message.",
    )

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_rejects_python_effect_before_approval():
    execution = _protected_script_execution(
        "cleanup.py",
        (
            "delete_stale_row(7)\n"
            "require_approval(\n"
            "    'delete-stale', 'delete', resource='row/7'\n"
            ")\n"
        ),
    )

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_rejects_python_effect_in_approval_argument():
    execution = _protected_script_execution(
        "cleanup.py",
        (
            "require_approval(\n"
            "    'delete-stale', 'delete', resource=delete_stale_row(7)\n"
            ")\n"
        ),
    )

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_rejects_shadowed_python_approval_helper():
    execution = {
        "no_agent": True,
        "script": {
            "path": "cleanup.py",
            "body": (
                "from hermes_crunch.approval_runtime import require_approval\n"
                "require_approval = lambda *_args, **_kwargs: None\n"
                "require_approval('delete-stale', 'delete')\n"
            ),
        },
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "delete-stale",
                    "effect": "delete",
                    "description": "Delete the stale row.",
                }
            ],
        },
    }

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_rejects_checkpoint_in_decorated_helper():
    execution = _protected_script_execution(
        "cleanup.py",
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "def noop(_function):\n"
            "    return lambda: None\n"
            "@noop\n"
            "def gate():\n"
            "    require_approval(\n"
            "        'delete-stale', 'delete', resource='row/7'\n"
            "    )\n"
            "gate()\n"
        ),
    )

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "from unittest.mock import Mock as require_approval\n"
            "require_approval(\n"
            "    'delete-stale', 'delete', resource='row/7'\n"
            ")\n",
            "shadow",
        ),
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "def gate():\n"
            "    require_approval(\n"
            "        'delete-stale', 'delete', resource='row/7'\n"
            "    )\n"
            "from builtins import print as gate\n"
            "gate()\n",
            "approval runtime",
        ),
    ],
)
def test_compiler_rejects_import_shadowed_python_checkpoint(body, error):
    execution = _protected_script_execution("cleanup.py", body)

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


@pytest.mark.parametrize(
    "body",
    [
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "def gate():\n"
            "    require_approval(\n"
            "        'delete-stale', 'delete', resource='row/7'\n"
            "    )\n"
            "__name__ = 'not-main'\n"
            "if __name__ == '__main__':\n"
            "    gate()\n"
        ),
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "def gate(__name__):\n"
            "    if __name__ == '__main__':\n"
            "        require_approval(\n"
            "            'delete-stale', 'delete', resource='row/7'\n"
            "        )\n"
            "gate('not-main')\n"
        ),
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "def gate():\n"
            "    if __name__ == '__main__':\n"
            "        require_approval(\n"
            "            'delete-stale', 'delete', resource='row/7'\n"
            "        )\n"
            "    __name__ = 'not-main'\n"
            "gate()\n"
        ),
    ],
)
def test_compiler_rejects_checkpoint_behind_rebound_name_main_guard(body):
    execution = _protected_script_execution(
        "cleanup.py",
        body,
    )

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "cleanup.py",
            "from hermes_crunch.approval_runtime import require_approval\n"
            "require_approval('delete-stale', 'delete')\n",
        ),
        (
            "cleanup.py",
            "from hermes_crunch.approval_runtime import require_approval\n"
            "require_approval(\n"
            "    'delete-stale', 'delete', resource='  '\n"
            ")\n",
        ),
        (
            "cleanup.sh",
            "set -e\n"
            '"$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
            "--checkpoint delete-stale --effect delete\n",
        ),
        (
            "cleanup.sh",
            "set -e\n"
            '"$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
            "--checkpoint delete-stale --effect delete --resource ''\n",
        ),
    ],
)
def test_compiler_requires_nonempty_runtime_proposal(path, body):
    execution = _protected_script_execution(path, body)

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


@pytest.mark.parametrize(
    "body",
    [
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "def main():\n"
            "    require_approval('delete-stale', 'delete', resource='row/7')\n"
            "main()\n"
        ),
        (
            "from hermes_crunch.approval_runtime import require_approval\n"
            "def main():\n"
            "    require_approval('delete-stale', 'delete', resource='row/7')\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        ),
    ],
)
def test_compiler_rejects_function_wrapped_python_checkpoint(body):
    execution = {
        "no_agent": False,
        "script": {
            "path": "cleanup.py",
            "body": body,
        },
        "prompt": "Summarize the cleanup.",
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "delete-stale",
                    "effect": "delete",
                    "description": "Delete the stale row.",
                }
            ],
        },
    }

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


@pytest.mark.parametrize(
    "body",
    [
        (
            "set -e\n"
            "# $HERMES_AUTHORIZATION_PYTHON $HERMES_AUTHORIZATION_RUNTIME request "
            "--checkpoint delete-stale --effect delete\n"
            "delete_stale_row 7\n"
        ),
        (
            "set -e\n"
            "echo '$HERMES_AUTHORIZATION_PYTHON $HERMES_AUTHORIZATION_RUNTIME request "
            "--checkpoint delete-stale --effect delete'\n"
            "delete_stale_row 7\n"
        ),
        (
            "cat <<'CHECKPOINT'\n"
            "$HERMES_AUTHORIZATION_PYTHON $HERMES_AUTHORIZATION_RUNTIME request "
            "--checkpoint delete-stale --effect delete\n"
            "CHECKPOINT\n"
            "delete_stale_row 7\n"
        ),
    ],
)
def test_compiler_rejects_shell_checkpoint_in_comments_or_data(body):
    execution = {
        "no_agent": True,
        "script": {"path": "cleanup.sh", "body": body},
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "delete-stale",
                    "effect": "delete",
                    "description": "Delete the stale row.",
                }
            ],
        },
    }

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_requires_shell_fail_fast_before_checkpoint():
    execution = {
        "no_agent": True,
        "script": {
            "path": "cleanup.sh",
            "body": (
                '"$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
                "--checkpoint delete-stale --effect delete\n"
                "set -e\n"
                "delete_stale_row 7\n"
            ),
        },
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "delete-stale",
                    "effect": "delete",
                    "description": "Delete the stale row.",
                }
            ],
        },
    }

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


@pytest.mark.parametrize(
    "body",
    [
        (
            "set -e\n"
            '"$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
            "--checkpoint delete-stale --effect delete || true\n"
            "delete_stale_row 7\n"
        ),
        (
            "set -e\n"
            "gate() {\n"
            '  "$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
            "--checkpoint delete-stale --effect delete\n"
            "}\n"
            "gate || true\n"
            "delete_stale_row 7\n"
        ),
        (
            "set -e\n"
            "if should_delete; then\n"
            '  "$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
            "--checkpoint delete-stale --effect delete\n"
            "fi\n"
            "delete_stale_row 7\n"
        ),
        (
            "set -e\n"
            "(\n"
            '  "$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
            "--checkpoint delete-stale --effect delete --resource row/7\n"
            ") || true\n"
            "delete_stale_row 7\n"
        ),
        (
            "set -e\n"
            "time while false; do\n"
            '  "$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
            "--checkpoint delete-stale --effect delete --resource row/7\n"
            "done\n"
            "delete_stale_row 7\n"
        ),
    ],
)
def test_compiler_rejects_shell_checkpoint_in_failure_suppressing_list(body):
    execution = {
        "no_agent": True,
        "script": {
            "path": "cleanup.sh",
            "body": body,
        },
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "delete-stale",
                    "effect": "delete",
                    "description": "Delete the stale row.",
                }
            ],
        },
    }

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_rejects_backtick_command_substitution():
    execution = _protected_script_execution(
        "cleanup.sh",
        (
            "set -e\n"
            "result=`\n"
            '  "$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
            "--checkpoint delete-stale --effect delete --resource row/7\n"
            "` || true\n"
            "delete_stale_row 7\n"
        ),
    )

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


@pytest.mark.parametrize(
    "helper_command",
    ["source ./mutable-helper.sh", ". ./mutable-helper.sh", "./mutable-helper.sh"],
)
def test_compiler_rejects_mutable_local_shell_helpers(helper_command):
    execution = _protected_script_execution(
        "cleanup.sh",
        (
            "set -e\n"
            f"{helper_command}\n"
            '"$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
            "--checkpoint delete-stale --effect delete --resource row/7\n"
            "delete_stale_row 7\n"
        ),
    )

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


@pytest.mark.parametrize(
    "mutation",
        [
            "HERMES_AUTHORIZATION_PYTHON=/usr/bin/true",
            "export HERMES_AUTHORIZATION_ENDPOINT=/tmp/fake.sock",
            "unset HERMES_AUTHORIZATION_TOKEN",
            "printf -v HERMES_AUTHORIZATION_PYTHON /usr/bin/true",
        ],
)
def test_compiler_rejects_shell_approval_bootstrap_rebinding(mutation):
    execution = _protected_script_execution(
        "cleanup.sh",
        (
            "set -e\n"
            f"{mutation}\n"
            '"$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
            "--checkpoint delete-stale --effect delete --resource row/7\n"
            "delete_stale_row 7\n"
        ),
    )

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


@pytest.mark.parametrize("fail_fast", ["set -e", "set -euo pipefail", "set -o errexit"])
def test_compiler_rejects_shell_inline_checkpoint_after_fail_fast(fail_fast):
    execution = {
        "no_agent": True,
        "script": {
            "path": "cleanup.sh",
            "body": (
                f"{fail_fast}\n"
                '"$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
                "--checkpoint delete-stale --effect delete --resource row/7\n"
                "delete_stale_row 7\n"
            ),
        },
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "delete-stale",
                    "effect": "delete",
                    "description": "Delete the stale row.",
                }
            ],
        },
    }

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_rejects_shell_effect_before_approval():
    execution = _protected_script_execution(
        "cleanup.sh",
        (
            "set -euo pipefail\n"
            "delete_stale_row 7\n"
            '"$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
            "--checkpoint delete-stale --effect delete --resource row/7\n"
        ),
    )

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_rejects_shell_effect_in_approval_argument():
    execution = _protected_script_execution(
        "cleanup.sh",
        (
            "set -euo pipefail\n"
            '"$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
            "--checkpoint delete-stale --effect delete "
            '--resource "$(delete_stale_row 7)"\n'
        ),
    )

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_rejects_shell_checkpoint_after_errexit_is_disabled():
    execution = {
        "no_agent": True,
        "script": {
            "path": "cleanup.sh",
            "body": (
                "set -e\n"
                "set +e\n"
                '"$HERMES_AUTHORIZATION_PYTHON" -I "$HERMES_AUTHORIZATION_RUNTIME" request '
                "--checkpoint delete-stale --effect delete\n"
                "delete_stale_row 7\n"
            ),
        },
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "delete-stale",
                    "effect": "delete",
                    "description": "Delete the stale row.",
                }
            ],
        },
    }

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_rejects_checkpoint_effect_mismatch():
    execution = {
        "no_agent": True,
        "script": {
            "path": "cleanup.py",
            "body": (
                "from hermes_crunch.approval_runtime import require_approval\n"
                "require_approval('delete-stale', 'update')\n"
            ),
        },
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "delete-stale",
                    "effect": "delete",
                    "description": "Delete the stale row.",
                }
            ],
        },
    }

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_compiler_rejects_undeclared_script_checkpoint():
    execution = {
        "no_agent": False,
        "script": {
            "path": "cleanup.py",
            "body": (
                "from hermes_crunch.approval_runtime import require_approval\n"
                "require_approval('publish-report', 'publish')\n"
            ),
        },
        "prompt": "Summarize the run.",
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "reviewed-report",
                    "effect": "publish",
                    "description": "Publish the reviewed report.",
                }
            ],
        },
    }

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(execution), available_toolsets={"file", "terminal"}
        )


def test_drafter_accepts_loaded_skill_and_read_only_probe(tmp_path, monkeypatch):
    recording = tmp_path / "recording"
    recording.mkdir()
    captured = {}
    execution = {"no_agent": False, "script": {"path": "collect.py", "body": "print('data')\n"}, "prompt": "Use the collected data.", "skills": ["example-skill"], "enabled_toolsets": ["terminal"], "requirements": _requirements()}
    messages = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "skill_view", "arguments": '{"name":"example-skill"}'}},
        {"function": {"name": "terminal", "arguments": '{"command":"command -v example"}'}},
    ]}]
    _runtime(monkeypatch, _plan(execution), captured, messages=messages)

    result = drafting.author_workflow(
        "Create the result.",
        {
            "steps": [{
                "action": "create",
                "browser_origin": "https://example.test",
            }]
        },
        recording_dir=recording,
        home=tmp_path,
    )
    assert result["execution"]["skills"] == ["example-skill"]
    assert result["llm_policy"] == "on_change"


def test_compact_evidence_preserves_semantics_without_secure_values():
    compact = drafting._compact_evidence({"steps": [
        {"action": "navigate", "app": "Browser", "browser_origin": "https://example.test", "browser_url": "https://example.test/items", "target": {"href": "https://example.test/items", "label": "Items"}},
        {"action": "type", "app": "Secrets", "target": {"value": "hidden", "secure_input": True}},
        {"action": "paste", "app": "Tasks", "target": {"value": "link", "ax": {"ancestor_path": [{"description": "Inbox list"}, {"identifier": "_NS:13"}]}}},
    ]})
    assert compact["steps"][0]["browser_url"] == "https://example.test/items"
    assert compact["steps"][0]["target"]["href"] == "https://example.test/items"
    assert "target" not in compact["steps"][1]
    assert compact["steps"][2]["containers"] == ["Inbox list"]
    assert compact["capability_hints"]["apps"] == ["Browser", "Secrets", "Tasks"]


def test_compact_evidence_indexes_v3_pack_without_injecting_raw_artifacts():
    compact = drafting._compact_evidence({
        "steps": [{"action": "click", "app": "Notes"}],
        "evidence_pack": {
            "schemaVersion": "hermes-crunch-evidence-v1",
            "summary": {"action_count": 81},
            "quality": {},
            "actions": [
                {"id": str(index), "action": "click", "target": {"label": "Done"}, "input": {"x": index}}
                for index in range(81)
            ],
            "active_surfaces": [{"payload": {"window": {"title": "private"}}}],
            "artifacts": [{"path": "raw/input.jsonl", "sha256": "not-for-prompt"}],
        },
    })

    assert "evidence_pack" not in compact
    assert compact["evidence_pack_path"] == "evidence-pack.json"
    assert len(compact["evidence_summary"]["actions"]) == 80
    assert compact["evidence_summary"]["action_count_truncated"] is True
    assert "active_surfaces" not in json.dumps(compact)
    assert "not-for-prompt" not in json.dumps(compact)


@pytest.mark.parametrize("field", ["prompt", "script"])
def test_compiler_rejects_runtime_recording_evidence_references(field):
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Complete the task.",
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
    }
    if field == "prompt":
        execution["prompt"] = "Open evidence-pack.json before executing."
    else:
        execution["script"] = {"path": "collect.py", "body": "open('raw/input.jsonl')\n"}

    with pytest.raises(compiler.CompileError, match="must not reference local recording evidence"):
        compiler.validate_draft_plan(_plan(execution), available_toolsets={"file"})


def test_compiler_rejects_current_native_recording_video():
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Replay visual/active.mp4.",
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
    }

    with pytest.raises(compiler.CompileError, match="must not reference local recording evidence"):
        compiler.validate_draft_plan(_plan(execution), available_toolsets={"file"})


def test_compiler_allows_legitimate_workflow_visual_directory():
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Publish the rendered file at visual/quarterly-summary.mp4.",
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
    }

    plan = compiler.validate_draft_plan(_plan(execution), available_toolsets={"file"})

    assert plan["execution"]["prompt"] == execution["prompt"]


@pytest.mark.parametrize("field", ["schedule", "profile", "workdir", "model", "generated_skills"])
def test_validator_rejects_cron_management_fields(field):
    execution = {"no_agent": False, "script": None, "prompt": "run", "skills": [], "enabled_toolsets": [], "requirements": _requirements()}
    with pytest.raises(compiler.CompileError, match="unsupported fields"):
        compiler.validate_draft_plan(_plan(execution, **{field: "x"}), available_toolsets={"file"})


def test_validator_rejects_unavailable_toolset():
    execution = {"no_agent": False, "script": None, "prompt": "run", "skills": [], "enabled_toolsets": ["browser"], "requirements": _requirements()}
    with pytest.raises(compiler.CompileError, match="unavailable"):
        compiler.validate_draft_plan(_plan(execution), available_toolsets={"file"})


@pytest.mark.parametrize("steps", [[], ["one"] * 7, ["valid", ""]])
def test_validator_requires_a_small_nonempty_review_outline(steps):
    execution = {"no_agent": False, "script": None, "prompt": "run", "skills": [], "enabled_toolsets": [], "requirements": _requirements()}
    with pytest.raises(compiler.CompileError, match="steps"):
        compiler.validate_draft_plan(_plan(execution, steps=steps), available_toolsets={"file"})


def test_validator_rejects_effects_only_authorization():
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Add the reminder, then send the email and message.",
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": ["create", "send"],
            "target": "origin",
            "checkpoints": [],
        },
    }

    with pytest.raises(
        compiler.CompileError,
        match="blanket gates are unsupported",
    ):
        compiler.validate_draft_plan(
            _plan(
                execution,
                steps=["Add the reminder.", "Send the email.", "Send the message."],
                approval_steps=[
                    {"step": 1, "effect": "create"},
                    {"step": 2, "effect": "send"},
                    {"step": 3, "effect": "send"},
                ],
            ),
            available_toolsets={"file"},
        )


def test_agent_operations_keep_same_effect_actions_independent():
    operations = [
        {
            "id": "create-video-reminder",
            "effect": "create",
            "description": "Create the video reminder.",
            "tool": "computer_use",
            "arguments": {
                "all": [
                    {"field": "action", "equals": "type"},
                    {"field": "text", "contains": "youtube.com/watch?v="},
                ]
            },
        },
        {
            "id": "create-example-note",
            "effect": "create",
            "description": "Create the Example Domain note.",
            "tool": "computer_use",
            "arguments": {
                "all": [
                    {"field": "action", "equals": "type"},
                    {"field": "text", "contains": "Example Domain"},
                ]
            },
        },
    ]
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Create the reminder, then create the note.",
        "skills": [],
        "enabled_toolsets": ["computer_use"],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "operations": operations,
            "target": "origin",
            "checkpoints": [],
        },
    }

    plan = compiler.validate_draft_plan(
        _plan(
            execution,
            steps=["Find the video.", "Create the reminder.", "Read the site.", "Create the note."],
            approval_steps=[
                {"step": 2, "operation_id": "create-video-reminder", "effect": "create"},
                {"step": 4, "operation_id": "create-example-note", "effect": "create"},
            ],
        ),
        available_toolsets={"computer_use"},
    )
    boundaries = compiler.effective_approval_boundaries({
        "execution": plan["execution"],
        "review": {
            "execution_steps": plan["steps"],
            "approval_steps": plan["approval_steps"],
        },
    })["boundaries"]

    assert [boundary["key"] for boundary in boundaries] == [
        "operation:create-video-reminder",
        "operation:create-example-note",
    ]
    assert [boundary["steps"] for boundary in boundaries] == [[2], [4]]
    assert [boundary["description"] for boundary in boundaries] == [
        "Create the video reminder.",
        "Create the Example Domain note.",
    ]


def test_agent_review_operation_id_requires_an_executable_operation():
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Create the reminder, then create the note.",
        "skills": [],
        "enabled_toolsets": ["computer_use"],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "target": "origin",
            "operations": [
                {
                    "id": "different-operation",
                    "effect": "create",
                    "description": "Create a different reviewed item.",
                    "tool": "computer_use",
                    "arguments": {"field": "action", "equals": "type"},
                }
            ],
            "checkpoints": [],
        },
    }

    with pytest.raises(
        compiler.CompileError,
        match="unknown authorization operation: create-video-reminder",
    ):
        compiler.validate_draft_plan(
            _plan(
                execution,
                steps=["Create the reminder.", "Create the note."],
                approval_steps=[
                    {
                        "step": 1,
                        "operation_id": "create-video-reminder",
                        "effect": "create",
                    },
                    {
                        "step": 2,
                        "operation_id": "create-example-note",
                        "effect": "create",
                    },
                ],
            ),
            available_toolsets={"computer_use"},
        )


def test_saved_cosmetic_operation_checks_require_redraft_without_breaking_review():
    workflow = {
        "execution": {
            "no_agent": False,
            "script": None,
            "authorization": {
                "mode": "human_in_loop",
                "effects": ["create"],
                "checkpoints": [],
            },
        },
        "review": {
            "execution_steps": ["Create the reminder.", "Create the note."],
            "approval_steps": [
                {
                    "step": 1,
                    "operation_id": "create-reminder",
                    "effect": "create",
                },
                {
                    "step": 2,
                    "operation_id": "create-note",
                    "effect": "create",
                },
            ],
        },
    }

    with pytest.raises(
        compiler.CompileError,
        match="blanket gates are unsupported",
    ):
        compiler.effective_approval_boundaries(workflow)

    assert compiler.review_blockers(workflow) == [{
        "kind": "authorization_invalid",
        "message": (
                "authorization.effects blanket gates are unsupported; use explicit "
                "authorization.operations or one exact authorization.script approval"
        ),
        "resolution": "redraft",
    }]
    assert compiler.crunch_install_guard(workflow)[
        "requires_review_resolution"
    ] is True


def test_authorization_selection_only_changes_operation_enabled_state():
    operation = {
        "id": "create-note",
        "effect": "create",
        "description": "Create the reviewed note.",
        "tool": "computer_use",
        "arguments": {
            "all": [
                {"field": "action", "equals": "type"},
                {"field": "text", "contains": "Example Domain"},
            ]
        },
    }
    execution = {
        "no_agent": False,
        "script": None,
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "operations": [operation],
            "checkpoints": [],
        },
    }

    selected = compiler.validate_authorization_selection(
        execution,
        {
            "mode": "human_in_loop",
            "effects": [],
            "operations": [{**operation, "enabled": False}],
            "checkpoints": [],
        },
    )

    assert selected["operations"][0]["enabled"] is False
    with pytest.raises(compiler.CompileError, match="cannot add, remove, or change"):
        compiler.validate_authorization_selection(
            execution,
            {
                "mode": "human_in_loop",
                "effects": [],
                "operations": [{**operation, "tool": "terminal"}],
                "checkpoints": [],
            },
        )


@pytest.mark.parametrize(
    "approval_steps, message",
    [
        (
            [{
                "step": 3,
                "operation_id": "send-result",
                "effect": "send",
            }],
            "between 1 and 2",
        ),
        (
            [{
                "step": 1,
                "operation_id": "send-result",
                "effect": "delete",
            }],
            "does not match authorization operation",
        ),
        (
            [{"step": 1, "effect": "send", "checkpoint_id": "missing"}],
            "checkpoint_id is unsupported",
        ),
    ],
)
def test_validator_rejects_invalid_review_approval_mapping(approval_steps, message):
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Send the result.",
        "skills": [],
        "enabled_toolsets": ["computer_use"],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "target": "origin",
            "operations": [
                {
                    "id": "send-result",
                    "effect": "send",
                    "description": "Send the reviewed result.",
                    "tool": "computer_use",
                    "arguments": {"field": "action", "equals": "type"},
                }
            ],
            "checkpoints": [],
        },
    }

    with pytest.raises(compiler.CompileError, match=message):
        compiler.validate_draft_plan(
            _plan(
                execution,
                steps=["Prepare the result.", "Send the result."],
                approval_steps=approval_steps,
            ),
            available_toolsets={"computer_use"},
        )


def test_compiler_emits_native_hybrid_payload(tmp_path):
    execution = compiler.validate_draft_plan(
        _plan({"no_agent": False, "script": {"path": "collect.py", "body": "print('data')\n"}, "prompt": "Judge.", "skills": [], "enabled_toolsets": ["file"], "requirements": _requirements()}),
        available_toolsets={"file"},
    )["execution"]
    result = compiler.compile_workflow(
        {"name": "Hybrid", "schedule": "every 5m", "deliver": "local", "execution": execution},
        tmp_path / "home",
        tmp_path / "build",
    )
    assert {
        key: value
        for key, value in result.payload.items()
        if key not in {"script", "authorization"}
    } == {
        "action": "create", "name": "Hybrid", "schedule": "every 5m", "deliver": "local",
        "prompt": "Judge.",
        "enabled_toolsets": ["file"], "no_agent": False,
    }
    assert result.payload["authorization"]["mode"] == "human_in_loop"
    assert result.payload["authorization"]["script"]["id"] == (
        "execute-reviewed-script"
    )
    assert result.payload["authorization"]["script_sha256"]
    assert result.payload["authorization"]["execution_sha256"]
    assert Path(result.payload["script"]).parts[0] == "crunch"
    assert Path(result.payload["script"]).name == "collect.py"


def test_effective_boundaries_keep_disabled_exact_script_approval():
    execution = {
        "no_agent": True,
        "script": {
            "path": "job.py",
            "body": "send_mail()\npublish_report()\n",
        },
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "operations": [],
            "checkpoints": [],
            "script": {
                "id": "execute-reviewed-script",
                "effect": "execute",
                "description": "Execute the exact reviewed script.",
            },
        },
    }
    plan = compiler.validate_draft_plan(
        _plan(
            execution,
            steps=["Send mail and publish the report."],
            approval_steps=[
                {
                    "step": 1,
                    "effect": "execute",
                    "script_id": "execute-reviewed-script",
                },
            ],
        ),
        available_toolsets={"file"},
    )
    selected_authorization = {
        **plan["execution"]["authorization"],
        "script": {
            **plan["execution"]["authorization"]["script"],
            "enabled": False,
        },
    }
    selected_authorization = compiler.validate_authorization_selection(
        plan["execution"],
        selected_authorization,
    )
    boundaries = compiler.effective_approval_boundaries({
        "execution": {
            **plan["execution"],
            "authorization": selected_authorization,
        },
        "review": {"approval_steps": plan["approval_steps"]},
    })["boundaries"]

    assert [item["key"] for item in boundaries] == [
        "script:execute-reviewed-script"
    ]
    assert boundaries[0]["enabled"] is False
    assert boundaries[0]["steps"] == [1]


def test_compiler_rejects_mutually_exclusive_script_checkpoints():
    execution = {
        "no_agent": True,
        "script": {
            "path": "upsert.py",
            "body": (
                "from hermes_crunch.approval_runtime import require_approval\n"
                "existing = find_existing_item()\n"
                "if existing:\n"
                "    require_approval('update-item', 'update', resource='item/1')\n"
                "else:\n"
                "    require_approval('create-item', 'create', resource='item/1')\n"
            ),
        },
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {
                    "id": "create-item",
                    "effect": "create",
                    "description": "Create the item.",
                },
                {
                    "id": "update-item",
                    "effect": "update",
                    "description": "Update the item.",
                },
            ],
        },
    }
    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(
                execution,
                steps=["Create or update the item."],
                approval_steps=[
                    {
                        "step": 1,
                        "operation_id": "upsert-item",
                        "effect": "create",
                        "checkpoint_id": "create-item",
                    },
                    {
                        "step": 1,
                        "operation_id": "upsert-item",
                        "effect": "update",
                        "checkpoint_id": "update-item",
                    },
                ],
            ),
            available_toolsets={"file"},
        )


def test_effective_boundaries_reject_conditional_script_checkpoints():
    workflow = {
        "execution": {
            "no_agent": True,
            "script": {
                "path": "route.py",
                "body": (
                    "from hermes_crunch.approval_runtime import require_approval\n"
                    "if use_primary_route:\n"
                    "    require_approval('primary-route', 'execute', resource='primary')\n"
                    "else:\n"
                    "    require_approval('fallback-route', 'execute', resource='fallback')\n"
                ),
            },
            "authorization": {
                "mode": "human_in_loop",
                "effects": [],
                "checkpoints": [
                    {
                        "id": "primary-route",
                        "effect": "execute",
                        "description": "Use the primary route.",
                    },
                    {
                        "id": "fallback-route",
                        "effect": "execute",
                        "description": "Use the fallback route.",
                    },
                ],
            },
        },
        "review": {
            "approval_steps": [
                {
                    "step": 1,
                    "operation_id": "route-action",
                    "effect": "execute",
                    "checkpoint_id": "primary-route",
                },
                {
                    "step": 1,
                    "operation_id": "route-action",
                    "effect": "execute",
                    "checkpoint_id": "fallback-route",
                },
            ],
        },
    }

    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.effective_approval_boundaries(workflow)


def test_effective_boundaries_return_one_exact_script_approval():
    execution = {
        "no_agent": True,
        "script": {
            "path": "publish.py",
            "body": "create_report()\npublish_report()\n",
        },
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "operations": [],
            "checkpoints": [],
            "script": {
                "id": "execute-reviewed-script",
                "effect": "execute",
                "description": "Execute the exact reviewed script.",
            },
        },
    }
    plan = compiler.validate_draft_plan(
        _plan(
            execution,
            approval_steps=[
                {
                    "step": 1,
                    "script_id": "execute-reviewed-script",
                    "effect": "execute",
                },
            ],
        ),
        available_toolsets={"file"},
    )

    boundaries = compiler.effective_approval_boundaries({
        "execution": plan["execution"],
        "review": {"approval_steps": plan["approval_steps"]},
    })["boundaries"]

    assert [item["key"] for item in boundaries] == [
        "script:execute-reviewed-script",
    ]


def test_compiler_rejects_multi_effect_branch_script_checkpoints():
    execution = {
        "no_agent": True,
        "script": {
            "path": "route.py",
            "body": (
                "from hermes_crunch.approval_runtime import require_approval\n"
                "if existing:\n"
                "    require_approval('update-item', 'update', resource='item/1')\n"
                "    require_approval('send-update', 'send', resource='item/1')\n"
                "else:\n"
                "    require_approval('create-item', 'create', resource='item/1')\n"
                "    require_approval('send-create', 'send', resource='item/1')\n"
            ),
        },
        "prompt": None,
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
            "checkpoints": [
                {"id": "update-item", "effect": "update", "description": "Update item."},
                {"id": "send-update", "effect": "send", "description": "Send update."},
                {"id": "create-item", "effect": "create", "description": "Create item."},
                {"id": "send-create", "effect": "send", "description": "Send creation."},
            ],
        },
    }
    approval_steps = [
        {"step": 1, "effect": checkpoint["effect"], "checkpoint_id": checkpoint["id"]}
        for checkpoint in execution["authorization"]["checkpoints"]
    ]
    with pytest.raises(compiler.CompileError, match="Inline script checkpoints"):
        compiler.validate_draft_plan(
            _plan(
                execution,
                steps=["Create or update the item, then send the result."],
                approval_steps=approval_steps,
            ),
            available_toolsets={"file"},
        )


def test_validator_requires_placement_for_each_hybrid_runtime_boundary():
    execution = {
        "no_agent": False,
            "script": {
                "path": "send.py",
                "body": "publish_result()\n",
        },
        "prompt": "Send the result after the script publishes it.",
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
        "authorization": {
            "mode": "human_in_loop",
            "effects": [],
                "operations": [],
                "checkpoints": [],
                "script": {
                    "id": "execute-reviewed-script",
                    "effect": "execute",
                    "description": "Execute the exact reviewed script.",
                },
        },
    }

    with pytest.raises(
        compiler.CompileError,
        match=r"script:execute-reviewed-script",
    ):
        compiler.validate_draft_plan(
            _plan(
                execution,
                approval_steps=[],
            ),
            available_toolsets={"file"},
        )


def test_validator_preserves_step_local_capture_quality_verification():
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Inspect the result.",
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
    }
    warning = {
        "kind": "capture_quality_gap",
        "severity": "info",
        "message": "The final confirmation was not visible.",
        "requires_explicit_approval": False,
        "step": 1,
        "verify": "Confirm the item appears once in the destination list.",
    }

    plan = compiler.validate_draft_plan(
        _plan(execution, warnings=[warning]),
        available_toolsets={"file"},
    )

    assert plan["warnings"] == [warning]
    assert compiler.validate_draft_plan(
        _plan(execution, warnings=[{**warning, "verify": None}]),
        available_toolsets={"file"},
    )["warnings"] == []
    assert compiler.validate_draft_plan(
        _plan(execution, warnings=[{**warning, "step": 2}]),
        available_toolsets={"file"},
    )["warnings"] == []


def test_validator_ignores_capture_placement_fields_on_execution_warning():
    execution = {
        "no_agent": False,
        "script": None,
        "prompt": "Inspect the result and stop if the runtime account differs.",
        "skills": [],
        "enabled_toolsets": [],
        "requirements": _requirements(),
    }

    plan = compiler.validate_draft_plan(
        _plan(
            execution,
            warnings=[{
                "kind": "runtime_account_check",
                "severity": "info",
                "message": "The runtime account must match the intended account.",
                "requires_explicit_approval": False,
                "step": 1,
                "verify": "Stop without changes if the account differs.",
            }],
        ),
        available_toolsets={"file"},
    )

    assert plan["warnings"] == [{
        "kind": "runtime_account_check",
        "severity": "info",
        "message": "The runtime account must match the intended account.",
        "requires_explicit_approval": False,
    }]


def test_capture_diagnostics_do_not_become_review_blockers():
    workflow = {
        "review": {
            "warnings": [{
                "kind": "capture_quality_gap",
                "severity": "info",
                "message": "The final confirmation was not visible.",
                "requires_explicit_approval": False,
                "step": 1,
                "verify": "Confirm the item exists once.",
            }],
            "recording_warnings": [{
                "kind": "missing_replay_data",
                "message": "Internal capture correlation was incomplete.",
                "requires_explicit_approval": True,
            }],
        }
    }

    assert compiler.review_blockers(workflow) == []
    workflow["review"]["warnings"].append({
        "kind": "unsafe_destination",
        "severity": "blocking",
        "message": "The destination is not identified.",
        "requires_explicit_approval": True,
    })
    assert compiler.review_blockers(workflow) == [{
        "kind": "unsafe_destination",
        "message": "The destination is not identified.",
        "resolution": "redraft",
    }]


def test_only_capture_integrity_failures_require_rerecording():
    workflow = {
        "review": {
            "capture_acceptance": {
                "schemaVersion": "hermes-crunch-capture-acceptance-v1",
                "capture_manifest_sha256": "a" * 64,
                "blockers": [{
                    "code": "missing_success_evidence",
                    "message": "No post-save receipt was correlated.",
                    "resolution": "rerecord",
                }],
                "summary": {
                    "action_count": 4,
                    "target_evidence_count": 2,
                    "mutation_evidence_count": 2,
                    "outcome_evidence_count": 0,
                },
            },
            "warnings": [],
        },
    }

    # Historical semantic entries remain readable, but a compiler-valid plan
    # must not send the user back through the demonstration loop.
    assert compiler.review_blockers(workflow) == []

    workflow["review"]["capture_acceptance"] = {
        "schemaVersion": "hermes-crunch-capture-acceptance-v1",
        "blockers": [{
            "code": "capture_acceptance_invalid",
            "message": "Hermes could not verify the saved recording.",
            "resolution": "rerecord",
        }],
    }
    assert compiler.review_blockers(workflow) == [{
        "kind": "capture_acceptance_invalid",
        "message": "Hermes could not verify the saved recording.",
        "resolution": "rerecord",
    }]
