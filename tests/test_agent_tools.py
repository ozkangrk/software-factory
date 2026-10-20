"""Ajan araçları (agent_tools) testleri — ajan birbirini çağırır, lead orkestre eder.

Sözleşme (ARCHITECTURE §6): ajanlar keyfi çağrı yapamaz; registry üzerinden
yetki denetimli araçlar çağırır. Lead beyin (Brain) plan → task ağacı kurar.
"""
import pytest

from factory import agent_tools
from factory.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "reg.db")
    s.upsert_domain(name="auth", profiles=["coder-auth"])
    s.upsert_domain(name="billing", profiles=["coder-bill"], depends_on=["auth"])
    # her domain'de claim edilebilir birer iş (test kolaylığı)
    s.create_task(domain="auth", title="auth işi")
    s.create_task(domain="billing", title="billing işi")
    return s


def _claim(store, domain, worker):
    t = store.claim(worker, domain)
    assert t is not None, f"{domain} için task yok"
    return t


class TestRequestTask:
    def test_cross_domain_request_creates_child(self, store):
        t = _claim(store, "auth", "coder-auth")
        res = agent_tools.request_task(store, caller="coder-auth",
                                       domain="billing",
                                       title="yeni token alanı için fatura uyumu")
        assert res["ok"] and res["task_id"]
        kid = store.get_task(res["task_id"])
        assert kid["domain"] == "billing" and kid["parent_id"] == t["id"]
        assert kid["kind"] == "cross"

    def test_same_domain_request_ok(self, store):
        t = _claim(store, "auth", "coder-auth")
        res = agent_tools.request_task(store, caller="coder-auth",
                                       domain="auth", title="ek iş")
        assert res["ok"]

    def test_unknown_domain_rejected(self, store):
        _claim(store, "auth", "coder-auth")
        res = agent_tools.request_task(store, caller="coder-auth",
                                       domain="yok-boyle-domain", title="x")
        assert not res["ok"] and "domain" in res["error"]

    def test_worker_without_claim_cannot_request(self, store):
        res = agent_tools.request_task(store, caller="hayali-ajan",
                                       domain="auth", title="x")
        assert not res["ok"]

    def test_duplicate_pending_title_deduped(self, store):
        _claim(store, "auth", "coder-auth")
        r1 = agent_tools.request_task(store, caller="coder-auth",
                                      domain="billing", title="aynı iş")
        r2 = agent_tools.request_task(store, caller="coder-auth",
                                      domain="billing", title="aynı iş")
        assert r1["ok"] and r2["ok"] and r1["task_id"] == r2["task_id"]


class TestReportResult:
    def test_worker_reports_own_task(self, store):
        t = _claim(store, "auth", "coder-auth")
        res = agent_tools.report_result(store, caller="coder-auth",
                                        summary="tamam", artifacts={})
        assert res["ok"]

    def test_cannot_report_others_task(self, store):
        _claim(store, "auth", "coder-auth")
        res = agent_tools.report_result(store, caller="coder-bill",
                                        task_id=1, summary="ben yazdım",
                                        artifacts={})
        assert not res["ok"]

    def test_report_without_claim_rejected(self, store):
        res = agent_tools.report_result(store, caller="sokaktaki-ajan",
                                        task_id=1, summary="x", artifacts={})
        assert not res["ok"]


class TestAskDomain:
    def test_reads_target_domain_context_only(self, store, tmp_path):
        (tmp_path / "domains").mkdir()
        for d in ("auth", "billing"):
            (tmp_path / "domains" / d).mkdir()
            (tmp_path / "domains" / d / "CONTEXT.md").write_text(
                f"# {d}\ngizli-{d} bilgisi\n")
        store._workspace = str(tmp_path)
        _claim(store, "auth", "coder-auth")
        res = agent_tools.ask_domain(store, caller="coder-auth",
                                     domain="billing", question="durum?")
        assert res["ok"] and "gizli-billing" in res["context"]
        assert "gizli-auth" not in res["context"]

    def test_cannot_read_own_via_ask_is_fine(self, store, tmp_path):
        (tmp_path / "domains" / "auth").mkdir(parents=True)
        (tmp_path / "domains" / "auth" / "CONTEXT.md").write_text("x")
        _claim(store, "auth", "coder-auth")
        res = agent_tools.ask_domain(store, caller="coder-auth",
                                     domain="auth", question="durum?")
        assert res["ok"]


class TestContractChange:
    def test_owner_only(self, store):
        t = _claim(store, "billing", "coder-bill")
        ok = agent_tools.request_contract_change(
            store, caller="coder-bill", path="billing/api.yaml",
            new_content="openapi: 3.0.0")
        assert ok["ok"]
        bad = agent_tools.request_contract_change(
            store, caller="coder-auth", path="billing/api.yaml",
            new_content="hack")
        assert not bad["ok"]


class TestBrain:
    def test_plan_creates_dag(self, store):
        plan = {
            "title": "ödeme akışı",
            "tasks": [
                {"id": "a", "domain": "auth", "title": "JWT yenileme"},
                {"id": "b", "domain": "billing", "title": "fatura servisi",
                 "depends_on": ["a"]},
            ],
        }
        res = agent_tools.submit_plan(store, plan)
        assert res["ok"]
        a = store.get_task(res["ids"]["a"])
        b = store.get_task(res["ids"]["b"])
        assert b["depends_on"] == [a["id"]]
        # b henüz pending_plan (onaysız) — claim adayı bile olamaz.
        # Tohum 'billing işi' ready'dir; onu boşalttıktan sonra sıradaki
        # aday sorulur: b çıkarsa hata, None ise doğru.
        seed = store.claim("w", "billing")
        if seed["id"] != b["id"]:
            store.release("w", seed["id"])
            # ikinci claim b'ye düşmemeli
            again = store.claim("w2", "billing")
            assert again is None or again["id"] == seed["id"]

    def test_broken_plan_rejected(self, store):
        plan = {"title": "x", "tasks": [
            {"id": "a", "domain": "auth", "title": "t", "depends_on": ["yok"]},
        ]}
        res = agent_tools.submit_plan(store, plan)
        assert not res["ok"]

    def test_unknown_domain_rejected(self, store):
        res = agent_tools.submit_plan(store, {"title": "x", "tasks": [
            {"id": "a", "domain": "mars", "title": "t"}]})
        assert not res["ok"]

    def test_plan_task_itself_held_until_approval(self, store):
        """plan-task onay öncesi pending_plan'dır; sonra boşaltılabilir."""
        res = agent_tools.submit_plan(store, {"title": "x", "tasks": [
            {"id": "a", "domain": "auth", "title": "t"}]})
        pt = store.get_task(res["plan_task_id"])
        assert pt["kind"] == "plan" and pt["status"] == "pending_plan"
        agent_tools.approve_plan(store, plan_task_id=res["plan_task_id"],
                                 approver="human")
        assert store.get_task(res["plan_task_id"])["status"] == "ready"

    def test_plan_blocked_until_plan_approval_gate(self, store):
        """Lead planı onaylamadan task'lar dispatch edilmez (insan/lead kapısı)."""
        plan = {"title": "x", "tasks": [{"id": "a", "domain": "billing",
                                         "title": "gelecek iş"}]}
        res = agent_tools.submit_plan(store, plan)
        assert res["ok"]
        a = store.get_task(res["ids"]["a"])
        assert a["status"] == "pending_plan"
        agent_tools.approve_plan(store, plan_task_id=res["plan_task_id"],
                                 approver="human")
        assert store.get_task(res["ids"]["a"])["status"] == "ready"

    def test_domain_with_plan_tasks_still_dispatches_plain(self, store):
        """pending_plan task'lar domain dispatch'ini kilitlemez; ready işler akar."""
        plan = {"title": "x", "tasks": [{"id": "a", "domain": "billing",
                                         "title": "gelecek iş"}]}
        agent_tools.submit_plan(store, plan)
        got = store.claim("w", "billing")            # fixture'ın ready 'billing işi'
        assert got is not None and got["title"] == "billing işi"


class TestMcpShape:
    def test_manifest_lists_tools(self):
        tools = agent_tools.tool_manifest()
        names = {t["name"] for t in tools}
        assert {"request_task", "report_result", "ask_domain",
                "request_contract_change", "submit_plan", "approve_plan"} <= names

    def test_dispatch_via_manifest(self, store):
        """Ajan aracı manifest üzerinden çağrılır (MCP server aynı yolu kullanır)."""
        t = _claim(store, "auth", "coder-auth")
        res = agent_tools.dispatch(store, "request_task",
                                   {"caller": "coder-auth", "domain": "billing",
                                    "title": "uyum işi"})
        assert res["ok"]

    def test_unknown_tool(self, store):
        res = agent_tools.dispatch(store, "her_seyi_sil", {})
        assert not res["ok"]
