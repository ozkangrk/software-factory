"""Lead beyni — gelen isteği plan DAG'ına çevirir, dispatch eder, gate'ler.

Çalışma: tek döngü (systemd/Job). Her turda:
  1. bekleyen istek varsa lead profili (LLM çağrısı) plan üretir → submit_plan
  2. insan/lead onaylı planlar dispatch'e açık (approve_plan)
  3. ready task'ları domain bütçesine göre worker'a verir (max_parallel)
  4. claimed task'ların gate'lerini tarar; yeşilse done, bayatsa iade
LLM çağrısı `--runner` ile verilir (ör. "claude -p" veya "hermes chat -p lead -q");
boşsa planner pasif kalır (dispatch/gate yine koşar).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time

from .agent_tools import submit_plan, approve_plan
from .gates import evaluate_gates
from .store import Store

PLANNER_PROMPT = """Sen software factory lead agent'ısın. Bu isteği domain \
işlerine bölen JSON plan üret. Yalnız JSON döndür:
{{"title": "...", "tasks": [{{"id": "a", "domain": "<domain>", \
"title": "...", "body": "...", "depends_on": ["<id>"]}}]}}
Bilinmeyen domain kullanma. Domainler: {domains}
İstek: {request}
Bağlam (varsa): {context}
"""


class Lead:
    def __init__(self, store: Store, runner: str = "", max_parallel: int = 2):
        self.store = store
        self.runner = runner
        self.max_parallel = max_parallel

    # --- plan üretimi (LLM) ---
    def plan_request(self, request: str, context: str = "") -> dict | None:
        if not self.runner:
            return None
        domains = ", ".join(d["name"] for d in self.store.list_domains())
        prompt = PLANNER_PROMPT.format(domains=domains, request=request,
                                       context=context[:4000])
        try:
            proc = subprocess.run(self.runner + " " + json.dumps(prompt),
                                  shell=True, capture_output=True, text=True,
                                  timeout=300)
            text = proc.stdout
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end < 0:
                return None
            return json.loads(text[start:end + 1])
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            return None

    # --- dispatch ---
    def active_count(self) -> int:
        row = self.store._conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE status='claimed' "
            "AND domain != 'lead'").fetchone()
        return row["c"]

    def dispatch_once(self) -> list[int]:
        """Ready task'lardan bütçe dahilinde claim edenleri başlatır.

        Worker başlatma runner'a devredilir (Claude Code/Codex); burada
        sadece claim + yönlendirme kararı vardır (Multica senaryosunda
        dispatch'i Multica yapar — bu döngü onun yerini aldığında)."""
        started: list[int] = []
        budget = self.max_parallel - self.active_count()
        if budget <= 0:
            return started
        for domain in self.store.list_domains():
            if budget <= 0:
                break
            if domain["name"] == "lead":     # lead koltuğu worker'a verilmez
                continue
            cap = domain["max_parallel_workers"]
            held = self.store._conn.execute(
                "SELECT COUNT(*) c FROM tasks WHERE domain=? AND status='claimed'",
                (domain["name"],)).fetchone()["c"]
            while held < cap and budget > 0:
                t = self.store.claim(f"worker-{domain['name']}", domain["name"])
                if t is None:
                    break
                started.append(t["id"])
                held += 1
                budget -= 1
        return started

    # --- gate denetimi ---
    def sweep_once(self) -> dict:
        """Claimed task'ların gate'lerini yeniden değerlendirir."""
        rows = self.store._conn.execute(
            "SELECT id, domain FROM tasks WHERE status='claimed'").fetchall()
        result = {"advanced": [], "waiting": []}
        for r in rows:
            if r["domain"] == "lead":        # plan-görevi gate'e tabi değil;
                continue                     # approve_plan sonrası sürer
            report = evaluate_gates(self.store, r["id"])
            if report.all_required_passed:
                self.store.finish(r["id"], "done")
                result["advanced"].append(r["id"])
            else:
                result["waiting"].append(r["id"])
        return result

    def tick(self, request: str | None = None) -> dict:
        out: dict = {}
        if request:
            plan = self.plan_request(request)
            if plan:
                out["plan"] = submit_plan(self.store, plan)
        out["started"] = self.dispatch_once()
        out["sweep"] = self.sweep_once()
        return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="factory-lead")
    ap.add_argument("workspace")
    ap.add_argument("--runner", default="",
                    help='LLM komutu şablonu, ör: "claude -p" ya da '
                         '\'hermes chat -p lead -q\'')
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=20)
    ap.add_argument("--request", default=None, help="tek seferlik istek planla")
    args = ap.parse_args(argv)
    store = Store(f"{args.workspace}/registry.db")
    store._workspace = args.workspace
    lead = Lead(store, runner=args.runner)
    if args.once:
        print(json.dumps(lead.tick(args.request), ensure_ascii=False))
        return
    while True:
        print(json.dumps(lead.tick(), ensure_ascii=False), flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
