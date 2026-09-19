import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from .models import Pipeline


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def operation_hash(payload):
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


class ImportStore:
    def __init__(self, store):
        self.store = store
        self.db = store.connection
        with self.db:
            for statement in (
                "CREATE TABLE IF NOT EXISTS github_plans (id TEXT PRIMARY KEY, data TEXT NOT NULL)",
                (
                    "CREATE TABLE IF NOT EXISTS github_imports (operation_id TEXT PRIMARY KEY, "
                    "payload_hash TEXT NOT NULL, workspace_id TEXT REFERENCES workspaces(id) "
                    "ON DELETE SET NULL, data TEXT NOT NULL)"
                ),
                (
                    "CREATE TABLE IF NOT EXISTS tasklet_sources (tasklet_id TEXT PRIMARY KEY "
                    "REFERENCES tasklets(id) ON DELETE CASCADE, data TEXT NOT NULL)"
                ),
                (
                    "CREATE TABLE IF NOT EXISTS edge_sources (edge_id TEXT PRIMARY KEY "
                    "REFERENCES edges(id) ON DELETE CASCADE, data TEXT NOT NULL)"
                ),
            ):
                self.db.execute(statement)
            for row in self.db.execute("SELECT id,data FROM github_plans").fetchall():
                data = json.loads(row["data"])
                if data["status"] == "running":
                    data.update(status="cancelled", error="Backend перезапущен. Повторите анализ.")
                    self.db.execute(
                        "UPDATE github_plans SET data=? WHERE id=?", (canonical(data), row["id"])
                    )

    def save_plan(self, plan):
        with self.db:
            self.db.execute(
                "INSERT INTO github_plans (id,data) VALUES (?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (plan["id"], canonical(plan)),
            )

    def plan(self, plan_id):
        row = self.db.execute("SELECT data FROM github_plans WHERE id=?", (plan_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def tasklet_source(self, tasklet_id):
        row = self.db.execute(
            "SELECT data FROM tasklet_sources WHERE tasklet_id=?", (tasklet_id,)
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def edge_source(self, edge_id):
        row = self.db.execute(
            "SELECT data FROM edge_sources WHERE edge_id=?", (edge_id,)
        ).fetchone()
        return json.loads(row["data"]) if row else {}

    def metadata(self, workspace_id):
        row = self.db.execute(
            "SELECT data FROM github_imports WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def existing(self, operation_id, fingerprint):
        row = self.db.execute(
            "SELECT payload_hash,workspace_id FROM github_imports WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != fingerprint:
            raise HTTPException(
                409, "Ключ импорта уже использован с другим планом или параметрами."
            )
        workspace = self.store.workspace(row["workspace_id"]) if row["workspace_id"] else None
        if workspace is None:
            raise HTTPException(410, "Это пространство уже было создано и затем удалено.")
        return workspace

    def create(self, payload, plan, graph):
        fingerprint = operation_hash(payload)
        existing = self.existing(payload["operation_id"], fingerprint)
        if existing is not None:
            return existing
        stamp = datetime.now(UTC).isoformat()
        workspace_id = uuid4().hex
        selection = plan["selection"]
        ids = {issue["id"]: uuid4().hex for issue in selection["issues"]}
        with self.db:
            self.db.execute(
                "INSERT INTO workspaces (id,name,workspace_context,working_directory,created_at,updated_at,pipeline) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    workspace_id,
                    payload["name"],
                    "",
                    payload["working_directory"],
                    stamp,
                    stamp,
                    Pipeline().model_dump_json(),
                ),
            )
            for issue in selection["issues"]:
                tasklet_id = ids[issue["id"]]
                self.db.execute(
                    "INSERT INTO tasklets (id,workspace_id,title,prompt,model,working_directory,status,position,created_at,updated_at,error,last_output) "
                    "VALUES (?,?,?,?,NULL,NULL,'idle',?,?,?,NULL,'')",
                    (
                        tasklet_id,
                        workspace_id,
                        issue["title"],
                        issue["body"] or issue["title"],
                        canonical(graph["positions"][str(issue["id"])]),
                        stamp,
                        stamp,
                    ),
                )
                source = {
                    "provider": "github",
                    "repository": selection["repository"],
                    "issue": issue,
                    "imported_at": stamp,
                }
                self.db.execute(
                    "INSERT INTO tasklet_sources VALUES (?,?)", (tasklet_id, canonical(source))
                )
            for edge in graph["edges"]:
                edge_id = uuid4().hex
                self.db.execute(
                    "INSERT INTO edges (id,workspace_id,source,target,pass_context) VALUES (?,?,?,?,0)",
                    (edge_id, workspace_id, ids[edge["source"]], ids[edge["target"]]),
                )
                self.db.execute(
                    "INSERT INTO edge_sources VALUES (?,?)",
                    (
                        edge_id,
                        canonical({"origin": edge["origin"], "explanation": edge["explanation"]}),
                    ),
                )
            metadata = {
                "selection_id": selection["id"],
                "plan_id": plan["id"],
                "repository": selection["repository"],
                "decisions": payload["decisions"],
                "selection_changes": selection.get("history", []),
                "imported_at": stamp,
            }
            self.db.execute(
                "INSERT INTO github_imports (operation_id,payload_hash,workspace_id,data) VALUES (?,?,?,?)",
                (payload["operation_id"], fingerprint, workspace_id, canonical(metadata)),
            )
        return self.store.workspace(workspace_id)
