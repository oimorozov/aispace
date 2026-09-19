import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from .import_store import ImportStore
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
        try:
            self._initialize()
            with self.connection:
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS integrations (name TEXT PRIMARY KEY, secret TEXT)"
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS github_selections (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
                )
            self.imports = ImportStore(self)
            self.recover()
        except BaseException:
            self.connection.close()
            raise

    def _initialize(self):
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise RuntimeError("Версия базы данных новее версии приложения")
        if version < 2:
            self.connection.execute("PRAGMA foreign_keys=OFF")
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                if version == 0:
                    self._migrate_workspaces()
                self.connection.execute("ALTER TABLE tasklets ADD COLUMN conversation_id TEXT")
                if self.connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise sqlite3.IntegrityError("Нарушена принадлежность данных пространства")
                self.connection.execute("PRAGMA user_version=2")
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
        self.connection.execute("PRAGMA foreign_keys=ON")

    def _migrate_workspaces(self):
        legacy_tables = (
            "CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL)",
            ("CREATE TABLE IF NOT EXISTS tasklets (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "prompt TEXT NOT NULL, model TEXT, status TEXT NOT NULL, position TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT, "
            "last_output TEXT NOT NULL)"),
            ("CREATE TABLE IF NOT EXISTS edges (id TEXT PRIMARY KEY, source TEXT NOT NULL, "
            "target TEXT NOT NULL, pass_context INTEGER NOT NULL DEFAULT 0, UNIQUE(source,target))"),
            "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)",
            ("CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, tasklet_id TEXT NOT NULL, "
            "role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, run_id TEXT)"),
            "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, pipeline TEXT NOT NULL)",
            ("CREATE TABLE IF NOT EXISTS codex_sessions (tasklet_id TEXT PRIMARY KEY, "
            "thread_id TEXT NOT NULL, cwd TEXT NOT NULL)"),
        )
        for statement in legacy_tables:
            self.connection.execute(statement)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(tasklets)")}
        if "working_directory" not in columns:
            self.connection.execute("ALTER TABLE tasklets ADD COLUMN working_directory TEXT")
        defaults = {
            "api_key": None,
            "base_url": "https://api.openai.com/v1",
            "model": "",
            "max_parallel": 3,
        }
        self.connection.execute(
            "INSERT OR IGNORE INTO settings VALUES (1, ?)", (json.dumps(defaults),)
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO state VALUES (1, ?)", (Pipeline().model_dump_json(),)
        )
        settings = json.loads(
            self.connection.execute("SELECT data FROM settings WHERE id=1").fetchone()[0]
        )
        pipeline = self.connection.execute("SELECT pipeline FROM state WHERE id=1").fetchone()[0]
        workspace_id = new_id()
        stamp = now()
        self.connection.execute(
            "CREATE TABLE workspaces (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "workspace_context TEXT NOT NULL, working_directory TEXT, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, pipeline TEXT NOT NULL)"
        )
        self.connection.execute(
            "INSERT INTO workspaces VALUES (?,?,?,?,?,?,?)",
            (
                workspace_id,
                "Моё пространство",
                settings.pop("workspace_context", ""),
                settings.pop("working_directory", None),
                stamp,
                stamp,
                pipeline,
            ),
        )
        tables = {
            "tasklets": (
                ("id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) "
                "ON DELETE CASCADE, title TEXT NOT NULL, prompt TEXT NOT NULL, model TEXT, "
                "status TEXT NOT NULL, position TEXT NOT NULL, created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, error TEXT, last_output TEXT NOT NULL, "
                "working_directory TEXT, UNIQUE(id,workspace_id)"),
                ("id,title,prompt,model,status,position,created_at,updated_at,error,last_output,"
                "working_directory"),
            ),
            "runs": (
                ("id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) "
                "ON DELETE CASCADE, data TEXT NOT NULL, UNIQUE(id,workspace_id)"),
                "id,data",
            ),
            "edges": (
                ("id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) "
                "ON DELETE CASCADE, source TEXT NOT NULL, target TEXT NOT NULL, "
                "pass_context INTEGER NOT NULL DEFAULT 0, UNIQUE(source,target), "
                "FOREIGN KEY(source,workspace_id) REFERENCES tasklets(id,workspace_id) "
                "ON DELETE CASCADE, FOREIGN KEY(target,workspace_id) "
                "REFERENCES tasklets(id,workspace_id) ON DELETE CASCADE"),
                "id,source,target,pass_context",
            ),
            "messages": (
                ("id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) "
                "ON DELETE CASCADE, tasklet_id TEXT NOT NULL, role TEXT NOT NULL, "
                "content TEXT NOT NULL, created_at TEXT NOT NULL, run_id TEXT, "
                "FOREIGN KEY(tasklet_id,workspace_id) REFERENCES tasklets(id,workspace_id) "
                "ON DELETE CASCADE, FOREIGN KEY(run_id,workspace_id) "
                "REFERENCES runs(id,workspace_id) ON DELETE CASCADE"),
                "id,tasklet_id,role,content,created_at,run_id",
            ),
            "codex_sessions": (
                ("tasklet_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL "
                "REFERENCES workspaces(id) ON DELETE CASCADE, thread_id TEXT NOT NULL, "
                "cwd TEXT NOT NULL, FOREIGN KEY(tasklet_id,workspace_id) "
                "REFERENCES tasklets(id,workspace_id) ON DELETE CASCADE"),
                "tasklet_id,thread_id,cwd",
            ),
        }
        for table, (definition, fields) in tables.items():
            self.connection.execute(f"CREATE TABLE {table}_v1 ({definition})")
            self.connection.execute(
                f"INSERT INTO {table}_v1 (rowid,workspace_id,{fields}) "
                f"SELECT rowid,?,{fields} FROM {table}",
                (workspace_id,),
            )
        for table in ("messages", "edges", "codex_sessions", "tasklets", "runs", "state"):
            self.connection.execute(f"DROP TABLE {table}")
        for table in tables:
            self.connection.execute(f"ALTER TABLE {table}_v1 RENAME TO {table}")
        for table in tables:
            self.connection.execute(f"CREATE INDEX {table}_workspace ON {table}(workspace_id)")
        self.connection.execute(
            "CREATE INDEX messages_tasklet ON messages(tasklet_id,created_at)"
        )
        self.connection.execute(
            "UPDATE settings SET data=? WHERE id=1", (json.dumps(settings),)
        )

    def close(self):
        self.connection.close()

    def github_token(self):
        row = self.connection.execute(
            "SELECT secret FROM integrations WHERE name='github'"
        ).fetchone()
        return row[0] if row else None

    def save_github_token(self, token):
        with self.connection:
            self.connection.execute(
                "INSERT INTO integrations (name,secret) VALUES ('github',?) "
                "ON CONFLICT(name) DO UPDATE SET secret=excluded.secret",
                (token,),
            )

    def save_github_selection(self, snapshot):
        with self.connection:
            self.connection.execute(
                "INSERT INTO github_selections (id,data) VALUES (?,?)",
                (snapshot["id"], json.dumps(snapshot)),
            )

    def github_selection(self, selection_id):
        row = self.connection.execute(
            "SELECT data FROM github_selections WHERE id=?", (selection_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def recover(self):
        for workspace in self.workspaces():
            pipeline = workspace["pipeline"]
            if pipeline["status"] in {"running", "stopping"}:
                pipeline.update(
                    status="cancelled",
                    finished_at=now(),
                    error="Выполнение прервано перезапуском приложения",
                )
                self.save_pipeline(pipeline, workspace_id=workspace["id"])
        with self.connection:
            self.connection.execute(
                "UPDATE tasklets SET status='cancelled', error=?, updated_at=? "
                "WHERE status IN ('running', 'queued')",
                ("Выполнение прервано перезапуском приложения", now()),
            )

    @property
    def default_workspace_id(self):
        row = self.connection.execute(
            "SELECT id FROM workspaces ORDER BY created_at,id LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def _workspace_id(self, workspace_id=None):
        workspace_id = workspace_id if workspace_id is not None else self.default_workspace_id
        if not self.workspace_info(workspace_id):
            raise HTTPException(404, "Пространство не найдено")
        return workspace_id

    @staticmethod
    def _decode_workspace(row):
        return {**dict(row), "pipeline": json.loads(row["pipeline"])}

    def workspaces(self):
        return [
            self._decode_workspace(row)
            for row in self.connection.execute(
                "SELECT id,name,created_at,updated_at,pipeline FROM workspaces "
                "ORDER BY created_at,id"
            )
        ]

    def workspace_info(self, workspace_id):
        row = self.connection.execute(
            "SELECT * FROM workspaces WHERE id=?", (workspace_id,)
        ).fetchone()
        return self._decode_workspace(row) if row else None

    def workspace(self, workspace_id=None):
        workspace_id = workspace_id if workspace_id is not None else self.default_workspace_id
        data = self.workspace_info(workspace_id)
        if data is None:
            return None
        return {**data, "tasklets": self.tasklets(workspace_id), "edges": self.edges(workspace_id), "import_metadata": self.imports.metadata(workspace_id)}

    def create_workspace(self, data):
        stamp = now()
        workspace_id = new_id()
        with self.connection:
            self.connection.execute(
                "INSERT INTO workspaces VALUES (?,?,?,?,?,?,?)",
                (workspace_id, data["name"], "", None, stamp, stamp, Pipeline().model_dump_json()),
            )
        return self.workspace(workspace_id)

    def update_workspace(self, workspace_id, changes):
        self._check_fields(changes, {"name", "workspace_context", "working_directory"})
        if not self.workspace_info(workspace_id):
            return None
        data = {**changes, "updated_at": now()}
        fields = ", ".join(f"{key}=?" for key in data)
        with self.connection:
            self.connection.execute(
                f"UPDATE workspaces SET {fields} WHERE id=?", (*data.values(), workspace_id)
            )
        return self.workspace(workspace_id)

    def delete_workspace(self, workspace_id):
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM workspaces WHERE id=? "
                "AND json_extract(pipeline,'$.status') NOT IN ('running','stopping')",
                (workspace_id,),
            )
            if cursor.rowcount:
                return True
            if self.workspace_info(workspace_id):
                raise HTTPException(409, "Сначала остановите выполнение пространства")
            return False

    def settings(self, private=False, workspace_id=None):
        data = {
            "execution_mode": "api",
            "codex_sandbox": "read-only",
            **json.loads(
                self.connection.execute("SELECT data FROM settings WHERE id=1").fetchone()[0]
            ),
        }
        if workspace_id is not None:
            workspace = self.workspace_info(self._workspace_id(workspace_id))
            data.update(
                workspace_context=workspace["workspace_context"],
                working_directory=workspace["working_directory"],
            )
        if private:
            return data
        key = data.pop("api_key")
        return {**data, "api_key_configured": bool(key)}

    @staticmethod
    def _check_fields(changes, allowed):
        if changes.keys() - allowed:
            raise ValueError("Недопустимые поля изменения")

    def save_settings(self, changes):
        self._check_fields(
            changes,
            {"execution_mode", "codex_sandbox", "api_key", "base_url", "model", "max_parallel"},
        )
        data = {**self.settings(private=True), **changes}
        if "api_key" in changes:
            data["api_key"] = data["api_key"] or None
        with self.connection:
            self.connection.execute("UPDATE settings SET data=? WHERE id=1", (json.dumps(data),))
        return self.settings()

    def tasklets(self, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        return [
            self.decode_tasklet(row)
            for row in self.connection.execute(
                "SELECT * FROM tasklets WHERE workspace_id=? ORDER BY created_at,id",
                (workspace_id,),
            )
        ]

    def tasklet(self, tasklet_id, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        row = self.connection.execute(
            "SELECT * FROM tasklets WHERE id=? AND workspace_id=?", (tasklet_id, workspace_id)
        ).fetchone()
        return self.decode_tasklet(row) if row else None

    def _require_tasklet(self, tasklet_id, workspace_id):
        tasklet = self.tasklet(tasklet_id, workspace_id)
        if not tasklet:
            raise HTTPException(404, "Тасклет не найден")
        return tasklet

    def decode_tasklet(self, row):
        data = dict(row)
        data["position"] = json.loads(data["position"])
        data["source"] = self.imports.tasklet_source(data["id"])
        return data

    def create_tasklet(self, data, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        stamp = now()
        self._check_fields(data, {"title", "prompt", "model", "working_directory", "position"})
        tasklet = {
            "id": new_id(),
            "workspace_id": workspace_id,
            "conversation_id": None,
            "working_directory": None,
            "prompt": "",
            "model": None,
            "position": {"x": 0, "y": 0},
            **data,
            "status": "idle",
            "created_at": stamp,
            "updated_at": stamp,
            "error": None,
            "last_output": "",
        }
        with self.connection:
            self.connection.execute(
                "INSERT INTO tasklets (id,workspace_id,title,prompt,model,status,position,"
                "created_at,updated_at,error,last_output,working_directory) VALUES "
                "(:id,:workspace_id,:title,:prompt,:model,:status,:position,:created_at,"
                ":updated_at,:error,:last_output,:working_directory)",
                {**tasklet, "position": json.dumps(tasklet["position"])},
            )
        return tasklet

    def update_tasklet(self, tasklet_id, changes, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        self._require_tasklet(tasklet_id, workspace_id)
        self._check_fields(
            changes,
            {"title", "prompt", "model", "working_directory", "position", "status", "error", "last_output"},
        )
        data = {**changes, "updated_at": now()}
        if "position" in data:
            data["position"] = json.dumps(data["position"])
        fields = ", ".join(f"{key}=?" for key in data)
        with self.connection:
            self.connection.execute(
                f"UPDATE tasklets SET {fields} WHERE id=? AND workspace_id=?",
                (*data.values(), tasklet_id, workspace_id),
            )
        return self.tasklet(tasklet_id, workspace_id)

    def delete_tasklet(self, tasklet_id, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        self._require_tasklet(tasklet_id, workspace_id)
        with self.connection:
            self.connection.execute(
                "DELETE FROM tasklets WHERE id=? AND workspace_id=?", (tasklet_id, workspace_id)
            )

    def _session_workspace_id(self, tasklet_id, workspace_id=None):
        if workspace_id is None:
            row = self.connection.execute(
                "SELECT workspace_id FROM tasklets WHERE id=?", (tasklet_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Тасклет не найден")
            workspace_id = row[0]
        self._require_tasklet(tasklet_id, workspace_id)
        return workspace_id

    def codex_session(self, tasklet_id, workspace_id=None):
        workspace_id = self._session_workspace_id(tasklet_id, workspace_id)
        row = self.connection.execute(
            "SELECT thread_id,cwd FROM codex_sessions WHERE tasklet_id=? AND workspace_id=?",
            (tasklet_id, workspace_id),
        ).fetchone()
        return dict(row) if row else None

    def save_codex_session(self, tasklet_id, thread_id, cwd, workspace_id=None):
        workspace_id = self._session_workspace_id(tasklet_id, workspace_id)
        with self.connection:
            self.connection.execute(
                "INSERT INTO codex_sessions (tasklet_id,workspace_id,thread_id,cwd) VALUES (?,?,?,?) "
                "ON CONFLICT(tasklet_id) DO UPDATE SET thread_id=excluded.thread_id,cwd=excluded.cwd",
                (tasklet_id, workspace_id, thread_id, str(cwd)),
            )

    def edges(self, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        return [
            {**dict(row), "pass_context": bool(row["pass_context"]), **self.imports.edge_source(row["id"])}
            for row in self.connection.execute(
                "SELECT id,source,target,pass_context FROM edges WHERE workspace_id=? ORDER BY rowid",
                (workspace_id,),
            )
        ]

    def create_edge(self, data, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        self._check_fields(data, {"source", "target", "pass_context"})
        self._require_tasklet(data["source"], workspace_id)
        self._require_tasklet(data["target"], workspace_id)
        edge = {"id": new_id(), "pass_context": False, **data}
        with self.connection:
            self.connection.execute(
                "INSERT INTO edges (id,workspace_id,source,target,pass_context) "
                "VALUES (:id,:workspace_id,:source,:target,:pass_context)",
                {**edge, "workspace_id": workspace_id},
            )
        return edge

    def update_edge(self, edge_id, pass_context, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE edges SET pass_context=? WHERE id=? AND workspace_id=?",
                (pass_context, edge_id, workspace_id),
            )
            if not cursor.rowcount:
                raise HTTPException(404, "Связь не найдена")
        return next(edge for edge in self.edges(workspace_id) if edge["id"] == edge_id)

    def delete_edge(self, edge_id, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM edges WHERE id=? AND workspace_id=?", (edge_id, workspace_id)
            )
            if not cursor.rowcount:
                raise HTTPException(404, "Связь не найдена")

    def messages(self, tasklet_id, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        tasklet = self._require_tasklet(tasklet_id, workspace_id)
        return [
            {**dict(row), "conversation_id": tasklet["conversation_id"]}
            for row in self.connection.execute(
                "SELECT * FROM messages WHERE tasklet_id=? AND workspace_id=? "
                "ORDER BY created_at,rowid",
                (tasklet_id, workspace_id),
            )
        ]

    def create_message(self, tasklet_id, role, content, run_id, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        tasklet = self._require_tasklet(tasklet_id, workspace_id)
        if run_id and not self.connection.execute(
            "SELECT 1 FROM runs WHERE id=? AND workspace_id=?", (run_id, workspace_id)
        ).fetchone():
            raise HTTPException(404, "Запуск не найден")
        message = {
            "id": new_id(),
            "workspace_id": workspace_id,
            "conversation_id": tasklet["conversation_id"],
            "tasklet_id": tasklet_id,
            "role": role,
            "content": content,
            "created_at": now(),
            "run_id": run_id,
        }
        with self.connection:
            self.connection.execute(
                "INSERT INTO messages (id,workspace_id,tasklet_id,role,content,created_at,run_id) "
                "VALUES (:id,:workspace_id,:tasklet_id,:role,:content,:created_at,:run_id)",
                message,
            )
        return message

    def update_message(self, message, content, workspace_id=None):
        workspace_id = self._workspace_id(
            workspace_id if workspace_id is not None else message.get("workspace_id")
        )
        tasklet = self._require_tasklet(message["tasklet_id"], workspace_id)
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE messages SET content=? WHERE id=? AND tasklet_id=? AND workspace_id=?",
                (content, message["id"], message["tasklet_id"], workspace_id),
            )
            if not cursor.rowcount:
                raise HTTPException(404, "Сообщение не найдено")
        return {
            **dict(self.connection.execute(
                "SELECT * FROM messages WHERE id=? AND workspace_id=?",
                (message["id"], workspace_id),
            ).fetchone()),
            "conversation_id": tasklet["conversation_id"],
        }

    def accept_run(self, pipeline, chosen, invalidated, reset_context=True, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        chosen = set(chosen)
        invalidated = set(invalidated) - chosen
        for tasklet_id in chosen | invalidated:
            self._require_tasklet(tasklet_id, workspace_id)
        stamp = now()
        serialized = json.dumps(pipeline)
        with self.connection:
            for tasklet_id in chosen:
                if reset_context:
                    self.connection.execute(
                        "DELETE FROM messages WHERE tasklet_id=? AND workspace_id=?",
                        (tasklet_id, workspace_id),
                    )
                    self.connection.execute(
                        "DELETE FROM codex_sessions WHERE tasklet_id=? AND workspace_id=?",
                        (tasklet_id, workspace_id),
                    )
                    self.connection.execute(
                        "UPDATE tasklets SET last_output='',conversation_id=? "
                        "WHERE id=? AND workspace_id=?",
                        (pipeline["id"], tasklet_id, workspace_id),
                    )
                self.connection.execute(
                    "UPDATE tasklets SET status='queued',error=NULL,updated_at=? "
                    "WHERE id=? AND workspace_id=?",
                    (stamp, tasklet_id, workspace_id),
                )
            for tasklet_id in invalidated:
                self.connection.execute(
                    "UPDATE tasklets SET status='idle',error=NULL,updated_at=? "
                    "WHERE id=? AND workspace_id=?",
                    (stamp, tasklet_id, workspace_id),
                )
            self.connection.execute(
                "INSERT INTO runs (id,workspace_id,data) VALUES (?,?,?)",
                (pipeline["id"], workspace_id, serialized),
            )
            self.connection.execute(
                "UPDATE workspaces SET pipeline=? WHERE id=?", (serialized, workspace_id)
            )

    def pipeline(self, workspace_id=None):
        return self.workspace_info(self._workspace_id(workspace_id))["pipeline"]

    def save_pipeline(self, data, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        serialized = json.dumps(data)
        with self.connection:
            if data["id"]:
                row = self.connection.execute(
                    "SELECT workspace_id FROM runs WHERE id=?", (data["id"],)
                ).fetchone()
                if row and row[0] != workspace_id:
                    raise HTTPException(404, "Запуск не найден")
                self.connection.execute(
                    "INSERT INTO runs (id,workspace_id,data) VALUES (?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                    (data["id"], workspace_id, serialized),
                )
            self.connection.execute(
                "UPDATE workspaces SET pipeline=? WHERE id=?", (serialized, workspace_id)
            )

    def runs(self, workspace_id=None):
        workspace_id = self._workspace_id(workspace_id)
        return [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT data FROM runs WHERE workspace_id=? ORDER BY rowid", (workspace_id,)
            )
        ]
