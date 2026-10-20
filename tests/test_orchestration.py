"""Uçtan uca orkestrasyon testi — sahte runner'la lead + ajan çağrıları.

Gerçek LLM yerine deterministik sahte runner (PLANNER_OUT) kullanılır;
Lead'in plan→onay→dispatch→gate döngüsü ve MCP stdio protokolü doğrulanır.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from factory.agent_tools import approve_plan, dispatch
from factory.lead import Lead
from factory.mcp import _call
from factory.store import Store

PLAN = {"title": "ödeme akışı", "tasks": [
    {"id": "a", "domain": "auth", "title": "JWT yenileme uçları", "body": "",
     "depends_on": []},
    {"id": "b", "domain": "billing", "title": "fatura servisi", "body": "",
     "depends_on": ["a"]},
]}


@pytest.fixture()
def world(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    st = Store(ws / "registry.db", workspace=ws)
    st.upsert_domain(name="lead", profiles=["lead-brain"], max_parallel_workers=1)
    st.upsert_domain(name="auth", profiles=["coder-auth"])
    st.upsert_domain(name="billing", profiles=["coder-bill"], depends_on=["auth"])
    for d in ("auth", "billing"):
        (ws / "domains" / d).mkdir(parents=True)
        (ws / "domains" / d / "CONTEXT.md").write_text(f"# {d}\ntuzak: yok\n")
    return st, ws


def test_lead_plan_approve_dispatch_gate(world):
    st, ws = world
    lead = Lead(st, runner="", max_parallel=2)

    # lead planı kurar (runner'sız doğrudan submit)
    res = dispatch(st, "submit_plan", {"plan": PLAN})
    assert res["ok"]
    # dispatch onaysız hiçbir şey başlatmaz
    assert lead.dispatch_once() == []
    # onay sonrası iki iş de hazır
    approve_plan(st, plan_task_id=res["plan_task_id"], approver="human")
    started = lead.dispatch_once()
    assert len(started) == 1                       # b, a'ya bağımlı — bekler

    # a worker'ı: dispatch_once a'yı worker-auth'a verdi (auth domain'inin
    # sabit worker'ı); cross çağrı aynı caller kimliğiyle yapılır.
    a = st._conn.execute(
        "SELECT id FROM tasks WHERE title LIKE 'JWT%'").fetchone()["id"]
    lease = st._conn.execute(
        "SELECT worker FROM leases WHERE task=?", (a,)).fetchone()
    assert lease is not None and lease["worker"] == "worker-auth"
    p = ws / f"{a}-analysis.md"; p.write_text("analiz: uçlar eklenecek")
    st.set_artifact(a, "analysis", str(p))
    cross = dispatch(st, "request_task",
                     {"caller": f"worker-auth", "domain": "billing",
                      "title": "token-fatura sözleşmesi"})
    assert cross["ok"]
    tr = ws / f"{a}-tests.md"; tr.write_text("$ pytest\nexit=0\nok")
    st.record_test_run(a, "pytest", 0, str(tr))
    rev = ws / f"{a}-review.md"; rev.write_text("APPROVE")
    st.record_review(a, "reviewer-x", "coder-auth", "APPROVE", str(rev))
    st.mark_context_updated(a, str(ws / "domains/auth/CONTEXT.md"))
    # contract touch: billing bağımlılığı cross child'i besler.
    # a'nın kendi domain'i auth olduğu için sahibi-açılımı yok; cross
    # child'i contract gate'i besler (cross de inceleme sayılır).
    st.add_contract_touch(a, "auth/tokens.yaml", owner_domain="auth")

    sweep = lead.sweep_once()
    assert a in sweep["advanced"]                  # gate'ler yeşil → done

    # a done oldu. b cross child'e (4) bağımlı; claim ısmarlanmış sırayı
    # garanti etmez — önce cross child'i doğrudan ilerlet, sonra b gelsin.
    cross_kid = st._conn.execute(
        "SELECT id FROM tasks WHERE kind='cross'").fetchone()["id"]
    st.finish(cross_kid, "done", skip_gate_check=True)
    got_b = st.claim("worker-billing", "billing")
    assert got_b is not None and got_b["title"] == "fatura servisi"

    # b ilerler: kanıtlarını koy, sweep done etsin
    b = st._conn.execute(
        "SELECT id FROM tasks WHERE title='fatura servisi'").fetchone()["id"]
    pb = ws / f"{b}-analysis.md"; pb.write_text("analiz b")
    st.set_artifact(b, "analysis", str(pb))
    trb = ws / f"{b}-tests.md"; trb.write_text("ok")
    st.record_test_run(b, "pytest", 0, str(trb))
    rvb = ws / f"{b}-review.md"; rvb.write_text("APPROVE")
    st.record_review(b, "reviewer-y", "coder-bill", "APPROVE", str(rvb))
    st.mark_context_updated(b, str(ws / "domains/billing/CONTEXT.md"))
    assert b in lead.sweep_once()["advanced"]


def test_lead_plan_with_fake_llm_runner(world, tmp_path):
    st, ws = world
    fake = tmp_path / "fakeplanner.sh"
    fake.write_text("#!/bin/sh\ncat <<'EOF'\n"
                    "gürültü öncesi " + json.dumps(PLAN) + " gürültü sonrası\nEOF\n")
    fake.chmod(0o755)
    lead = Lead(st, runner=str(fake), max_parallel=2)
    out = lead.tick(request="ödeme akışını yap")
    assert out["plan"]["ok"]
    assert set(out["plan"]["ids"]) == {"a", "b"}


def test_mcp_stdio_protocol(world):
    st, ws = world
    init = _call(st, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "factory"
    lst = _call(st, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in lst["result"]["tools"]}
    assert "request_task" in names
    # claim yoksa yetki hatası döner (isError=True)
    call = _call(st, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "request_task",
                                 "arguments": {"caller": "hayalet",
                                               "domain": "auth",
                                               "title": "x"}}})
    assert call["result"]["isError"] is True
    # gerçek akış: tohum task + claim + çağrı
    st.create_task(domain="auth", title="tohum")
    st.claim("coder-auth", "auth")
    call2 = _call(st, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "request_task",
                                  "arguments": {"caller": "coder-auth",
                                                "domain": "billing",
                                                "title": "uyum"}}})
    assert call2["result"]["isError"] is False
    body = json.loads(call2["result"]["content"][0]["text"])
    assert body["ok"]
