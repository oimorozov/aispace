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
        if not destination:
            return
        payload = {
            "transport": "codex",
            "kind": kind,
            "process_id": self.process_id,
            "time": time.monotonic(),
            **values,
        }
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
        thread_id = params.get("threadId") or uuid.uuid4().hex
        path = self.state / f"{thread_id}.json"
        previous = json.loads(path.read_text()) if path.exists() else {}
        current = {**previous, **params, "id": thread_id}
        current.pop("threadId", None)
        self.threads[thread_id] = current
        goal_path = self.state / f"{thread_id}.goal.json"
        if goal_path.exists():
            self.goals[thread_id] = json.loads(goal_path.read_text())
        path.write_text(json.dumps(current))
        self.audit("thread", thread_id=thread_id, parameters=current)
        return {"thread": current}

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
        self.turns[turn_id] = record
        self.audit(
            "start",
            thread_id=thread_id,
            turn_id=turn_id,
            label=label,
            input=text,
            parameters=self.threads[thread_id],
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
                    "cancelled", thread_id=thread_id, turn_id=turn_id, label=label
                )
            else:
                text = f"Готово: {label}."
                item_id = uuid.uuid4().hex
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
                self.audit("finish", thread_id=thread_id, turn_id=turn_id, label=label)
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
