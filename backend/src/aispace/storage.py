import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import Pipeline


def now():
    return datetime.now(UTC).isoformat()


def new_id():
    return uuid4().hex


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "aispace.sqlite3"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasklets (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL,
                model TEXT, status TEXT NOT NULL, position TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                error TEXT, last_output TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edges (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL REFERENCES tasklets(id) ON DELETE CASCADE,
                target TEXT NOT NULL REFERENCES tasklets(id) ON DELETE CASCADE,
                pass_context INTEGER NOT NULL DEFAULT 0,
                UNIQUE(source, target)
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                tasklet_id TEXT NOT NULL REFERENCES tasklets(id) ON DELETE CASCADE,
                role TEXT NOT NULL, content TEXT NOT NULL,
                created_at TEXT NOT NULL, run_id TEXT REFERENCES runs(id)
            );
            CREATE INDEX IF NOT EXISTS messages_tasklet ON messages(tasklet_id, created_at);
            CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, pipeline TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS codex_sessions (
                tasklet_id TEXT PRIMARY KEY REFERENCES tasklets(id) ON DELETE CASCADE,
                thread_id TEXT NOT NULL, cwd TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(tasklets)")}
        if "working_directory" not in columns:
            self.connection.execute("ALTER TABLE tasklets ADD COLUMN working_directory TEXT")
        self.connection.execute(
            "INSERT OR IGNORE INTO settings VALUES (1, ?)",
            (
                json.dumps(
                    {
                        "api_key": None,
                        "base_url": "https://api.openai.com/v1",
                        "model": "",
                        "max_parallel": 3,
                        "workspace_context": "",
                    }
                ),
            ),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO state VALUES (1, ?)",
            (Pipeline().model_dump_json(),),
        )
        self.connection.commit()
        self.recover()

    def close(self):
        self.connection.close()

    def recover(self):
        pipeline = self.pipeline()
        if pipeline["status"] in {"running", "stopping"}:
            pipeline.update(
                status="cancelled",
                finished_at=now(),
                error="Выполнение прервано перезапуском приложения",
            )
            self.save_pipeline(pipeline)
        self.connection.execute(
            "UPDATE tasklets SET status='cancelled', error=?, updated_at=? "
            "WHERE status IN ('running', 'queued')",
            ("Выполнение прервано перезапуском приложения", now()),
        )
        self.connection.commit()

    def settings(self, private=False):
        data = json.loads(
            self.connection.execute("SELECT data FROM settings WHERE id=1").fetchone()[0]
        )
        data = {
            "execution_mode": "api",
            "working_directory": None,
            "codex_sandbox": "read-only",
            **data,
        }
        if private:
            return data
        key = data.pop("api_key")
        return {**data, "api_key_configured": bool(key)}

    def save_settings(self, changes):
        data = self.settings(private=True)
        data.update(changes)
        if "api_key" in changes:
            data["api_key"] = data["api_key"] or None
        self.connection.execute("UPDATE settings SET data=? WHERE id=1", (json.dumps(data),))
        self.connection.commit()
        return self.settings()

    def tasklets(self):
        return [
            self.decode_tasklet(row)
            for row in self.connection.execute("SELECT * FROM tasklets ORDER BY created_at, id")
        ]

    def tasklet(self, tasklet_id):
        row = self.connection.execute("SELECT * FROM tasklets WHERE id=?", (tasklet_id,)).fetchone()
        return self.decode_tasklet(row) if row else None

    @staticmethod
    def decode_tasklet(row):
        data = dict(row)
        data["position"] = json.loads(data["position"])
        return data

    def create_tasklet(self, data):
        stamp = now()
        tasklet = {
            "id": new_id(),
            "working_directory": None,
            **data,
            "status": "idle",
            "created_at": stamp,
            "updated_at": stamp,
            "error": None,
            "last_output": "",
        }
        self.connection.execute(
            "INSERT INTO tasklets (id,title,prompt,model,status,position,created_at,updated_at,"
            "error,last_output,working_directory) VALUES (:id,:title,:prompt,:model,:status,"
            ":position,:created_at,:updated_at,:error,:last_output,:working_directory)",
            {**tasklet, "position": json.dumps(tasklet["position"])},
        )
        self.connection.commit()
        return tasklet

    def update_tasklet(self, tasklet_id, changes):
        data = {**changes, "updated_at": now()}
        if "position" in data:
            data["position"] = json.dumps(data["position"])
        fields = ", ".join(f"{key}=?" for key in data)
        self.connection.execute(
            f"UPDATE tasklets SET {fields} WHERE id=?", (*data.values(), tasklet_id)
        )
        self.connection.commit()
        return self.tasklet(tasklet_id)

    def delete_tasklet(self, tasklet_id):
        self.connection.execute("DELETE FROM tasklets WHERE id=?", (tasklet_id,))
        self.connection.commit()

    def codex_session(self, tasklet_id):
        row = self.connection.execute(
            "SELECT thread_id, cwd FROM codex_sessions WHERE tasklet_id=?", (tasklet_id,)
        ).fetchone()
        return dict(row) if row else None

    def save_codex_session(self, tasklet_id, thread_id, cwd):
        self.connection.execute(
            "INSERT OR REPLACE INTO codex_sessions (tasklet_id,thread_id,cwd) VALUES (?,?,?)",
            (tasklet_id, thread_id, str(cwd)),
        )
        self.connection.commit()

    def edges(self):
        return [
            {**dict(row), "pass_context": bool(row["pass_context"])}
            for row in self.connection.execute("SELECT * FROM edges ORDER BY rowid")
        ]

    def create_edge(self, data):
        edge = {"id": new_id(), **data}
        self.connection.execute(
            "INSERT INTO edges VALUES (:id,:source,:target,:pass_context)", edge
        )
        self.connection.commit()
        return edge

    def update_edge(self, edge_id, pass_context):
        self.connection.execute(
            "UPDATE edges SET pass_context=? WHERE id=?", (pass_context, edge_id)
        )
        self.connection.commit()
        return next(edge for edge in self.edges() if edge["id"] == edge_id)

    def delete_edge(self, edge_id):
        self.connection.execute("DELETE FROM edges WHERE id=?", (edge_id,))
        self.connection.commit()

    def messages(self, tasklet_id):
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM messages WHERE tasklet_id=? ORDER BY created_at, rowid",
                (tasklet_id,),
            )
        ]

    def create_message(self, tasklet_id, role, content, run_id):
        message = {
            "id": new_id(),
            "tasklet_id": tasklet_id,
            "role": role,
            "content": content,
            "created_at": now(),
            "run_id": run_id,
        }
        self.connection.execute(
            "INSERT INTO messages VALUES (:id,:tasklet_id,:role,:content,:created_at,:run_id)",
            message,
        )
        self.connection.commit()
        return message

    def update_message(self, message, content):
        self.connection.execute(
            "UPDATE messages SET content=? WHERE id=?", (content, message["id"])
        )
        self.connection.commit()
        return {**message, "content": content}

    def pipeline(self):
        return json.loads(
            self.connection.execute("SELECT pipeline FROM state WHERE id=1").fetchone()[0]
        )

    def save_pipeline(self, data):
        serialized = json.dumps(data)
        self.connection.execute("UPDATE state SET pipeline=? WHERE id=1", (serialized,))
        if data["id"]:
            self.connection.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?)", (data["id"], serialized)
            )
        self.connection.commit()

    def workspace(self):
        return {"tasklets": self.tasklets(), "edges": self.edges(), "pipeline": self.pipeline()}
