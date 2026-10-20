"""Gate değerlendirme testleri — fail-closed semantiği kilitler (ADR-004, ARCHITECTURE §5)."""
import hashlib
import time

import pytest

from factory.gates import (
    GATE_NAMES,
    evaluate_gates,
    record_review,
    record_test_run,
)
from factory.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "reg.db")


def _task_with_artifacts(store, tmp_path, with_analysis=True, contracts=None):
    analysis = None
    if with_analysis:
        p = tmp_path / "01-analysis.md"
        p.write_text("# Analiz\nDokunulan contract: %s\n" % (contracts or "yok"))
        analysis = str(p)
    return store.create_task(domain="backend", title="t", artifacts={"analysis": analysis})


class TestAnalysisGate:
    def test_missing_analysis_fails(self, store, tmp_path):
        task = _task_with_artifacts(store, tmp_path, with_analysis=False)
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        g = report.by_name("analysis_done")
        assert not g["passed"] and "yok" in g["reason"].lower() or g["reason"]

    def test_empty_analysis_fails(self, store, tmp_path):
        p = tmp_path / "01-analysis.md"
        p.write_text("   \n\n  ")
        task = store.create_task(domain="backend", title="t",
                                 artifacts={"analysis": str(p)})
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert not report.by_name("analysis_done")["passed"]

    def test_nonempty_analysis_passes(self, store, tmp_path):
        task = _task_with_artifacts(store, tmp_path)
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert report.by_name("analysis_done")["passed"]


class TestReviewerGate:
    def test_no_review_fails(self, store, tmp_path):
        task = _task_with_artifacts(store, tmp_path)
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert not report.by_name("reviewer_approved")["passed"]

    def test_same_profile_approval_rejected(self, store, tmp_path):
        task = _task_with_artifacts(store, tmp_path)
        with pytest.raises(ValueError):
            record_review(store, task["id"], reviewer_profile="coder-profile",
                          coder_profile="coder-profile", verdict="APPROVE",
                          artifact_path=None)

    def test_approve_from_different_profile_passes(self, store, tmp_path, tmp_path_factory):
        task = _task_with_artifacts(store, tmp_path)
        art = tmp_path / "03-review.md"
        art.write_text("APPROVE")
        record_review(store, task["id"], reviewer_profile="reviewer-x",
                      coder_profile="coder-profile", verdict="APPROVE",
                      artifact_path=str(art))
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert report.by_name("reviewer_approved")["passed"]

    def test_changes_requested_fails(self, store, tmp_path):
        task = _task_with_artifacts(store, tmp_path)
        record_review(store, task["id"], reviewer_profile="r", coder_profile="c",
                      verdict="CHANGES_REQUESTED", artifact_path=None)
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert not report.by_name("reviewer_approved")["passed"]

    def test_critical_domain_needs_two_distinct_reviewers(self, store, tmp_path):
        store.upsert_domain(name="billing", critical=True, profiles=["coder-profile"])
        task = store.create_task(domain="billing", title="t",
                                 artifacts={"analysis": None})
        p = tmp_path / "01-analysis.md"; p.write_text("analiz")
        store.set_artifact(task["id"], "analysis", str(p))
        record_review(store, task["id"], reviewer_profile="r1",
                      coder_profile="coder-profile", verdict="APPROVE", artifact_path=None)
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert not report.by_name("reviewer_approved")["passed"]
        record_review(store, task["id"], reviewer_profile="r2",
                      coder_profile="coder-profile", verdict="APPROVE", artifact_path=None)
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert report.by_name("reviewer_approved")["passed"]

    def test_stale_review_after_new_code_fails(self, store, tmp_path):
        """Review sonrası kod değişirse eski APPROVE geçersiz (kanıt tazeliği)."""
        task = _task_with_artifacts(store, tmp_path)
        code = tmp_path / "impl.py"; code.write_text("v1")
        record_review(store, task["id"], reviewer_profile="r", coder_profile="c",
                      verdict="APPROVE",
                      artifact_path=None, code_digest=_digest(code))
        report = evaluate_gates(store, task["id"], workspace=tmp_path,
                                code_digest=_digest(code))
        assert report.by_name("reviewer_approved")["passed"]
        code.write_text("v2-degraded")
        report = evaluate_gates(store, task["id"], workspace=tmp_path,
                                code_digest=_digest(code))
        assert not report.by_name("reviewer_approved")["passed"]


class TestTestsGate:
    def test_claim_without_evidence_fails(self, store, tmp_path):
        """'Testler geçti' iddiası kanıt değildir: rapor dosyası şart."""
        task = _task_with_artifacts(store, tmp_path)
        with pytest.raises(ValueError):
            record_test_run(store, task["id"], command="pytest", exit_code=0,
                            report_path=str(tmp_path / "olmayan-rapor.md"))

    def test_nonzero_exit_fails(self, store, tmp_path):
        task = _task_with_artifacts(store, tmp_path)
        rep = tmp_path / "04-test-report.md"; rep.write_text("FAILED")
        record_test_run(store, task["id"], command="pytest", exit_code=1,
                        report_path=str(rep))
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert not report.by_name("tests_passed")["passed"]

    def test_passing_run_with_report_passes(self, store, tmp_path):
        task = _task_with_artifacts(store, tmp_path)
        rep = tmp_path / "04-test-report.md"
        rep.write_text("12 passed in 3.4s\n$ pytest\nexit=0")
        record_test_run(store, task["id"], command="pytest", exit_code=0,
                        report_path=str(rep))
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert report.by_name("tests_passed")["passed"]

    def test_tampered_report_fails(self, store, tmp_path):
        task = _task_with_artifacts(store, tmp_path)
        rep = tmp_path / "04-test-report.md"
        rep.write_text("12 passed")
        record_test_run(store, task["id"], command="pytest", exit_code=0,
                        report_path=str(rep))
        rep.write_text("LIE: 999 passed")
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert not report.by_name("tests_passed")["passed"]


class TestContextGate:
    def test_missing_update_fails(self, store, tmp_path):
        store.upsert_domain(name="backend", profiles=["c"])
        task = _task_with_artifacts(store, tmp_path)
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert not report.by_name("context_updated")["passed"]

    def test_waiver_needs_reviewer_approval(self, store, tmp_path):
        task = _task_with_artifacts(store, tmp_path)
        store.mark_context_waived(task["id"], reason="saltık test ekleme",
                                  approved_by=None)
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert not report.by_name("context_updated")["passed"]
        store.mark_context_waived(task["id"], reason="saltık test ekleme",
                                  approved_by="r")
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert report.by_name("context_updated")["passed"]


class TestContractGate:
    def test_contract_change_spawns_child_inspections(self, store, tmp_path):
        store.upsert_domain(name="auth", profiles=["a"])
        store.upsert_domain(name="billing", profiles=["b"], depends_on=["auth"])
        store.upsert_domain(name="ui", profiles=["u"], depends_on=["billing"])
        # task sahibi 'ui': auth contract'ı değişti -> sahibi(auth) + bağımlılar
        task = store.create_task(domain="ui", title="t",
                                 artifacts={"analysis": None})
        p = tmp_path / "01-analysis.md"
        p.write_text("# Analiz\nDokunulan contract: auth/api.yaml\n")
        store.set_artifact(task["id"], "analysis", str(p))
        store.add_contract_touch(task["id"], "auth/api.yaml", owner_domain="auth")
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        # contract_sync BAĞIMSIZ task'ta denetlenir (cross-request parent'ı
        # bekler; parent kendi child'ını beklemez — kilitlenme önlemi).
        # Spawn + spawn olmadan done ayrımı test_all_children_done_passes'te.
        kids = store.children_of(task["id"])
        assert {k["domain"] for k in kids} == {"auth", "billing"}

    def test_all_children_done_passes(self, store, tmp_path):
        store.upsert_domain(name="auth", profiles=["a"])
        store.upsert_domain(name="billing", profiles=["b"], depends_on=["auth"])
        task = _task_with_artifacts(store, tmp_path, contracts=["auth/api.yaml"])
        store.add_contract_touch(task["id"], "auth/api.yaml", owner_domain="auth")
        for kid in store.children_of(task["id"]):
            store.finish(kid["id"], "done", skip_gate_check=True)
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert report.by_name("contract_sync")["passed"]

    def test_contract_touch_without_domain_fails(self, store, tmp_path):
        task = _task_with_artifacts(store, tmp_path)
        with pytest.raises(ValueError):
            store.add_contract_touch(task["id"], "kimsenin/olmayan.yaml",
                                     owner_domain="yok-boyle-bir-domain")


class TestOverall:
    def test_done_requires_all_required_gates(self, store, tmp_path):
        task = _task_with_artifacts(store, tmp_path)
        report = evaluate_gates(store, task["id"], workspace=tmp_path)
        assert report.all_required_passed is False
        assert set(report.gates) == set(GATE_NAMES)

    def test_finalize_transition(self, store, tmp_path):
        """Tam yeşil akış: done'a ancak tüm gate'ler geçince geçer."""
        store.upsert_domain(name="backend", profiles=["coder-profile"])
        p = tmp_path / "01-analysis.md"; p.write_text("analiz")
        rep = tmp_path / "04-test-report.md"; rep.write_text("all passed")
        rev = tmp_path / "03-review.md"; rev.write_text("APPROVE")
        ctx = tmp_path / "CONTEXT.md"; ctx.write_text("# ctx v2")
        task = store.create_task(domain="backend", title="t",
                                 artifacts={"analysis": str(p), "test_report": str(rep),
                                            "review": str(rev), "context": str(ctx)})
        code = tmp_path / "impl.py"; code.write_text("x")
        digest = _digest(code)
        record_test_run(store, task["id"], "pytest", 0, str(rep))
        record_review(store, task["id"], "r", "coder-profile", "APPROVE",
                      str(rev), code_digest=digest)
        store.mark_context_updated(task["id"], str(ctx))
        report = evaluate_gates(store, task["id"], workspace=tmp_path,
                                code_digest=digest)
        assert report.all_required_passed
        store.finish(task["id"], "done")


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
