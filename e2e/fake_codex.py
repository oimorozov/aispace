import json
import os
import re
import signal
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


class FakeCodex:
    def __init__(self):
        self.process_id = uuid.uuid4().hex
        self.output_lock = threading.Lock()
        self.audit_lock = threading.Lock()
        self.turns = {}
        self.threads = {}
        self.goals = {}
        self.workers = []
        self.state = Path(os.environ.get("AISPACE_DATA_DIR", "/tmp")) / "fake-codex"
        self.state.mkdir(parents=True, exist_ok=True)

    def audit(self, kind, **values):
        destination = os.environ.get("AISPACE_FAKE_CODEX_AUDIT_URL")
        payload = {
            "transport": "codex",
            "kind": kind,
            "process_id": self.process_id,
            "time": time.monotonic(),
            **values,
        }
        local = os.environ.get("AISPACE_FAKE_CODEX_AUDIT_FILE")
        if local:
            with self.audit_lock, Path(local).open("a") as output:
                output.write(json.dumps(payload) + "\n")
        if not destination:
            return
        request = Request(
            destination,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.audit_lock, urlopen(request, timeout=3) as response:
            response.read()

    def emit(self, payload):
        with self.output_lock:
            print(json.dumps(payload), flush=True)

    def notify(self, method, params):
        self.emit({"method": method, "params": params})

    def result(self, message, value):
        self.emit({"id": message["id"], "result": value})

    def thread(self, params):
        ephemeral = params.get("ephemeral") is True
        if ephemeral:
            self.validate_planner(params)
        thread_id = params.get("threadId") or uuid.uuid4().hex
        path = self.state / f"{thread_id}.json"
        previous = (
            json.loads(path.read_text()) if not ephemeral and path.exists() else {}
        )
        current = {**previous, **params, "id": thread_id}
        current.pop("threadId", None)
        self.threads[thread_id] = current
        goal_path = self.state / f"{thread_id}.goal.json"
        if not ephemeral and goal_path.exists():
            self.goals[thread_id] = json.loads(goal_path.read_text())
        if not ephemeral:
            path.write_text(json.dumps(current))
        self.audit(
            "planning_thread" if ephemeral else "thread",
            thread_id=thread_id,
            parameters=current,
        )
        return {"thread": current}

    def validate_planner(self, params):
        required = {
            "shell_tool",
            "unified_exec",
            "shell_snapshot",
            "code_mode",
            "code_mode_host",
            "multi_agent",
            "multi_agent_v2",
            "goals",
            "apps",
            "plugins",
            "remote_plugin",
            "browser_use",
            "browser_use_external",
            "computer_use",
            "image_generation",
            "view_image",
            "hooks",
            "memories",
            "skill_search",
            "skill_mcp_dependency_install",
            "workspace_dependencies",
            "in_app_local_automation",
            "request_permissions_tool",
            "default_mode_request_user_input",
        }
        config = params.get("config", {})
        features = config.get("features", {})
        overrides = [
            sys.argv[index + 1]
            for index, item in enumerate(sys.argv[:-1])
            if item == "-c"
        ]
        assert required <= features.keys()
        assert all(value is False for value in features.values())
        assert all(f"features.{feature}=false" in overrides for feature in required)
        assert "notify=[]" in overrides and "project_doc_max_bytes=0" in overrides
        assert params.get("sandbox") == "read-only"
        assert params.get("approvalPolicy") == "never"
        assert params.get("dynamicTools") == []
        assert params.get("selectedCapabilityRoots") == []
        assert params.get("modelProvider") == "openai"
        assert params.get("baseInstructions") and params.get("developerInstructions")
        assert config.get("web_search") == "disabled"
        assert config.get("notify") == [] and config.get("project_doc_max_bytes") == 0
        assert config.get("mcp_servers", {}).get("fixture_untrusted") == {
            "enabled": False,
            "required": False,
        }

    def planning_result(self, params, text):
        assert isinstance(params.get("outputSchema"), dict)
        assert params["outputSchema"]["properties"]["edges"]["type"] == "array"
        data = json.loads(text)
        ids = {issue["number"]: issue["id"] for issue in data["issues"]}
        configuration = json.loads(
            os.environ.get("AISPACE_FAKE_CODEX_PLANNER_JSON", "{}")
        )
        provider = os.environ.get("AISPACE_E2E_PROVIDER_URL")
        if provider:
            with urlopen(f"{provider}/planner", timeout=3) as response:
                configuration = json.load(response)
        mode = configuration.get("mode", "valid")
        pairs = [(ids[2], ids[3])] if {2, 3} <= ids.keys() else []
        if mode == "cycle":
            pairs += [(ids[3], ids[1])]
        elif mode == "self":
            pairs = [(ids[1], ids[1])]
        elif mode == "foreign":
            pairs = [(ids[1], 999999999)]
        elif mode == "reversed":
            pairs = [(ids[2], ids[1])]
        graph = {
            "edges": [
                {
                    "source": source,
                    "target": target,
                    "origin": "ai",
                    "explanation": "UI использует API",
                }
                for source, target in pairs
            ]
        }
        return {
            "planning": True,
            "mode": mode,
            "text": "not graph JSON" if mode == "invalid" else json.dumps(graph),
            "delay": min(
                float(configuration.get("delay", 30 if mode == "slow" else 0.05)), 30
            ),
        }

    def start_turn(self, message, params):
        thread_id = params["threadId"]
        turn_id = uuid.uuid4().hex
        text = "\n".join(item.get("text", "") for item in params.get("input", []))
        labels = re.findall(r"\[\[label:([^\]]+)\]\]", text)
        delays = re.findall(r"\[\[delay:([\d.]+)\]\]", text)
        label = labels[-1] if labels else "codex-chat"
        delay = min(float(delays[-1]), 30) if delays else 0.1
        stop = threading.Event()
        record = {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "label": label,
            "stop": stop,
        }
        if self.threads[thread_id].get("ephemeral"):
            record.update(self.planning_result(params, text))
            record["label"] = label = "github-planner"
            delay = record["delay"]
        self.turns[turn_id] = record
        self.audit(
            "planning_start" if record.get("planning") else "start",
            thread_id=thread_id,
            turn_id=turn_id,
            label=label,
            input=text,
            parameters=self.threads[thread_id],
            turn_parameters=params,
        )
        turn = {"id": turn_id, "status": "inProgress", "items": []}
        self.result(message, {"turn": turn})
        self.notify("turn/started", {"threadId": thread_id, "turn": turn})
        worker = threading.Thread(
            target=self.execute_turn,
            args=(record, delay),
        )
        self.workers.append(worker)
        worker.start()

    def execute_turn(self, record, delay):
        thread_id = record["thread_id"]
        turn_id = record["turn_id"]
        label = record["label"]
        interrupted = record["stop"].wait(delay)
        try:
            if interrupted:
                self.audit(
                    "planning_cancelled" if record.get("planning") else "cancelled",
                    thread_id=thread_id,
                    turn_id=turn_id,
                    label=label,
                )
            else:
                text = record.get("text", f"Готово: {label}.")
                item_id = uuid.uuid4().hex
                if record.get("mode") == "tool":
                    self.notify(
                        "item/completed",
                        {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "item": {
                                "id": item_id,
                                "type": "mcpToolCall",
                                "status": "failed",
                            },
                        },
                    )
                self.notify(
                    "item/agentMessage/delta",
                    {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "itemId": item_id,
                        "delta": text,
                    },
                )
                self.notify(
                    "item/completed",
                    {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {"id": item_id, "type": "agentMessage", "text": text},
                    },
                )
                goal = self.goals.get(thread_id)
                if goal:
                    goal["status"] = "complete"
                    (self.state / f"{thread_id}.goal.json").write_text(json.dumps(goal))
                    self.notify(
                        "thread/goal/updated", {"threadId": thread_id, "goal": goal}
                    )
                self.audit(
                    "planning_finish" if record.get("planning") else "finish",
                    thread_id=thread_id,
                    turn_id=turn_id,
                    label=label,
                )
            self.turns.pop(turn_id, None)
            self.notify(
                "turn/completed",
                {
                    "threadId": thread_id,
                    "turn": {
                        "id": turn_id,
                        "status": "interrupted" if interrupted else "completed",
                        "items": [],
                    },
                },
            )
        except BrokenPipeError:
            self.turns.pop(turn_id, None)

    def handle(self, message):
        method = message.get("method")
        params = message.get("params") or {}
        if "id" not in message:
            return
        if method == "initialize":
            result = {"userAgent": "aispace-e2e-codex"}
        elif method == "config/read":
            self.audit("planning_config_read")
            result = {
                "config": {
                    "mcp_servers": {
                        "fixture_untrusted": {
                            "command": "must-never-run",
                            "enabled": True,
                        },
                    }
                }
            }
        elif method == "account/read":
            result = {
                "account": {
                    "type": "chatgpt",
                    "email": "local-test@example.invalid",
                    "planType": "pro",
                },
                "requiresOpenaiAuth": True,
            }
        elif method == "account/login/start":
            result = {
                "type": "chatgptDeviceCode",
                "loginId": uuid.uuid4().hex,
                "verificationUrl": "https://example.invalid/activate",
                "userCode": "E2E-TEST",
            }
        elif method in {"account/login/cancel", "account/logout"}:
            result = {}
        elif method in {"thread/start", "thread/resume"}:
            result = self.thread(params)
        elif method == "turn/start":
            self.start_turn(message, params)
            return
        elif method == "turn/interrupt":
            self.audit("interrupt", parameters=params)
            record = self.turns.get(params["turnId"])
            self.result(message, {})
            if record:
                record["stop"].set()
            return
        elif method == "thread/backgroundTerminals/clean":
            self.audit("cleanup", thread_id=params["threadId"])
            result = {}
        elif method == "thread/backgroundTerminals/list":
            result = {"terminals": []}
        elif method == "thread/goal/get":
            self.audit("goal_get", parameters=params)
            result = {"goal": self.goals.get(params["threadId"])}
        elif method == "thread/goal/set":
            thread_id = params["threadId"]
            goal = {
                **self.goals.get(thread_id, {}),
                **{
                    key: params[key] for key in ("objective", "status") if key in params
                },
            }
            self.goals[thread_id] = goal
            (self.state / f"{thread_id}.goal.json").write_text(json.dumps(goal))
            self.audit("goal_set", thread_id=thread_id, goal=goal)
            result = {"goal": goal}
            self.notify("thread/goal/updated", {"threadId": thread_id, "goal": goal})
        elif method == "thread/goal/clear":
            self.goals.pop(params["threadId"], None)
            (self.state / f"{params['threadId']}.goal.json").unlink(missing_ok=True)
            self.audit("goal_clear", parameters=params)
            result = {}
        else:
            self.emit(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": f"Unsupported method: {method}",
                    },
                }
            )
            return
        self.result(message, result)

    def shutdown(self):
        for turn in list(self.turns.values()):
            turn["stop"].set()
        for worker in self.workers:
            worker.join(timeout=4)
        ephemeral = [
            thread_id
            for thread_id, params in self.threads.items()
            if params.get("ephemeral")
        ]
        if ephemeral:
            self.audit("planning_cleanup", thread_ids=ephemeral)
        self.audit("process_exit", active_turns=len(self.turns))


def main():
    server = FakeCodex()

    def terminate(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    try:
        for line in sys.stdin:
            server.handle(json.loads(line))
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
