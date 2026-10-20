"""Store testleri — şema, claim/heartbeat (CS10 kalıbı), domain registry, DAG."""
import time

import pytest

from factory.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "reg.db")


class TestTasks:
    def test_create_and_get(self, store):
        t = store.create_task(domain="backend", title="api ekle",
                              body="detay", kind="feature")
        got = store.get_task(t["id"])
        assert got["domain"] == "backend" and got["status"] == "ready"
        assert got["kind"] == "feature"

    def test_claim_only_ready(self, store):
        t = store.create_task(domain="backend", title="t")
        claimed = store.claim("w1", "backend")
        assert claimed and claimed["id"] == t["id"]
        assert store.claim("w2", "backend") is None  # ready kalmadı

    def test_claim_wrong_domain_none(self, store):
        store.create_task(domain="backend", title="t")
        assert store.claim("w1", "frontend") is None

    def test_expired_lease_reclaimed(self, store):
        store.create_task(domain="backend", title="t")
        # lease'i doğrudan geçmişe al (sleep'ten bağımsız, deterministik)
        store.claim("w1", "backend", lease_seconds=1200)
        store._conn.execute("UPDATE leases SET expires=0")
        store._conn.commit()
        # stale taraması attempts'ı 1'e çıkarır; worker'ın kendisi 2. denemedir
        got = store.claim("w2", "backend")
        assert got is not None
        assert got["attempts"] == 1   # stale reclaim sayacı

    def test_heartbeat_extends(self, store):
        t = store.create_task(domain="backend", title="t")
        store.claim("w1", "backend", lease_seconds=0.05)
        store.heartbeat("w1", t["id"], lease_seconds=30)
        time.sleep(0.06)
        assert store.claim("w2", "backend") is None  # lease hâlâ canlı

    def test_max_attempts_blocks(self, store):
        store.create_task(domain="backend", title="t", max_attempts=2)
        store.claim("w1", "backend", lease_seconds=1200)
        store._conn.execute("UPDATE leases SET expires=0")
        store._conn.commit()
        store.claim("w2", "backend", lease_seconds=1200)
        store._conn.execute("UPDATE leases SET expires=0")
        store._conn.commit()
        assert store.claim("w3", "backend") is None
        t = store._conn.execute("SELECT status, attempts FROM tasks").fetchone()
        assert t["status"] == "blocked" and t["attempts"] == 2

    def test_dag_blocks_children(self, store):
        p = store.create_task(domain="backend", title="parent")
        c = store.create_task(domain="backend", title="child",
                              depends_on=[p["id"]])
        # parent ready değilken child claimed olamaz: claim listesi boş
        assert store.claim("w1", "backend")["id"] == p["id"]
        store.finish(p["id"], "done", skip_gate_check=True)
        assert store.claim("w1", "backend")["id"] == c["id"]


class TestDomains:
    def test_upsert_and_get(self, store):
        store.upsert_domain(name="billing", profiles=["coder-b"],
                            depends_on=["auth"], critical=True,
                            paths=["src/billing/**"])
        d = store.get_domain("billing")
        assert d["critical"] and d["depends_on"] == ["auth"]
        assert d["profiles"] == ["coder-b"]

    def test_unknown_domain_none(self, store):
        assert store.get_domain("yok") is None

    def test_dependents_reverse_lookup(self, store):
        store.upsert_domain(name="auth", profiles=["a"])
        store.upsert_domain(name="billing", profiles=["b"], depends_on=["auth"])
        store.upsert_domain(name="ui", profiles=["u"], depends_on=["auth"])
        assert {d["name"] for d in store.dependents_of("auth")} == {"billing", "ui"}


class TestEvents:
    def test_events_recorded(self, store):
        t = store.create_task(domain="backend", title="t")
        store.claim("w1", "backend")
        evs = store.events(t["id"])
        kinds = [e["kind"] for e in evs]
        assert "created" in kinds and "claimed" in kinds
