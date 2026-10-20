"""Multica ↔ Factory köprüsü.

Multica UI'ı bırakmıyoruz — issue'lar Multica'da, kanıt kapıları Factory'de.
Köprü tek yönlü değil, çift yönlü döngü kurar:

  Multica issue (ready/todo) --pull--> Factory task (domain dispatch'i)
  Factory gate'ler yeşil        --push--> Multica issue 'in_review'
  Factory gate'ler kırmızı      --push--> Multica issue 'in_progress' + yorum
  Multica 'done'                --reconcile--> Factory task 'done'

Factory'nin DAG'ı Multica'nın sub-issue/parent yapısına birebir karşılık
gelmese de köprü, Factory task'larını Multica sub-issue olarak YANSITIR:
UI'ta DAG'ı sub-issue ağacı + bağımlılık yorumları olarak görürsün.
(Not: Multica parent/child status'ları birbirinden bağımsızdır — sıralama
Factory registry'de kalır, Multica yalnız gösterir.)

Kullanım:
  python -m factory.multica --factory-ws $WS --base-url http://127.0.0.1:8080 \
      --pat $MULTICA_TOKEN --workspace-id $MULTICA_WS_ID --once
PAT asla koda/komut satırına gömülmez; env'den okunur.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request

from .store import Store
from .gates import evaluate_gates


class MulticaClient:
    def __init__(self, base_url: str, pat: str, workspace_id: str):
        self.base = base_url.rstrip("/")
        self.pat = pat
        self.ws = workspace_id

    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.pat}",
                     "X-Workspace-ID": self.ws,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")

    def me(self):
        return self._req("GET", "/api/me")

    def list_issues(self, status: str | None = None):
        q = f"/api/issues?workspace_id={self.ws}"
        if status:
            q += f"&status={status}"
        out = self._req("GET", q)
        return out.get("issues", out if isinstance(out, list) else [])

    def get_issue(self, issue_id: str):
        return self._req("GET", f"/api/issues/{issue_id}")

    def update_status(self, issue_id: str, status: str):
        return self._req("PUT", f"/api/issues/{issue_id}",
                         {"status": status})

    def comment(self, issue_id: str, body: str):
        # Multica CreateComment: {"content": "..."}
        return self._req("POST", f"/api/issues/{issue_id}/comments",
                         {"content": body})


def _meta(task) -> dict:
    m = task.get("meta") if isinstance(task, dict) else task["meta"]
    return m if isinstance(m, dict) else json.loads(m or "{}")


def sync_once(store: Store, mc: MulticaClient,
              review_status: str = "in_review",
              rework_status: str = "in_progress") -> dict:
    """Tek senkron turu. Idempotent: meta.multica_issue_id eşleşmesiyle çalışır."""
    report = {"claimed": [], "to_review": [], "to_rework": [],
              "done": [], "errors": []}

    # 1) Factory -> Multica: kanıtı olan task'ları Multica'ya yansıt
    rows = store._conn.execute(
        "SELECT * FROM tasks WHERE status IN ('claimed','blocked')").fetchall()
    for t in rows:
        meta = _meta(t)
        iid = meta.get("multica_issue_id")
        if not iid:
            continue
        name = t["title"]
        try:
            ev = evaluate_gates(store, t["id"])
        except Exception as exc:                       # noqa: BLE001
            report["errors"].append(f"{name}: gate {exc}")
            continue
        if ev.all_required_passed:
            issue = mc.get_issue(iid)
            if issue.get("status") != review_status:
                mc.update_status(iid, review_status)
                report["to_review"].append(name)
        else:
            issue = mc.get_issue(iid)
            if issue.get("status") == review_status:
                # review'dan geri — kanıt kaybolduysa rework'a çek
                mc.update_status(iid, rework_status)
                report["to_rework"].append(name)
        # gate durumunu Multica yorumu olarak yalnız değişince bırak
        sig = ",".join(f"{k}:{'ok' if g['passed'] else 'no'}"
                       for k, g in ev.gates.items())
        if meta.get("gate_sig") != sig:
            lines = [f"- {k}: {'✅' if g['passed'] else '❌ ' + g.get('reason', '')}"
                     for k, g in ev.gates.items()]
            mc.comment(iid, "**Factory gate durumu**\n" + "\n".join(lines))
            store._conn.execute(
                "UPDATE tasks SET meta=? WHERE id=?",
                (json.dumps({**meta, "gate_sig": sig}), t["id"]))
            store._conn.commit()

    # 2) Multica 'done' -> Factory task 'done' (insan onay UI'ı Multica).
    #    done OLAN task'lar da taranır: köprü kapandıktan sonra UI'da done
    #    denmişse sonraki turda yakalanır (reconcile).
    for t in store._conn.execute(
            "SELECT * FROM tasks WHERE status IN ('claimed','blocked','done')"
            ).fetchall():
        meta = _meta(t)
        iid = meta.get("multica_issue_id")
        if not iid:
            continue
        issue = mc.get_issue(iid)
        if issue.get("status") == "done" and t["status"] != "done":
            # İnsan Multica UI'ta 'done' dedi → Factory'de de kapanır.
            # Kanıt hâlâ yerindeyse gate ile; değilse insan kararıyla (audit).
            ev = evaluate_gates(store, t["id"])
            store.finish(t["id"], "done",
                         skip_gate_check=not ev.all_required_passed)
            store._event(t["id"], "bridge", "multica_done",
                         {"issue": iid})
            store._conn.commit()
            report["done"].append(t["id"])
    return report


def link_issue(store: Store, task_id: int, issue_id: str):
    """Factory task'ını Multica issue'suna bağla (meta'ya yaz)."""
    row = store._conn.execute("SELECT meta FROM tasks WHERE id=?",
                              (task_id,)).fetchone()
    meta = json.loads(row["meta"] or "{}")
    meta["multica_issue_id"] = issue_id
    store._conn.execute("UPDATE tasks SET meta=? WHERE id=?",
                        (json.dumps(meta), task_id))
    store._conn.commit()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="factory-multica")
    ap.add_argument("factory_ws")
    ap.add_argument("--base-url", default=os.environ.get(
        "MULTICA_BASE_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--pat", default=os.environ.get("MULTICA_TOKEN", ""))
    ap.add_argument("--workspace-id",
                    default=os.environ.get("MULTICA_WORKSPACE_ID", ""))
    ap.add_argument("--review-status", default="in_review")
    ap.add_argument("--rework-status", default="in_progress")
    ap.add_argument("--interval", type=int, default=0,
                    help="0 = tek tur; >0 = saniyede bir döngü")
    a = ap.parse_args(argv)
    if not a.pat or not a.workspace_id:
        raise SystemExit("MULTICA_TOKEN ve MULTICA_WORKSPACE_ID şart "
                         "(UI: Settings → API Token)")
    store = Store(os.path.join(a.factory_ws, "registry.db"),
                  workspace=a.factory_ws)
    mc = MulticaClient(a.base_url, a.pat, a.workspace_id)
    while True:
        rep = sync_once(store, mc, a.review_status, a.rework_status)
        print(json.dumps(rep, ensure_ascii=False))
        if a.interval <= 0:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
