"""Persistencia local en SQLite: cada ejecución conserva su transcripción."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from uuid import uuid4


class IdeaNotFound(Exception):
    pass


class EstimationInProgress(Exception):
    pass


class EmptyTranscription(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _database(data_dir: Path):
    with closing(sqlite3.connect(Path(data_dir) / "ideas.sqlite3", timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        with db:
            yield db


def init_store(data_dir: Path) -> None:
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    with _database(data_dir) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS ideas (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                description TEXT NOT NULL, transcription TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK(status IN ('draft','in_review','planned','done')),
                notes TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS estimation_runs (
                id TEXT PRIMARY KEY, idea_id TEXT NOT NULL REFERENCES ideas(id),
                state TEXT NOT NULL CHECK(state IN ('running','completed','failed')),
                transcription TEXT NOT NULL, estimation TEXT, model TEXT, provider TEXT,
                error TEXT, started_at TEXT NOT NULL, finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS runs_by_idea ON estimation_runs(idea_id, started_at);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_idea
                ON estimation_runs(idea_id) WHERE state = 'running';
        """)


def recover_running_runs(data_dir: Path) -> None:
    """Al iniciar un único servidor local, cerrar trabajos interrumpidos."""
    with _database(data_dir) as db:
        now = _now()
        db.execute("UPDATE ideas SET updated_at = ? WHERE id IN "
                   "(SELECT idea_id FROM estimation_runs WHERE state = 'running')", (now,))
        db.execute("UPDATE estimation_runs SET state = 'failed', finished_at = ?, error = ? "
                   "WHERE state = 'running'",
                   (now, "La aplicación se reinició antes de terminar la estimación."))


def _idea(db, idea_id: str) -> dict:
    row = db.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    if row is None:
        raise IdeaNotFound()
    return dict(row)


def _summary(db, idea: dict) -> dict:
    latest = db.execute("SELECT * FROM estimation_runs WHERE idea_id = ? "
                        "ORDER BY started_at DESC, rowid DESC LIMIT 1", (idea["id"],)).fetchone()
    count = db.execute("SELECT COUNT(*) FROM estimation_runs WHERE idea_id = ?",
                       (idea["id"],)).fetchone()[0]
    return {**idea, "latest_run": dict(latest) if latest else None, "run_count": count}


def list_ideas(data_dir: Path) -> list[dict]:
    with _database(data_dir) as db:
        rows = db.execute("SELECT * FROM ideas ORDER BY updated_at DESC, rowid DESC").fetchall()
        return [_summary(db, dict(row)) for row in rows]


def get_idea(data_dir: Path, idea_id: str) -> dict:
    with _database(data_dir) as db:
        idea = _summary(db, _idea(db, idea_id))
        idea["runs"] = [dict(row) for row in db.execute(
            "SELECT * FROM estimation_runs WHERE idea_id = ? ORDER BY started_at DESC, rowid DESC",
            (idea_id,)).fetchall()]
        return idea


def create_idea(data_dir: Path, values: dict) -> dict:
    idea_id, now = str(uuid4()), _now()
    with _database(data_dir) as db:
        db.execute("INSERT INTO ideas (id,title,description,transcription,notes,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", (idea_id, values["title"], values.get("description", ""),
                   values.get("transcription", ""), values.get("notes", ""), now, now))
    return get_idea(data_dir, idea_id)


def update_idea(data_dir: Path, idea_id: str, values: dict) -> dict:
    allowed = {"title", "description", "transcription", "status", "notes"}
    updates = {key: value for key, value in values.items() if key in allowed}
    with _database(data_dir) as db:
        _idea(db, idea_id)
        if updates:
            updates["updated_at"] = _now()
            assignments = ", ".join(f"{key} = ?" for key in updates)
            db.execute(f"UPDATE ideas SET {assignments} WHERE id = ?",
                       (*updates.values(), idea_id))
    return get_idea(data_dir, idea_id)


def start_run(data_dir: Path, idea_id: str) -> dict:
    with _database(data_dir) as db:
        # La lectura de la transcripción y la reserva del trabajo son atómicas.
        db.execute("BEGIN IMMEDIATE")
        idea = _idea(db, idea_id)
        if not idea["transcription"].strip():
            raise EmptyTranscription()
        run_id, now = str(uuid4()), _now()
        try:
            db.execute("INSERT INTO estimation_runs (id,idea_id,state,transcription,started_at) "
                       "VALUES (?,?,'running',?,?)", (run_id, idea_id, idea["transcription"], now))
        except sqlite3.IntegrityError as error:
            raise EstimationInProgress() from error
        db.execute("UPDATE ideas SET updated_at = ? WHERE id = ?", (now, idea_id))
        return dict(db.execute("SELECT * FROM estimation_runs WHERE id = ?", (run_id,)).fetchone())


def finish_run(data_dir: Path, run_id: str, result: dict | None = None) -> None:
    """Guardar solo errores genéricos: nunca textos de excepciones ni credenciales."""
    result = result or {}
    state = "completed" if result else "failed"
    error = None if result else "No se pudo generar la estimación. Puedes volver a intentarlo."
    with _database(data_dir) as db:
        now = _now()
        db.execute("UPDATE estimation_runs SET state=?,estimation=?,model=?,provider=?,error=?,"
                   "finished_at=? WHERE id=? AND state='running'",
                   (state, result.get("estimation"), result.get("model"), result.get("provider"),
                    error, now, run_id))
        db.execute("UPDATE ideas SET updated_at = ? WHERE id = "
                   "(SELECT idea_id FROM estimation_runs WHERE id = ?)", (now, run_id))
