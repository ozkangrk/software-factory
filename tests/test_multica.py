"""Multica köprüsü testleri — gerçek HTTP'ye çıkmadan sahte sunucuyla."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from factory.multica import MulticaClient, link_issue, sync_once
from factory.store import Store

# --- sahte Multica ---------------------------------------------------------

ISSUES = {}   # id -> issue dict
COMMENTS = []  # (issue_id, body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/issues/"):
            iid = self.path.rsplit("/", 1)[-1]
            if iid in ISSUES:
                return self._send(200, ISSUES[iid])
            return self._send(404, {"error": "yok"})
        return self._send(404, {"error": "bilinmeyen"})

    def do_PUT(self):
        iid = self.path.split("/")[3]
        n = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(n))
        if iid not in ISSUES:
            return self._send(404, {"error": "yok"})
        ISSUES[iid]["status"] = body["status"]
        return self._send(200, ISSUES[iid])

    def do_POST(self):
        parts = self.path.split("/")
        iid, n = parts[3], int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(n))
        if iid not in ISSUES:
            return self._send(404, {"error": "yok"})
        COMMENTS.append((iid, body["content"]))
        return self._send(200, {"ok": True})


@pytest.fixture()
def fake_multica():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    ISSUES.clear(); COMMENTS.clear()
    yield srv
    srv.shutdown()


@pytest.fixture()
def store(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    st = Store(ws / "r.db", workspace=ws)
    st.upsert_domain(name="web", profiles=["coder"])
    (ws / "domains" / "web").mkdir(parents=True)
    (ws / "domains" / "web" / "CONTEXT.md").write_text("# ctx\n")
    return st, ws


def _mk_task(st, title="blog listesi"):
    tid = st._insert_task_raw("web", title, status="claimed")
    return tid


def _client(fake_multica):
    port = fake_multica.server_address[1]
    return MulticaClient(f"http://127.0.0.1:{port}", "mul_test", "ws-1")


def test_green_gates_push_to_review(store, fake_multica):
    st, ws = store
    tid = _mk_task(st)
    ISSUES["iss-1"] = {"id": "iss-1", "status": "in_progress"}
    link_issue(st, tid, "iss-1")
    # kanıtlar
    p = ws / "01-analysis.md"; p.write_text("analiz")
    st.set_artifact(tid, "analysis", str(p))
    tr = ws / "t.md"; tr.write_text("ok"); st.record_test_run(tid, "pytest", 0, str(tr))
    rv = ws / "r.md"; rv.write_text("APPROVE")
    st.record_review(tid, "rv", "other-profile", "APPROVE", str(rv))
    st.mark_context_updated(tid, str(ws / "domains/web/CONTEXT.md"))
    mc = _client(fake_multica)
    rep = sync_once(st, mc)
    assert rep["to_review"] == ["blog listesi"]
    assert ISSUES["iss-1"]["status"] == "in_review"
    assert COMMENTS and "Factory gate durumu" in COMMENTS[0][1]
    # ikinci tur: aynı imza -> tekrar yorum YOK (idempotent)
    n_before = len(COMMENTS)
    sync_once(st, mc)
    assert len(COMMENTS) == n_before


def test_red_gates_do_not_reach_review(store, fake_multica):
    st, ws = store
    tid = _mk_task(st, title="eksik iş")
    ISSUES["iss-2"] = {"id": "iss-2", "status": "in_progress"}
    link_issue(st, tid, "iss-2")
    mc = _client(fake_multica)
    sync_once(st, mc)
    assert ISSUES["iss-2"]["status"] == "in_progress"   # in_review YOK
    # gate tablosu yorumu düşer, kırmızı göründüğü yerden anlaşılsın
    assert any("❌" in c for _, c in COMMENTS)


def test_review_falls_back_when_evidence_lost(store, fake_multica):
    st, ws = store
    tid = _mk_task(st, title="kaybolan kanıt")
    ISSUES["iss-3"] = {"id": "iss-3", "status": "in_review"}
    link_issue(st, tid, "iss-3")
    mc = _client(fake_multica)
    sync_once(st, mc)
    assert ISSUES["iss-3"]["status"] == "in_progress"   # geri çekildi


def test_human_done_in_multica_closes_factory_task(store, fake_multica):
    st, ws = store
    tid = _mk_task(st, title="insan onayı")
    st.finish(tid, "blocked", skip_gate_check=True)  # blocked olarak başlasın
    ISSUES["iss-4"] = {"id": "iss-4", "status": "in_review"}
    link_issue(st, tid, "iss-4")
    ISSUES["iss-4"]["status"] = "done"                # insan UI'da done dedi
    mc = _client(fake_multica)
    rep = sync_once(st, mc)
    assert tid in rep["done"]
    assert st.get_task(tid)["status"] == "done"
    ev = [e for e in st._conn.execute(
        "SELECT kind FROM events WHERE task=?", (tid,))]
    types = [e["kind"] for e in ev]
    assert "multica_done" in types
    assert "gate_bypass" in types     # kanıtsız done → iz bırakmalı


def test_unlinked_tasks_ignored(store, fake_multica):
    st, ws = store
    _mk_task(st, "bağlantısız")
    mc = _client(fake_multica)
    rep = sync_once(st, mc)
    assert rep == {"claimed": [], "to_review": [], "to_rework": [],
                   "done": [], "errors": []}
