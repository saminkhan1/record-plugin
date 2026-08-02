from __future__ import annotations

import importlib
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_api():
    package_name = f"record_plugin_api_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        package_name,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("Could not load Record plugin API")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return importlib.import_module(f"{package_name}.dashboard.plugin_api")


def _authorization_request(api):
    return api.RecordDesktopCaptureAuthorizationRequest(
        profile="default",
        name="Customer workflow",
        instructions="Open the customer portal and export the report.",
        schedule="0 9 * * 1-5",
        run_mode="schedule",
    )


def _start_request(api, authorization):
    request = _authorization_request(api).model_dump()
    return api.RecordDesktopRecordingStartRequest(
        **request,
        capture_authorization_nonce=authorization["capture_authorization_nonce"],
        capture_authorization=authorization,
    )


def test_desktop_capture_authorization_is_bound_and_single_use(
    tmp_path,
    monkeypatch,
):
    api = _load_plugin_api()
    api._DESKTOP_CAPTURE_AUTHORIZATIONS.clear()
    monkeypatch.setattr(api, "_cron_profile_home", lambda _profile: ("default", tmp_path))
    starts = []
    monkeypatch.setattr(
        api,
        "start_native_recording",
        lambda **kwargs: starts.append(kwargs) or {"status": "recording"},
    )

    authorization = api.provision_desktop_capture_authorization(
        _authorization_request(api),
    )
    assert authorization["source"] == "desktop"
    assert authorization["profile"] == "default"

    result = api.start_desktop_recording(_start_request(api, authorization))
    assert result == {"status": "recording"}
    assert starts[0]["capture_authorization_nonce"] == authorization["capture_authorization_nonce"]

    with pytest.raises(HTTPException, match="already used"):
        api.start_desktop_recording(_start_request(api, authorization))


def test_desktop_capture_authorization_rejects_changed_recording(
    tmp_path,
    monkeypatch,
):
    api = _load_plugin_api()
    api._DESKTOP_CAPTURE_AUTHORIZATIONS.clear()
    monkeypatch.setattr(api, "_cron_profile_home", lambda _profile: ("default", tmp_path))
    monkeypatch.setattr(
        api,
        "start_native_recording",
        lambda **_kwargs: pytest.fail("mismatched authorization started recording"),
    )

    authorization = api.provision_desktop_capture_authorization(
        _authorization_request(api),
    )
    changed = _start_request(api, authorization).model_copy(
        update={"name": "A different workflow"},
    )
    with pytest.raises(HTTPException, match="does not match"):
        api.start_desktop_recording(changed)
