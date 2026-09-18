import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from aispace.codex import CodexConnection, CodexProvider
from aispace.provider import ProviderError

FAKE = r"""
import asyncio
import json
import os
import signal
import subprocess
import sys

mode = os.environ.get("FAKE_CODEX_MODE", "complete")
log_path = os.environ["FAKE_CODEX_LOG"]
goals = {}
active = {}
children = []
turn_number = 0

def audit(kind, **data):
    with open(log_path, "a") as stream:
        stream.write(json.dumps({"kind": kind, "pid": os.getpid(), **data}) + "\n")

def emit(message):
    print(json.dumps(message), flush=True)

def notify(method, **params):
    emit({"method": method, "params": params})

async def run_turn(thread_id, turn_id, prompt, auto=False):
    await asyncio.sleep(0.03)
    if mode == "approval":
        emit({"id": "approval", "method": "item/commandExecution/requestApproval", "params": {"threadId": thread_id}})
    if mode == "child":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        children.append(child)
        audit("child", child_pid=child.pid)
    text = "continued" if auto else "answer"
    item_id = turn_id + "-item"
    notify("item/agentMessage/delta", threadId=thread_id, turnId=turn_id, itemId=item_id, delta=text)
    if mode in {"hold", "lost_ack", "ignore_interrupt", "child", "approval"}:
        await active[turn_id].wait()
        notify("turn/completed", threadId=thread_id, turn={"id": turn_id, "status": "interrupted"})
        audit("interrupted", turn_id=turn_id)
        return
    await asyncio.sleep(0.03)
    notify("item/completed", threadId=thread_id, turnId=turn_id, item={"id": item_id, "type": "agentMessage", "text": text})
    if mode == "goal" and auto:
        goals[thread_id]["status"] = "complete"
        notify("thread/goal/updated", threadId=thread_id, goal=goals[thread_id])
    notify("turn/completed", threadId=thread_id, turn={"id": turn_id, "status": "completed"})
    audit("completed", turn_id=turn_id)
    if mode == "goal" and not auto:
        await asyncio.sleep(0.03)
        if goals.get(thread_id, {}).get("status") == "active":
            await start_turn(thread_id, "auto", auto=True)

async def start_turn(thread_id, prompt, auto=False):
    global turn_number
    turn_number += 1
    turn_id = f"turn-{os.getpid()}-{turn_number}"
    active[turn_id] = asyncio.Event()
    notify("turn/started", threadId=thread_id, turn={"id": turn_id, "status": "inProgress"})
    audit("turn", thread_id=thread_id, turn_id=turn_id, prompt=prompt, auto=auto)
    asyncio.create_task(run_turn(thread_id, turn_id, prompt, auto))
    return {"turn": {"id": turn_id, "status": "inProgress"}}

async def main():
    audit("boot")
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            return
        message = json.loads(line)
        if "method" not in message:
            audit("client_response", message=message)
            continue
        method = message["method"]
        params = message.get("params", {})
        audit("request", method=method, params=params)
        if "id" not in message:
            continue
        result = {}
        if method == "initialize":
            if mode == "startup":
                await asyncio.sleep(60)
        elif method == "account/read":
            result = {"account": {"type": "apiKey" if mode == "api_key" else "chatgpt", "email": "test@example.com", "planType": "pro", "accessToken": "secret-never-show"}}
        elif method == "account/login/start":
            result = {"loginId": "login", "verificationUrl": "https://auth.openai.com/codex/device", "userCode": "TEST-CODE", "accessToken": "secret-never-show"}
        elif method in {"thread/start", "thread/resume"}:
            result = {"thread": {"id": params.get("threadId", f"thread-{os.getpid()}")}}
        elif method == "thread/goal/get":
            result = {"goal": goals.get(params["threadId"])}
        elif method == "thread/goal/set":
            thread_id = params["threadId"]
            goal = goals.setdefault(thread_id, {"objective": "existing", "status": "paused"})
            goal.update({key: value for key, value in params.items() if key != "threadId"})
            result = {"goal": goal}
            notify("thread/goal/updated", threadId=thread_id, goal=goal)
        elif method == "thread/goal/clear":
            goals.pop(params["threadId"], None)
        elif method == "turn/start":
            result = await start_turn(params["threadId"], params["input"][0]["text"])
            if mode == "turn_start":
                await asyncio.sleep(60)
        elif method == "turn/interrupt":
            if mode != "ignore_interrupt":
                active[params["turnId"]].set()
            if mode in {"lost_ack", "ignore_interrupt"}:
                continue
        elif method == "thread/backgroundTerminals/clean":
            for child in children:
                child.terminate()
                child.wait()
            audit("cleaned")
        emit({"id": message["id"], "result": result})

if mode == "ignore_interrupt":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
asyncio.run(main())
"""


class MemoryStore:
    def __init__(self):
        self.sessions = {}

    def codex_session(self, tasklet_id):
        return self.sessions.get(tasklet_id)

    def save_codex_session(self, tasklet_id, thread_id, cwd):
        self.sessions[tasklet_id] = {"thread_id": thread_id, "cwd": cwd}


class Directories:
    def resolve(self, path):
        return Path(path).resolve(strict=True)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    script = tmp_path / "fake.py"
    script.write_text(FAKE)
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AISPACE_CODEX_COMMAND_JSON", json.dumps([sys.executable, str(script)]))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setattr(CodexConnection, "stop_timeout", 0.15)
    monkeypatch.setattr(CodexConnection, "process_timeout", 0.15)
    provider = CodexProvider(MemoryStore(), Directories())
    settings = {"working_directory": str(tmp_path), "codex_sandbox": "read-only", "model": ""}

    def events():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    return provider, settings, events


async def collect(provider, settings, task_id="task", content="hello", history=None):
    return "".join(
        [
            chunk
            async for chunk in provider.stream(
                settings,
                settings.get("model", ""),
                [*(history or []), {"role": "user", "content": content}],
                {"id": task_id},
            )
        ]
    )


async def wait_event(events, kind, count=1):
    async with asyncio.timeout(5):
        while True:
            found = [event for event in events() if event["kind"] == kind]
            if len(found) >= count:
                return found
            await asyncio.sleep(0.01)


def assert_dead(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_native_thread_context_persistence_and_no_duplicate_output(harness):
    provider, settings, events = harness
    history = [{"role": "assistant", "content": "earlier result"}]
    assert await collect(provider, settings, history=history) == "answer"
    session = provider.store.sessions["task"]
    assert await collect(provider, settings, content="follow up", history=history) == "answer"
    requests = [event for event in events() if event["kind"] == "request"]
    start = next(event["params"] for event in requests if event["method"] == "thread/start")
    resume = next(event["params"] for event in requests if event["method"] == "thread/resume")
    assert start["cwd"] == settings["working_directory"]
    assert start["sandbox"] == "read-only"
    assert start["approvalPolicy"] == "never"
    assert resume["threadId"] == session["thread_id"]
    turns = [event for event in events() if event["kind"] == "turn"]
    assert "earlier result" in turns[0]["prompt"]
    assert turns[1]["prompt"] == "follow up"
    for event in events():
        if event["kind"] == "boot":
            assert_dead(event["pid"])


async def test_status_and_device_login_never_expose_tokens(harness):
    provider, _, events = harness
    try:
        status = await provider.status()
        assert status["authenticated"] and status["auth_type"] == "chatgpt"
        login = await provider.login()
        assert login == {
            "login_id": "login",
            "auth_url": "https://auth.openai.com/codex/device",
            "user_code": "TEST-CODE",
        }
        assert "secret-never-show" not in json.dumps([status, login])
        await provider.cancel_login("login")
        await provider.logout()
        methods = [event.get("method") for event in events()]
        assert "account/login/cancel" in methods
        assert "account/logout" in methods
        assert "turn/start" not in methods
    finally:
        await provider.close()


async def test_api_key_login_is_not_used_as_subscription(harness, monkeypatch):
    provider, settings, events = harness
    monkeypatch.setenv("FAKE_CODEX_MODE", "api_key")
    try:
        assert (await provider.status())["authenticated"] is False
        with pytest.raises(ProviderError, match="Войдите через ChatGPT"):
            await collect(provider, settings)
        assert not [event for event in events() if event["kind"] == "turn"]
    finally:
        await provider.close()


@pytest.mark.parametrize("mode", ["hold", "lost_ack", "ignore_interrupt"])
async def test_stop_confirms_interrupt_or_kills_unresponsive_process(harness, monkeypatch, mode):
    provider, settings, events = harness
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    task = asyncio.create_task(collect(provider, settings))
    await wait_event(events, "turn")
    pid = next(event["pid"] for event in events() if event["kind"] == "boot")
    before = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - before < 2
    assert_dead(pid)
    assert not provider.running
    methods = [event.get("method") for event in events()]
    assert "turn/interrupt" in methods
    if mode != "ignore_interrupt":
        assert any(event["kind"] == "interrupted" for event in events())
        assert "thread/backgroundTerminals/clean" in methods


async def test_stop_during_initialize_leaves_no_process(harness, monkeypatch):
    provider, settings, events = harness
    monkeypatch.setenv("FAKE_CODEX_MODE", "startup")
    task = asyncio.create_task(collect(provider, settings))
    boot = (await wait_event(events, "boot"))[0]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert_dead(boot["pid"])
    assert not provider.running


async def test_stop_before_turn_start_response_keeps_session_and_kills_process(
    harness, monkeypatch
):
    provider, settings, events = harness
    monkeypatch.setenv("FAKE_CODEX_MODE", "turn_start")
    task = asyncio.create_task(collect(provider, settings))
    turn = (await wait_event(events, "turn"))[0]
    assert provider.store.sessions["task"]["thread_id"] == turn["thread_id"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert_dead(turn["pid"])
    assert not provider.running


async def test_concurrent_streams_stop_independently(harness, monkeypatch):
    provider, settings, events = harness
    monkeypatch.setenv("FAKE_CODEX_MODE", "hold")
    first = asyncio.create_task(collect(provider, settings, "first"))
    second = asyncio.create_task(collect(provider, settings, "second"))
    await wait_event(events, "turn", 2)
    second_connection = provider.running["second"][0]
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done()
    assert second_connection.process.returncode is None
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    for boot in [event for event in events() if event["kind"] == "boot"]:
        assert_dead(boot["pid"])


async def test_stop_cleans_background_command(harness, monkeypatch):
    provider, settings, events = harness
    monkeypatch.setenv("FAKE_CODEX_MODE", "child")
    task = asyncio.create_task(collect(provider, settings))
    child = (await wait_event(events, "child"))[0]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_dead(child["child_pid"])
    kinds = [event["kind"] for event in events()]
    assert kinds.index("interrupted") < kinds.index("cleaned")


async def test_goals_use_native_state_and_wait_for_automatic_continuation(harness, monkeypatch):
    provider, settings, events = harness
    monkeypatch.setenv("FAKE_CODEX_MODE", "goal")
    output = await asyncio.wait_for(collect(provider, settings, content="/goal Finish"), 5)
    assert output == "answer\n\ncontinued"
    goal_requests = [
        event["params"] for event in events() if event.get("method") == "thread/goal/set"
    ]
    assert goal_requests[0]["objective"] == "Finish"
    assert goal_requests[0]["status"] == "paused"
    assert goal_requests[1]["status"] == "active"
    turns = [event for event in events() if event["kind"] == "turn"]
    assert len(turns) == 2 and turns[1]["auto"]
    assert turns[0]["prompt"] == "Finish"


async def test_goal_stop_pauses_before_interrupt(harness, monkeypatch):
    provider, settings, events = harness
    monkeypatch.setenv("FAKE_CODEX_MODE", "hold")
    task = asyncio.create_task(collect(provider, settings, content="/goal Keep working"))
    async with asyncio.timeout(5):
        while not any(
            event.get("method") == "thread/goal/set" and event["params"].get("status") == "active"
            for event in events()
        ):
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    requests = [event for event in events() if event["kind"] == "request"]
    paused = max(
        index
        for index, event in enumerate(requests)
        if event["method"] == "thread/goal/set" and event["params"].get("status") == "paused"
    )
    interrupted = next(
        index for index, event in enumerate(requests) if event["method"] == "turn/interrupt"
    )
    assert paused < interrupted


async def test_unsupported_commands_are_not_sent_to_model(harness):
    provider, settings, events = harness
    with pytest.raises(ProviderError, match="не поддерживается"):
        await collect(provider, settings, content="/unrecognized do something")
    assert not [event for event in events() if event["kind"] == "turn"]
    assert "нет активной цели" in await collect(provider, settings, content="/goal")
    assert not [event for event in events() if event["kind"] == "turn"]


async def test_approval_requests_are_declined(harness, monkeypatch):
    provider, settings, events = harness
    monkeypatch.setenv("FAKE_CODEX_MODE", "approval")
    task = asyncio.create_task(collect(provider, settings))
    response = (await wait_event(events, "client_response"))[0]
    assert response["message"]["result"] == {"decision": "decline"}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_missing_working_directory_never_starts_turn(harness):
    provider, settings, events = harness
    settings["working_directory"] = ""
    with pytest.raises(ProviderError, match="директорию"):
        await collect(provider, settings)
    assert not [event for event in events() if event["kind"] == "turn"]


async def test_directory_change_starts_fresh_file_context(harness, tmp_path):
    provider, settings, events = harness
    await collect(provider, settings)
    original = provider.store.sessions["task"]["thread_id"]
    new_directory = tmp_path / "another-project"
    new_directory.mkdir()
    settings["working_directory"] = str(new_directory)
    await collect(
        provider,
        settings,
        content="new project",
        history=[{"role": "user", "content": "private previous project context"}],
    )
    assert provider.store.sessions["task"]["thread_id"] != original
    turns = [event for event in events() if event["kind"] == "turn"]
    assert turns[-1]["prompt"] == "new project"


async def test_custom_codex_home_is_created_private(harness, tmp_path, monkeypatch):
    provider, settings, _ = harness
    custom_home = tmp_path / "private-codex"
    monkeypatch.setenv("AISPACE_CODEX_HOME", str(custom_home))
    await collect(provider, settings)
    assert custom_home.is_dir()
    assert custom_home.stat().st_mode & 0o777 == 0o700
