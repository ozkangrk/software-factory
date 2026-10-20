"""agent_tools — ajanların birbirini çağırabildiği, yetki denetimli araç katmanı.

Felsefe (ARCHITECTURE §6): ajanlar arası serbest chat YOK. Her çağrı registry
üzerinden, kimlik (caller) ve yetki (claim/ownership) denetimiyle geçer.
Lead beyin (Brain) da aynı araç setini kullanır — imtiyazı sadece plan
kurmakta; kanıtlar yine gate'lerden geçer.

Çağrı akışı: worker `worker_id` ile claim alır → claim'i süresince o task'ın
sahibidir → araçlar caller'ın aktif claim'ine bakarak yetki verir.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .store import Store


# ---------- kimlik / yetki yardımcıları ----------

def _active_task(store: Store, caller: str) -> dict | None:
    """caller'ın (worker) şu an elindeki task; lease süresi dolmadıysa."""
    row = store._conn.execute(
        "SELECT t.* FROM leases l JOIN tasks t ON t.id=l.task "
        "WHERE l.worker=? AND l.expires > ?", (caller, time.time())).fetchone()
    if not row:
        return None
    return store._row_task(row)


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def _ok(**kw) -> dict:
    return {"ok": True, **kw}


# ---------- ajan araçları ----------

def request_task(store: Store, caller: str, domain: str, title: str,
                 body: str = "") -> dict:
    """Aktif task'tan başka domain'e (veya aynı domain'e) çocuk iş iste.

    Yetki: caller'ın aktif claim'i olmalı. Domain tanınmış olmalı.
    Aynı parent altında aynı başlık dedup edilir (spam koruması).
    """
    me = _active_task(store, caller)
    if me is None:
        return _err(f"aktif claim yok: {caller} — önce task al (claim)")
    if store.get_domain(domain) is None:
        return _err(f"bilinmeyen domain: {domain}")
    for kid in store.children_of(me["id"]):
        if kid["domain"] == domain and kid["title"] == title:
            return _ok(task_id=kid["id"], deduped=True)
    t = store.create_task(domain=domain, title=title, body=body,
                          kind="cross", parent_id=me["id"])
    return _ok(task_id=t["id"])


def report_result(store: Store, caller: str, summary: str,
                  artifacts: dict[str, str], task_id: int | None = None) -> dict:
    """Sonucunu kaydet. task_id verilmezse caller'ın aktif taskına yazılır;
    verildiyse yine aktif claim'i olmak ZORUNDA (kimsenin işine yazılamaz)."""
    me = _active_task(store, caller)
    if me is None:
        return _err(f"aktif claim yok: {caller}")
    target = me["id"] if task_id is None else task_id
    if target != me["id"]:
        return _err("başka task'a rapor yazılamaz")
    for name, path in (artifacts or {}).items():
        store.set_artifact(target, name, path)
    store._event(target, caller, "reported", {"summary": summary[:2000]})
    return _ok(task_id=target)


def ask_domain(store: Store, caller: str, domain: str, question: str) -> dict:
    """Bir domain'in CONTEXT.md + contracts/ içeriğini oku (salt-okur).

    Yetki: aktif claim. Soru olay geçmişine yazılır (denetlenebilirlik).
    Hindsight köprüsü ilerlenecek: şimdilik diskteki sahipli context.
    """
    me = _active_task(store, caller)
    if me is None:
        return _err(f"aktif claim yok: {caller}")
    d = store.get_domain(domain)
    if d is None:
        return _err(f"bilinmeyen domain: {domain}")
    from pathlib import Path
    ws = Path(getattr(store, "_workspace", "."))
    root = ws / "domains" / domain
    ctx = (root / "CONTEXT.md").read_text() if (root / "CONTEXT.md").exists() else ""
    contracts = {}
    cdir = root / "contracts"
    if cdir.is_dir():
        contracts = {p.name: p.read_text() for p in sorted(cdir.iterdir())}
    store._event(me["id"], caller, "ask_domain",
                 {"domain": domain, "question": question[:500]})
    store._conn.commit()
    return _ok(domain=domain, context=ctx, contracts=contracts)


def request_contract_change(store: Store, caller: str, path: str,
                            new_content: str) -> dict:
    """Contract dosyasını SADECE sahibi domain'in aktif worker'ı değiştirir.
    Değişiklik anında contract_touch tetiklenir → bağımlı domain'lere
    inceleme task'ları otomatik açılır."""
    me = _active_task(store, caller)
    if me is None:
        return _err(f"aktif claim yok: {caller}")
    owner = path.split("/", 1)[0]
    d = store.get_domain(owner)
    if d is None:
        return _err(f"bilinmeyen contract sahibi: {owner}")
    if me["domain"] != owner:
        return _err(f"'{path}' contract'ı yalnız {owner} domain'i tarafından "
                    "değiştirilebilir")
    from pathlib import Path
    ws = Path(getattr(store, "_workspace", "."))
    target = ws / "domains" / owner / "contracts" / path.split("/", 1)[1]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_content)
    store.add_contract_touch(me["id"], path, owner_domain=owner)
    kids = [(k["id"], k["domain"]) for k in store.children_of(me["id"])
            if k["kind"] == "contract"]
    return _ok(path=str(target), spawned=kids)


# ---------- Lead beyin: plan → DAG ----------

def submit_plan(store: Store, plan: dict) -> dict:
    """Lead planı: {title, tasks:[{id, domain, title, body?, depends_on?[id]}]}.

    Task'lar 'pending_plan' olarak doğar — plan_onay gate'i geçmeden ready
    olmaz ve dispatch edilmez. Böylece lead bile kanıttan kaçamaz: önce plan
    onayı (approve_plan), sonra gate'ler.
    """
    tasks = plan.get("tasks") or []
    if not tasks:
        return _err("boş plan")
    ids = {t["id"] for t in tasks}
    for t in tasks:
        if store.get_domain(t["domain"]) is None:
            return _err(f"bilinmeyen domain: {t['domain']}")
        for dep in t.get("depends_on") or []:
            if dep not in ids:
                return _err(f"plan içi olmayan bağımlılık: {dep}")
    # önce plan-üst task'ı (onay gate'i buna bağlanır)
    plan_task = store.create_task(domain="lead", title=f"plan: {plan['title']}",
                                  body=str(plan.get("title")), kind="plan")
    id_map: dict[str, int] = {}
    for t in tasks:
        # bağımlılıklar henüz numeric id'ye çevrilecek; iki geçiş
        id_map[t["id"]] = store._insert_task_raw(
            domain=t["domain"], title=t["title"], body=t.get("body", ""),
            kind="feature", parent_id=plan_task["id"], status="pending_plan")
    for t in tasks:
        # plan-üst task'ı DAG kökü olarak ebeveyn DEĞİL, referans: ebeveyni
        # kendisi (plan-task) olur ama parent-kapısı kuralı kind='plan' için
        # çalışmaz; depends_on gerçek sıralamayı taşır.
        deps = [id_map[d] for d in (t.get("depends_on") or [])]
        store._conn.execute(
            "UPDATE tasks SET depends_on=? WHERE id=?",
            (__import__("json").dumps(deps), id_map[t["id"]]))
    # plan onayını bekleme sırasında plan-task dispatch edilmesin;
    # approve_plan'da ready olur ve lead/insan tarafından kapatılır
    store._conn.execute(
        "UPDATE tasks SET status='pending_plan' WHERE id=?", (plan_task["id"],))
    store._conn.commit()
    return _ok(plan_task_id=plan_task["id"], ids=id_map)


def approve_plan(store: Store, plan_task_id: int, approver: str) -> dict:
    """Plan onayı (lead ya da insan). Tüm pending_plan çocukları ready olur."""
    pt = store.get_task(plan_task_id)
    if pt is None or pt["kind"] != "plan":
        return _err("plan task bulunamadı")
    n = 0
    for kid in store.children_of(plan_task_id):
        if kid["status"] == "pending_plan":
            store._conn.execute(
                "UPDATE tasks SET status='ready', updated_at=? WHERE id=?",
                (time.time(), kid["id"]))
            store._event(kid["id"], approver, "plan_approved", {})
            n += 1
    # plan-task artık boşaltılabilir
    store._conn.execute(
        "UPDATE tasks SET status='ready' WHERE id=? AND status='pending_plan'",
        (plan_task_id,))
    store._conn.commit()
    return _ok(released=n)


# ---------- MCP şekli (Claude Code / Codex bu manifest'i görür) ----------

TOOLS: list[dict] = [
    {"name": "request_task",
     "description": "Başka bir domain'in agent'ından iş iste. children "
                    "DAG'ına eklenir; hedef domain agent'ı sırası gelince alır.",
     "params": {"caller": "str", "domain": "str", "title": "str",
                "body": "str?"}},
    {"name": "report_result",
     "description": "Aktif task'ının sonucunu ve artefakt yollarını bildir.",
     "params": {"caller": "str", "summary": "str",
                "artifacts": "dict[str,str]", "task_id": "int?"}},
    {"name": "ask_domain",
     "description": "Bir domain'in CONTEXT.md ve contract'larını oku.",
     "params": {"caller": "str", "domain": "str", "question": "str"}},
    {"name": "request_contract_change",
     "description": "Kendi domain'inin contract dosyasını değiştir; bağımlı "
                    "domain'lere otomatik inceleme task'ları açılır.",
     "params": {"caller": "str", "path": "str", "new_content": "str"}},
    {"name": "submit_plan",
     "description": "Lead: iş planı DAG'ı gönder (onaylanana dek dispatch yok).",
     "params": {"plan": "dict"}},
    {"name": "approve_plan",
     "description": "Lead/insan: planı onayla; task'lar ready olur.",
     "params": {"plan_task_id": "int", "approver": "str"}},
]

_HANDLERS: dict[str, Callable] = {
    "request_task": request_task,
    "report_result": report_result,
    "ask_domain": ask_domain,
    "request_contract_change": request_contract_change,
    "submit_plan": submit_plan,
    "approve_plan": approve_plan,
}


def tool_manifest() -> list[dict]:
    return TOOLS


def dispatch(store: Store, tool: str, args: dict[str, Any]) -> dict:
    """MCP server'ın ve CLI köprüsünün ortak tek girişi."""
    fn = _HANDLERS.get(tool)
    if fn is None:
        return _err(f"bilinmeyen araç: {tool}")
    try:
        return fn(store, **args)
    except TypeError as e:
        return _err(f"geçersiz argüman: {e}")
