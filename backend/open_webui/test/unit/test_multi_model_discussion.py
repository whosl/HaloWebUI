import asyncio
import pathlib
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


_BACKEND_DIR = pathlib.Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from open_webui.utils import multi_model_discussion as discussion_mod  # noqa: E402


def _request(models):
    return SimpleNamespace(
        state=SimpleNamespace(MODELS=models, MODELS_AMBIGUOUS=set()),
        app=SimpleNamespace(state=SimpleNamespace(MODELS=models)),
    )


def _user(role="admin"):
    return SimpleNamespace(id="user-1", role=role)


def _models(*ids):
    return {model_id: {"id": model_id, "name": model_id} for model_id in ids}


def test_normalize_discussion_config_checks_access_and_clamps_rounds(monkeypatch):
    access_checks = []
    monkeypatch.setattr(discussion_mod, "BYPASS_MODEL_ACCESS_CONTROL", False)
    monkeypatch.setattr(
        discussion_mod,
        "check_model_access",
        lambda user, model: access_checks.append((user.id, model["id"])),
    )

    config = discussion_mod.normalize_discussion_config(
        {
            "participants": ["model-a", "model-b", "model-a"],
            "rounds": 99,
            "finalModel": "model-c",
        },
        _models("model-a", "model-b", "model-c"),
        set(),
        "model-c",
        _user("user"),
    )

    assert [item["request_id"] for item in config["participants"]] == [
        "model-a",
        "model-b",
    ]
    assert config["rounds"] == discussion_mod.MAX_DISCUSSION_ROUNDS
    assert access_checks == [
        ("user-1", "model-a"),
        ("user-1", "model-b"),
        ("user-1", "model-c"),
    ]


def test_normalize_discussion_config_rejects_too_many_models():
    with pytest.raises(HTTPException) as exc_info:
        discussion_mod.normalize_discussion_config(
            {"participants": ["a", "b", "c", "d", "e", "f"], "finalModel": "a"},
            _models("a", "b", "c", "d", "e", "f"),
            set(),
            "a",
            _user(),
        )

    assert exc_info.value.status_code == 400
    assert "at most" in exc_info.value.detail


def test_multi_model_discussion_orchestrates_models_and_events(monkeypatch):
    events = []
    upserts = []
    calls = []
    scheduled = {}

    async def event_emitter(event):
        events.append(event)

    async def fake_generate_chat_completion(_request, payload, _user):
        calls.append(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": f"answer from {payload['model']}",
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }

    def fake_create_task(coroutine, id=None):
        task = asyncio.create_task(coroutine)
        scheduled["task"] = task
        scheduled["chat_id"] = id
        return "task-1", task

    monkeypatch.setattr(discussion_mod, "get_event_emitter", lambda _metadata: event_emitter)
    monkeypatch.setattr(
        discussion_mod,
        "generate_chat_completion",
        fake_generate_chat_completion,
    )
    monkeypatch.setattr(discussion_mod, "create_task", fake_create_task)
    monkeypatch.setattr(
        discussion_mod.Chats,
        "upsert_message_to_chat_by_id_and_message_id",
        lambda *args, **kwargs: upserts.append((args, kwargs)),
    )

    async def run():
        result = await discussion_mod.generate_multi_model_discussion_completion(
            _request(_models("model-a", "model-b", "model-final")),
            {
                "model": "model-final",
                "messages": [{"role": "user", "content": "question"}],
                "stream": True,
            },
            _user(),
            {"chat_id": "chat-1", "message_id": "assistant-1"},
            {"id": "model-final"},
            {
                "enabled": True,
                "participants": ["model-a", "model-b"],
                "rounds": 1,
                "finalModel": "model-final",
            },
        )
        await scheduled["task"]
        return result

    result = asyncio.run(run())

    assert result == {"status": True, "task_id": "task-1"}
    assert scheduled["chat_id"] == "chat-1"
    assert [payload["model"] for payload in calls] == [
        "model-a",
        "model-b",
        "model-final",
    ]
    assert all(payload["stream"] is False for payload in calls)
    assert all("discussion" not in payload for payload in calls)

    discussion_events = [event for event in events if event["type"] == "discussion"]
    event_types = [event["data"]["type"] for event in discussion_events]
    assert "turn_done" in event_types
    assert "final_start" in event_types
    assert "done" in event_types

    final_start = next(
        event for event in discussion_events if event["data"]["type"] == "final_start"
    )
    assert final_start["data"]["state"]["status"] == "summarizing"

    turn_done = next(
        event for event in discussion_events if event["data"]["type"] == "turn_done"
    )
    assert turn_done["data"]["usage"]["total_tokens"] == 15
    assert turn_done["data"]["state"]["rounds"][0]["turns"][0]["usage"]["total_tokens"] == 15

    completion = next(event for event in events if event["type"] == "chat:completion")
    assert completion["data"]["content"] == "answer from model-final"
    assert completion["data"]["discussion"]["status"] == "completed"
    assert completion["data"]["usage"]["prompt_tokens"] == 30
    assert completion["data"]["usage"]["completion_tokens"] == 15
    assert completion["data"]["usage"]["total_tokens"] == 45
    assert len(completion["data"]["usage"]["per_model"]) == 3

    final_upsert = upserts[-1][0][2]
    assert final_upsert["content"] == "answer from model-final"
    assert final_upsert["usage"]["total_tokens"] == 45
    assert final_upsert["discussion"]["status"] == "completed"


def test_multi_model_discussion_cancellation_marks_message_stopped(monkeypatch):
    events = []
    upserts = []
    scheduled = {}

    async def event_emitter(event):
        events.append(event)

    async def never_finish(*_args, **_kwargs):
        await asyncio.Event().wait()

    def fake_create_task(coroutine, id=None):
        task = asyncio.create_task(coroutine)
        scheduled["task"] = task
        scheduled["chat_id"] = id
        return "task-1", task

    monkeypatch.setattr(discussion_mod, "get_event_emitter", lambda _metadata: event_emitter)
    monkeypatch.setattr(discussion_mod, "_run_model_once", never_finish)
    monkeypatch.setattr(discussion_mod, "create_task", fake_create_task)
    monkeypatch.setattr(
        discussion_mod.Chats,
        "upsert_message_to_chat_by_id_and_message_id",
        lambda *args, **kwargs: upserts.append((args, kwargs)),
    )

    async def run():
        await discussion_mod.generate_multi_model_discussion_completion(
            _request(_models("model-a", "model-b", "model-final")),
            {
                "model": "model-final",
                "messages": [{"role": "user", "content": "question"}],
                "stream": True,
            },
            _user(),
            {"chat_id": "chat-1", "message_id": "assistant-1"},
            {"id": "model-final"},
            {
                "enabled": True,
                "participants": ["model-a", "model-b"],
                "rounds": 1,
                "finalModel": "model-final",
            },
        )
        await asyncio.sleep(0)
        scheduled["task"].cancel()
        with pytest.raises(asyncio.CancelledError):
            await scheduled["task"]

    asyncio.run(run())

    stopped_events = [
        event
        for event in events
        if event["type"] == "discussion" and event["data"]["type"] == "stopped"
    ]
    assert stopped_events
    assert stopped_events[-1]["data"]["state"]["status"] == "stopped"

    stopped_upsert = upserts[-1][0][2]
    assert stopped_upsert["done"] is True
    assert stopped_upsert["stoppedByUser"] is True
    assert stopped_upsert["discussion"]["status"] == "stopped"
