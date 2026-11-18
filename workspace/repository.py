"""Short SQLite transactions. No transaction is held across external awaits."""
import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def new_id(prefix):
    return prefix + "_" + uuid.uuid4().hex


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class WorkspaceError(RuntimeError):
    pass


class Repository:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "workspace.sqlite3"

    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    async def transact(self, fn):
        def run():
            db = self.connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                result = fn(db)
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        return await asyncio.to_thread(run)

    async def initialize(self):
        def init(db):
            db.execute("CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, scope TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY, incident TEXT NOT NULL REFERENCES incidents(id),
                kind TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS records_scope ON records(incident,kind)")
            db.execute("CREATE INDEX IF NOT EXISTS resource_identity ON records(incident,json_extract(body,'$.cluster'),json_extract(body,'$.namespace'),json_extract(body,'$.name')) WHERE kind='resource'")
            db.execute("CREATE INDEX IF NOT EXISTS evidence_task_time ON records(incident,json_extract(body,'$.producer_task_id'),json_extract(body,'$.requested_at')) WHERE kind='evidence'")
            db.execute("CREATE INDEX IF NOT EXISTS attempt_task_time ON records(incident,json_extract(body,'$.task_id'),json_extract(body,'$.started_at')) WHERE kind='attempt'")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, incident TEXT REFERENCES incidents(id),
                record_id TEXT, action TEXT, at TEXT, body TEXT)""")
        await self.transact(init)

    @staticmethod
    def get(db, incident, record_id, kind=None):
        row = db.execute("SELECT * FROM records WHERE id=? AND incident=?", (record_id, incident)).fetchone()
        if row is None or (kind and row["kind"] != kind):
            raise WorkspaceError("reference missing or outside incident")
        return json.loads(row["body"])

    @staticmethod
    def list(db, incident, kind):
        return [json.loads(r[0]) for r in db.execute(
            "SELECT body FROM records WHERE incident=? AND kind=? ORDER BY rowid", (incident, kind))]

    @staticmethod
    def put(db, incident, kind, value, expected=None, action="saved"):
        value = dict(value)
        record_id = value["id"]
        row = db.execute("SELECT revision,incident,kind FROM records WHERE id=?", (record_id,)).fetchone()
        if row and (row[1] != incident or row[2] != kind):
            raise WorkspaceError("reference outside incident or wrong type")
        revision = row[0] if row else 0
        if expected is not None and expected != revision:
            raise WorkspaceError("revision conflict")
        value.update(incident_id=incident, revision=revision + 1)
        value[{"resource": "resource_id", "evidence": "evidence_id", "artifact": "artifact_id",
               "context": "context_id", "task": "task_id", "attempt": "attempt_id",
               "assessment": "assessment_id", "run": "run_id"}.get(kind, "id")] = record_id
        db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?)",
                   (record_id, incident, kind, value["revision"], json.dumps(value, ensure_ascii=False)))
        Repository.event(db, incident, record_id, action, value)
        return value

    @staticmethod
    def event(db, incident, record_id, action, body):
        db.execute("INSERT INTO events(incident,record_id,action,at,body) VALUES(?,?,?,?,?)",
                   (incident, record_id, action, utcnow(), json.dumps(body, ensure_ascii=False)))

    async def read(self, incident, record_id, kind=None):
        return await self.transact(lambda db: self.get(db, incident, record_id, kind))

    async def all(self, incident, kind):
        return await self.transact(lambda db: self.list(db, incident, kind))

    async def save(self, incident, kind, value, expected=None, action="saved"):
        return await self.transact(lambda db: self.put(db, incident, kind, value, expected, action))
