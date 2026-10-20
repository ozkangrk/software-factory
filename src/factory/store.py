"""Store — SQLite kalıcılık (CS10 registry kalıbı: WAL + busy_timeout, atomik claim).

Task, gate, domain, olay tabloları tek DB'de. Tüm yazılar lock'lu; okumalar
serbest. Fail-closed semantik gates.py'de; burada sadece şema + atomik işlemler.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'feature',      -- feature|bug|contract|analysis
    status TEXT NOT NULL DEFAULT 'ready',      -- ready|claimed|blocked|done|archived
    parent_id INTEGER,
    depends_on TEXT NOT NULL DEFAULT '[]',     -- JSON list[int]
    artifacts TEXT NOT NULL DEFAULT '{}',      -- JSON dict: name->path
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
    task INTEGER PRIMARY KEY REFERENCES tasks(id),
    worker TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS gates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task INTEGER NOT NULL REFERENCES tasks(id),
    name TEXT NOT NULL,
    passed INTEGER NOT NULL DEFAULT 0,
    evidence TEXT DEFAULT '',                  -- JSON: kanıt detayları
    checked_at REAL,
    checked_by TEXT,
    UNIQUE(task, name)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task INTEGER NOT NULL REFERENCES tasks(id),
    reviewer_profile TEXT NOT NULL,
    verdict TEXT NOT NULL,                     -- APPROVE|CHANGES_REQUESTED
    artifact_path TEXT,
    artifact_sha TEXT,
    code_digest TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task INTEGER NOT NULL REFERENCES tasks(id),
    command TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    report_path TEXT NOT NULL,
    report_sha TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS context_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task INTEGER NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL,                        -- updated|waived
    path TEXT,
    reason TEXT,
    approved_by TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS contract_touches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task INTEGER NOT NULL REFERENCES tasks(id),
    path TEXT NOT NULL,
    owner_domain TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS domains (
    name TEXT PRIMARY KEY,
    profiles TEXT NOT NULL,                    -- JSON list[str]
    depends_on TEXT NOT NULL DEFAULT '[]',
    critical INTEGER NOT NULL DEFAULT 0,
    paths TEXT NOT NULL DEFAULT '[]',
    max_parallel_workers INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task INTEGER,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks(domain, status, priority, id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
"""


def _now() -> float:
    return time.time()


def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---------- görevler ----------

    def create_task(self, domain: str, title: str, body: str = "",
                    kind: str = "feature", depends_on: list[int] | None = None,
                    artifacts: dict | None = None, parent_id: int | None = None,
                    max_attempts: int = 5, priority: int = 0) -> dict:
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tasks(domain,title,body,kind,depends_on,artifacts,"
                "parent_id,max_attempts,priority,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (domain, title, body, kind, json.dumps(depends_on or []),
                 json.dumps(artifacts or {}), parent_id, max_attempts,
                 priority, now, now))
            tid = cur.lastrowid
            self._event(tid, "system", "created", {"domain": domain, "kind": kind})
            self._conn.commit()
        return self.get_task(tid)

    def get_task(self, task_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._row_task(row) if row else None

    def children_of(self, task_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE parent_id=? AND status!='archived'",
            (task_id,)).fetchall()
        return [self._row_task(r) for r in rows]

    def set_artifact(self, task_id: int, name: str, path: str | None) -> None:
        t = self.get_task(task_id)
        arts = t["artifacts"]   # get_task JSON'u zaten çözdü
        arts[name] = path
        with self._lock:
            self._conn.execute("UPDATE tasks SET artifacts=?, updated_at=? WHERE id=?",
                               (json.dumps(arts), _now(), task_id))
            self._conn.commit()

    def claim(self, worker: str, domain: str, lease_seconds: float = 1200) -> dict | None:
        """Ready + bağımlılıkları done task'lardan birini atomik claim et.

        Süresi dolmuş lease'lar geri alınır (attempts++); max_attempts'ı aşan
        task blocked olur (CS10: 5 deneme sonrası pes et). lease_seconds<=0
        anında süresi dolmuş lease demektir ve ilk adımda iade edilir.
        """
        now = _now()
        with self._lock:
            if lease_seconds <= 0:
                # canlıyken alınmış lease'ı hemen düşür: expires'ı geçmişe al
                self._conn.execute(
                    "UPDATE leases SET expires=? WHERE expires>=?", (now, now))
            # süresi dolmuş lease'ları iade et, gerekirse blockla
            stale = self._conn.execute(
                "SELECT t.id, t.attempts, t.max_attempts FROM leases l "
                "JOIN tasks t ON t.id=l.task WHERE l.expires <= ?", (now,)).fetchall()
            for r in stale:
                self._conn.execute("DELETE FROM leases WHERE task=?", (r["id"],))
                if r["attempts"] + 1 >= r["max_attempts"]:
                    self._conn.execute(
                        "UPDATE tasks SET status='blocked', attempts=?, updated_at=? "
                        "WHERE id=?", (r["attempts"] + 1, now, r["id"]))
                    self._event(r["id"], "system", "blocked",
                                {"reason": "max_attempts"})
                else:
                    self._conn.execute(
                        "UPDATE tasks SET status='ready', attempts=?, updated_at=? "
                        "WHERE id=?", (r["attempts"] + 1, now, r["id"]))
            # claim adayı: ready, domain uyan, tüm depends_on done
            rows = self._conn.execute(
                "SELECT * FROM tasks t WHERE t.status='ready' AND t.domain=? "
                "AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.task=t.id) "
                "ORDER BY t.priority DESC, t.id ASC", (domain,)).fetchall()
            for r in rows:
                deps = json.loads(r["depends_on"])
                if deps and not self._all_done(deps):
                    continue
                if r["parent_id"] and self._conn.execute(
                        "SELECT status FROM tasks WHERE id=?",
                        (r["parent_id"],)).fetchone()["status"] != "done":
                    continue
                self._conn.execute(
                    "INSERT INTO leases(task,worker,expires) VALUES(?,?,?)",
                    (r["id"], worker, now + lease_seconds))
                self._conn.execute(
                    "UPDATE tasks SET status='claimed', updated_at=? WHERE id=?",
                    (now, r["id"]))
                self._event(r["id"], worker, "claimed", {"lease": lease_seconds})
                self._conn.commit()
                return self.get_task(r["id"])
            self._conn.commit()
        return None

    def heartbeat(self, worker: str, task_id: int, lease_seconds: float = 1200) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE leases SET expires=? WHERE task=? AND worker=?",
                (_now() + lease_seconds, task_id, worker))
            self._conn.commit()
            return cur.rowcount > 0

    def release(self, worker: str, task_id: int, note: str = "") -> None:
        with self._lock:
            self._conn.execute("DELETE FROM leases WHERE task=? AND worker=?",
                               (task_id, worker))
            self._conn.execute(
                "UPDATE tasks SET status='ready', updated_at=? WHERE id=?",
                (_now(), task_id))
            self._event(task_id, worker, "released", {"note": note})
            self._conn.commit()

    def finish(self, task_id: int, status: str,
               skip_gate_check: bool = False) -> dict:
        """done ancak tüm gate'ler geçerse olur (fail-closed).

        skip_gate_check yalnız sistem işlemleri (ör. DAG testleri) içindir;
        teslim hattında kullanılmaz.
        """
        assert status in ("done", "blocked", "archived")
        if status == "done" and not skip_gate_check:
            from .gates import evaluate_gates  # geç dolaşım
            report = evaluate_gates(self, task_id)
            if not report.all_required_passed:
                failing = [n for n, g in report.gates.items() if not g["passed"]]
                raise GateNotPassed(f"done reddedildi — gate'ler geçmedi: {failing}")
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                (status, _now(), task_id))
            self._conn.execute("DELETE FROM leases WHERE task=?", (task_id,))
            self._event(task_id, "system", status, {})
            self._conn.commit()
        return self.get_task(task_id)

    # ---------- gate kayıtları (kanıt girişleri) ----------

    def record_review(self, task_id: int, reviewer_profile: str,
                      coder_profile: str, verdict: str,
                      artifact_path: str | None, code_digest: str | None = None) -> int:
        if reviewer_profile == coder_profile:
            raise ValueError("reviewer, coder ile aynı profil olamaz (ADR-004)")
        p = Path(artifact_path) if artifact_path else None
        if p and not p.exists():
            raise ValueError("review artefaktı yok — kanıtsız onay kabul edilmez")
        # artefakt HASH'İ GATE anında hesaplanır (kayıt-anı hash, kayıttan
        # önce yazılıp sonradan silinen sahte dosya ile atlatılabilirdi)
        sha = None
        with self._lock:
            # kod yazarını task'a kalıcı işle (gate denetimi için)
            t = self.get_task(task_id)
            if t is not None:
                arts = t["artifacts"]   # get_task zaten dict'e çevirdi
                arts.setdefault("coder_profile", coder_profile)
                self._conn.execute("UPDATE tasks SET artifacts=? WHERE id=?",
                                   (json.dumps(arts), task_id))
            cur = self._conn.execute(
                "INSERT INTO reviews(task,reviewer_profile,verdict,artifact_path,"
                "artifact_sha,code_digest,created_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, reviewer_profile, verdict.upper(), artifact_path,
                 sha, code_digest, _now()))
            self._event(task_id, reviewer_profile, "review",
                        {"verdict": verdict, "sha": sha})
            self._conn.commit()
            return cur.lastrowid

    def record_test_run(self, task_id: int, command: str, exit_code: int,
                        report_path: str) -> int:
        p = Path(report_path)
        if not p.exists():
            raise ValueError("test raporu yok — kanıtsız 'geçti' kabul edilmez")
        # hash KAYIT anında dondurulur; gate anında raporla karşılaştırılır
        # (rapor sonradan silinse/değiştirilse de tahrif yakalanır)
        sha = _sha(str(p))
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO test_runs(task,command,exit_code,report_path,"
                "report_sha,created_at) VALUES(?,?,?,?,?,?)",
                (task_id, command, exit_code, str(p), sha, now))
            self._event(task_id, "system", "test_run",
                        {"exit": exit_code, "sha": sha[:12]})
            self._conn.commit()
            return cur.lastrowid

    def mark_context_updated(self, task_id: int, context_path: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO context_updates(task,kind,path,created_at) "
                "VALUES(?,?,?,?)", (task_id, "updated", context_path, _now()))
            self._event(task_id, "system", "context_updated", {})
            self._conn.commit()

    def mark_context_waived(self, task_id: int, reason: str,
                            approved_by: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO context_updates(task,kind,reason,approved_by,created_at) "
                "VALUES(?,?,?,?,?)", (task_id, "waived", reason, approved_by, _now()))
            self._conn.commit()

    def add_contract_touch(self, task_id: int, path: str, owner_domain: str) -> None:
        d = self.get_domain(owner_domain)
        if d is None:
            raise ValueError(f"bilinmeyen domain: {owner_domain}")
        task = self.get_task(task_id)
        now = _now()
        # etki analizi: sahibi + bağımlı domain'lere inceleme task'ı aç
        # (task'ları INSERT'ler bitince açılır — self._lock reentrant değil)
        affected: set[str] = set()
        if task and owner_domain != task["domain"]:
            affected.add(owner_domain)
        for dep in self.dependents_of(owner_domain):
            if task is None or dep["name"] != task["domain"]:
                affected.add(dep["name"])
        existing = {c["domain"] for c in self.children_of(task_id)}
        to_spawn = sorted(affected - existing)
        with self._lock:
            self._conn.execute(
                "INSERT INTO contract_touches(task,path,owner_domain,created_at) "
                "VALUES(?,?,?,?)", (task_id, path, owner_domain, now))
            self._event(task_id, "system", "contract_touch",
                        {"path": path, "owner": owner_domain})
            for dom in to_spawn:
                self._conn.execute(
                    "INSERT INTO tasks(domain,title,body,kind,parent_id,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (dom, f"contract incelemesi: {path}",
                     f"'{path}' contract'ı değişti (task #{task_id}). "
                     "Etkini raporla; uyum gerekiyorsa task aç.",
                     "contract", task_id, now, now))
            self._conn.commit()

    def set_gate(self, task_id: int, name: str, passed: bool,
                 evidence: dict, checked_by: str = "gatekeeper") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO gates(task,name,passed,evidence,checked_at,checked_by) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(task,name) DO UPDATE SET "
                "passed=excluded.passed, evidence=excluded.evidence, "
                "checked_at=excluded.checked_at, checked_by=excluded.checked_by",
                (task_id, name, int(passed), json.dumps(evidence, ensure_ascii=False),
                 _now(), checked_by))
            self._conn.commit()

    # ---------- domain ----------

    def upsert_domain(self, name: str, profiles: list[str],
                      depends_on: list[str] | None = None, critical: bool = False,
                      paths: list[str] | None = None,
                      max_parallel_workers: int = 1) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO domains(name,profiles,depends_on,critical,paths,"
                "max_parallel_workers) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET profiles=excluded.profiles, "
                "depends_on=excluded.depends_on, critical=excluded.critical, "
                "paths=excluded.paths, "
                "max_parallel_workers=excluded.max_parallel_workers",
                (name, json.dumps(profiles), json.dumps(depends_on or []),
                 int(critical), json.dumps(paths or []), max_parallel_workers))
            self._conn.commit()

    def get_domain(self, name: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM domains WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        return {"name": row["name"], "profiles": json.loads(row["profiles"]),
                "depends_on": json.loads(row["depends_on"]),
                "critical": bool(row["critical"]), "paths": json.loads(row["paths"]),
                "max_parallel_workers": row["max_parallel_workers"]}

    def list_domains(self) -> list[dict]:
        rows = self._conn.execute("SELECT name FROM domains ORDER BY name").fetchall()
        return [self.get_domain(r["name"]) for r in rows]

    def dependents_of(self, name: str) -> list[dict]:
        return [d for d in self.list_domains() if name in d["depends_on"]]

    # ---------- olaylar / istatistik ----------

    def events(self, task_id: int | None = None) -> list[dict]:
        if task_id is None:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY ts DESC LIMIT 500").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE task=? ORDER BY ts", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) c FROM tasks GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    # ---------- yardımcılar ----------

    def _all_done(self, ids: list[int]) -> bool:
        marks = ",".join("?" * len(ids))
        row = self._conn.execute(
            f"SELECT COUNT(*) c FROM tasks WHERE id IN ({marks}) "
            f"AND status='done'", ids).fetchone()
        return row["c"] == len(ids)

    def _event(self, task_id, actor, kind, payload):
        self._conn.execute(
            "INSERT INTO events(task,ts,actor,kind,payload) VALUES(?,?,?,?,?)",
            (task_id, _now(), actor, kind, json.dumps(payload, ensure_ascii=False)))

    @staticmethod
    def _row_task(row) -> dict:
        d = dict(row)
        d["depends_on"] = json.loads(d["depends_on"])
        d["artifacts"] = json.loads(d["artifacts"])
        return d


class GateNotPassed(Exception):
    """Fail-closed: kanıtsız done reddi (ARCHITECTURE §5)."""
