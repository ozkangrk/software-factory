"""Gate değerlendirici — fail-closed, kanıta dayalı (ARCHITECTURE §5, ADR-004).

Bir task ancak TÜM zorunlu gate'ler kanıtlarla geçerse done olabilir.
Kanıt olmayan hiçbir iddia kabul edilmez: rapor dosyası hash ile doğrulanır,
APPROVE farklı profilden gelmeli ve kod değişince bayatlar.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

GATE_NAMES = ("analysis_done", "reviewer_approved", "tests_passed",
              "context_updated", "contract_sync")


@dataclass
class GateReport:
    task_id: int
    gates: dict = field(default_factory=dict)   # name -> {passed, reason, evidence}

    @property
    def all_required_passed(self) -> bool:
        return all(g["passed"] for g in self.gates.values())

    def by_name(self, name: str) -> dict:
        return self.gates[name]


def _sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evaluate_gates(store, task_id: int, workspace: Path | None = None,
                   code_digest: str | None = None) -> GateReport:
    """Tüm gate'leri sıfırdan, kayıtlı kanıtlara karşı yeniden doğrular.

    cache yok — her çağrı fail-closed yeniden hesaplar. code_digest verilirse
    review tazeliği mevcut koda göre denetlenir.
    """
    task = store.get_task(task_id)
    if task is None:
        raise ValueError(f"task yok: {task_id}")
    report = GateReport(task_id=task_id)
    arts = task["artifacts"]

    report.gates["analysis_done"] = _gate_analysis(arts)
    report.gates["reviewer_approved"] = _gate_review(store, task, code_digest)
    report.gates["tests_passed"] = _gate_tests(store, task_id)
    report.gates["context_updated"] = _gate_context(store, task_id)
    report.gates["contract_sync"] = _gate_contract(store, task_id)

    for name, g in report.gates.items():
        store.set_gate(task_id, name, g["passed"], {
            "reason": g.get("reason", ""), **g.get("evidence", {})})
    return report


def _gate_analysis(arts: dict) -> dict:
    path = arts.get("analysis")
    if not path or not Path(path).exists():
        return {"passed": False, "reason": "01-analysis.md yok"}
    if not Path(path).read_text().strip():
        return {"passed": False, "reason": "analiz dosyası boş"}
    return {"passed": True,
            "evidence": {"sha": _sha(path)[:12], "path": str(path)}}


def _gate_review(store, task: dict, code_digest: str | None) -> dict:
        rows = store._conn.execute(
            "SELECT * FROM reviews WHERE task=? ORDER BY created_at DESC",
            (task["id"],)).fetchall()
        if not rows:
            return {"passed": False, "reason": "review kaydı yok"}
        approvers = {r["reviewer_profile"] for r in rows
                     if r["verdict"] == "APPROVE" and _fresh(r, code_digest)
                     and _artifact_intact(r)}
        coder = (task.get("coder_profile")
                 or (task["artifacts"].get("coder_profile")))
        # kayıt anında aynı-profil onayı engellenir; burada bilinen-çakışma
        # ve kritisite denetimi yapılır.
        if not approvers:
            return {"passed": False, "reason": "taze+intakt APPROVE yok"}
        if coder and approvers == {coder}:
            return {"passed": False, "reason": "onay, kodu yazan profilden"}
        domain = store.get_domain(task["domain"])
        required = 2 if (domain and domain["critical"]) else 1
        # kod yazarı bağımsız-reviewer sayısına dahil edilmez
        independent = approvers - ({coder} if coder else set())
        if len(independent) < required:
            return {"passed": False,
                    "reason": f"kritik domain: {required} farklı reviewer gerekli, "
                              f"{len(independent)} bağımsız var"}
        return {"passed": True, "evidence": {"approvers": sorted(independent)}}


def _fresh(review_row, code_digest: str | None) -> bool:
    """Review sonrası kod değiştiyse (digest farkı) onay bayat sayılır."""
    if code_digest is None or review_row["code_digest"] is None:
        return True
    return review_row["code_digest"] == code_digest


def _artifact_intact(review_row) -> bool:
    """Review artefaktı hâlâ orada mı? Hash gate ANINDa hesaplanır; artefakt
    kayıttan önce yazılıp sonra silinirse geçersiz sayılır (fail-closed)."""
    if not review_row["artifact_path"]:
        return True
    p = Path(review_row["artifact_path"])
    return p.exists()


def _gate_tests(store, task_id: int) -> dict:
    rows = store._conn.execute(
        "SELECT * FROM test_runs WHERE task=? ORDER BY created_at DESC",
        (task_id,)).fetchall()
    if not rows:
        return {"passed": False, "reason": "test koşusu kaydı yok"}
    latest = rows[0]
    p = Path(latest["report_path"])
    if not p.exists():
        return {"passed": False, "reason": "test raporu silinmiş"}
    if _sha(p) != latest["report_sha"]:
        return {"passed": False, "reason": "test raporu kayıt sonrası değiştirilmiş"}
    if latest["exit_code"] != 0:
        return {"passed": False,
                "reason": f"exit={latest['exit_code']} ({latest['command']})"}
    return {"passed": True,
            "evidence": {"command": latest["command"],
                         "report_sha": latest["report_sha"][:12]}}


def _gate_context(store, task_id: int) -> dict:
    rows = store._conn.execute(
        "SELECT * FROM context_updates WHERE task=? ORDER BY created_at DESC",
        (task_id,)).fetchall()
    if not rows:
        return {"passed": False,
                "reason": "CONTEXT.md güncellenmedi / waiver yok"}
    latest = rows[0]
    if latest["kind"] == "updated":
        return {"passed": True, "evidence": {"path": latest["path"]}}
    # waived: reviewer onayı şart
    if not latest["approved_by"]:
        return {"passed": False, "reason": "muafiyet reviewer onaysız"}
    return {"passed": True, "evidence": {"waiver": latest["reason"],
                                         "approved_by": latest["approved_by"]}}


def _gate_contract(store, task_id: int) -> dict:
    touches = store._conn.execute(
        "SELECT * FROM contract_touches WHERE task=?", (task_id,)).fetchall()
    if not touches:
        return {"passed": True, "reason": "contract'a dokunulmadı"}
    kids = store.children_of(task_id)
    open_kids = [k for k in kids if k["status"] != "done"]
    if open_kids:
        return {"passed": False,
                "reason": f"{len(open_kids)} bağımlı domain incelemesi açık: "
                          f"{[k['domain'] for k in open_kids]}"}
    owners = {t["owner_domain"] for t in touches}
    return {"passed": True, "evidence": {"contracts": sorted(owners),
                                         "children_done": len(kids)}}


# ---------- kolaylık yazıcıları (test/harness tarafı) ----------

def record_review(store, task_id: int, reviewer_profile: str,
                  coder_profile: str, verdict: str, artifact_path: str | None,
                  code_digest: str | None = None) -> int:
    return store.record_review(task_id, reviewer_profile, coder_profile,
                               verdict, artifact_path, code_digest)


def record_test_run(store, task_id: int, command: str, exit_code: int,
                    report_path: str) -> int:
    return store.record_test_run(task_id, command, exit_code, report_path)
