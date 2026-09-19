import argparse
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

events = []
responses = {}
planner_settings = {"mode": "valid"}
events_lock = threading.Lock()


def record(kind, request_id, label, **extra):
    with events_lock:
        events.append(
            {
                "kind": kind,
                "request_id": request_id,
                "label": label,
                "time": time.monotonic(),
                **extra,
            }
        )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def send_json(self, status, value):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/audit":
            with events_lock:
                snapshot = list(events)
            self.send_json(200, {"events": snapshot})
        elif path == "/planner":
            with events_lock:
                settings = dict(planner_settings)
            self.send_json(200, settings)
        elif path == "/v1/models":
            authorization = self.headers.get("Authorization")
            if authorization and authorization != "Bearer local-test-key":
                self.send_json(401, {"error": {"message": "Invalid mock API key"}})
                return
            self.send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "test-model",
                            "object": "model",
                            "created": 1,
                            "owned_by": "local-test",
                        }
                    ],
                },
            )
        else:
            self.send_json(404, {"error": {"message": "Unknown mock endpoint"}})

    def do_POST(self):
        payload = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
        )
        path = self.path.split("?")[0]
        if path == "/reset":
            with events_lock:
                events.clear()
                responses.clear()
                planner_settings.clear()
                planner_settings.update(mode="valid")
            self.send_json(200, {"ok": True})
            return
        if path == "/planner":
            with events_lock:
                planner_settings.clear()
                planner_settings.update(payload)
            self.send_json(200, {"ok": True})
            return
        if path == "/responses":
            with events_lock:
                responses[payload["label"]] = payload
            self.send_json(200, {"ok": True})
            return
        if path == "/codex/audit":
            with events_lock:
                events.append(payload)
            self.send_json(200, {"ok": True})
            return
        if path != "/v1/chat/completions":
            self.send_json(404, {"error": {"message": "Unknown mock endpoint"}})
            return
        if self.headers.get("Authorization") != "Bearer local-test-key":
            self.send_json(401, {"error": {"message": "Invalid mock API key"}})
            return
        messages = payload.get("messages", [])
        if (
            payload.get("response_format", {}).get("json_schema", {}).get("name")
            == "issue_graph"
        ):
            self.plan(payload)
            return
        combined = "\n".join(str(message.get("content", "")) for message in messages)
        labels = re.findall(r"\[\[label:([^\]]+)\]\]", combined)
        delays = re.findall(r"\[\[delay:([\d.]+)\]\]", combined)
        label = labels[-1] if labels else "chat"
        if not labels:
            last = messages[-1].get("content", "") if messages else ""
            for title, planned_label in (
                ("Базовая схема", "plan-a"),
                ("API", "plan-b"),
                ("Интерфейс", "plan-c"),
                ("Документация", "plan-d"),
            ):
                if last.startswith(f"# {title}\n") or last == title:
                    label = planned_label
                    break
        delay = min(float(delays[-1]), 30) if delays else 0.1
        request_id = f"chatcmpl-{uuid.uuid4().hex}"
        record(
            "start", request_id, label, messages=messages, model=payload.get("model")
        )
        with events_lock:
            response = responses.get(label)
        chunks = response.get("chunks", []) if response else []
        output = (
            "".join(chunk["content"] for chunk in chunks)
            if response
            else f"Готово: {label}."
        )
        try:
            if not payload.get("stream"):
                time.sleep(delay)
                record("finish", request_id, label)
                self.send_json(
                    200,
                    {
                        "id": request_id,
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": "test-model",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": output},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        },
                    },
                )
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                self.send_chunk(request_id, {"role": "assistant", "content": ""})
                if response:
                    for chunk in chunks:
                        self.wait_stream(chunk.get("delay", 0))
                        self.send_chunk(request_id, {"content": chunk["content"]})
                        record("chunk", request_id, label, content=chunk["content"])
                    if response.get("error"):
                        self.wfile.write(
                            b'data: {"error":{"message":"Mock stream failure","type":"server_error","code":"mock_stream_error"}}\n\n'
                        )
                        self.wfile.flush()
                        record("error", request_id, label)
                        return
                else:
                    self.wait_stream(delay)
                    self.send_chunk(request_id, {"content": output})
                record("finish", request_id, label)
                self.send_chunk(request_id, {}, "stop")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            record("disconnect", request_id, label)

    def plan(self, payload):
        request_id = uuid.uuid4().hex
        with events_lock:
            settings = dict(planner_settings)
        mode = settings.get("mode", "valid")
        record(
            "planning",
            request_id,
            "issue-graph",
            transport="api",
            payload=payload,
            mode=mode,
        )
        data = json.loads(payload["messages"][-1]["content"])
        ids = {issue["id"] for issue in data["issues"]}
        edge = {
            "source": 400002,
            "target": 400003,
            "origin": "ai",
            "explanation": "Интерфейсу нужен подготовленный API.",
        }
        edges = [edge] if {400002, 400003}.issubset(ids) else []
        if mode == "cycle":
            edges = [edge, {**edge, "source": 400003, "target": 400001}]
        elif mode == "self":
            edges = [{**edge, "source": 400003, "target": 400003}]
        elif mode == "foreign":
            edges = [{**edge, "source": 999999999}]
        elif mode == "reversed":
            edges = [{**edge, "source": 400002, "target": 400001}]
        elif mode == "unavailable":
            self.send_json(503, {"error": {"message": "fixture unavailable"}})
            return
        content = "not JSON" if mode == "invalid" else json.dumps({"edges": edges})
        message = {"role": "assistant", "content": content}
        if mode == "tool":
            message["tool_calls"] = [
                {
                    "id": "forbidden",
                    "type": "function",
                    "function": {"name": "shell", "arguments": "{}"},
                }
            ]
        try:
            time.sleep(settings.get("delay", 8 if mode == "slow" else 0.01))
            self.send_json(
                200, {"choices": [{"message": message, "finish_reason": "stop"}]}
            )
            record("planning_finish", request_id, "issue-graph", transport="api")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            record("planning_disconnect", request_id, "issue-graph", transport="api")

    def wait_stream(self, delay):
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            self.wfile.write(b": keepalive\n\n")
            self.wfile.flush()
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def send_chunk(self, request_id, delta, finish_reason=None):
        chunk = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "test-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"Mock provider ready on http://{args.host}:{args.port}/v1", flush=True)
    server.serve_forever()
