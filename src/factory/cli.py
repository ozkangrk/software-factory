"""factory CLI — tek giriş noktası.

    factory domain add ./factory-workspace --name billing --profiles coder-b --critical
    factory task add ./factory-workspace --domain billing --title "..."
    factory check  ./factory-workspace TASK_ID [--code-digest SHA]
    factory test-run ./factory-workspace TASK_ID -- cmd...   # gerçekten koşar, kanıtlar
    factory review ./factory-workspace TASK_ID --reviewer r2 --coder c1 --approve
    factory finalize ./factory-workspace TASK_ID
    factory stats ./factory-workspace
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from .domain import DomainError, load_domain, scaffold_domain, sync_to_store
from .gates import evaluate_gates, GATE_NAMES
from .store import GateNotPassed, Store


def _store(args) -> Store:
    ws = Path(args.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    return Store(ws / "registry.db")


def cmd_domain_add(args):
    d = scaffold_domain(Path(args.workspace), name=args.name,
                        profiles=args.profiles, depends_on=args.depends_on,
                        critical=args.critical, paths=args.paths,
                        responsibilities=args.responsibilities)
    sync_to_store(_store(args), d)
    print(f"domain hazır: {d.root}")


def cmd_domain_sync(args):
    d = load_domain(Path(args.workspace), args.name)
    sync_to_store(_store(args), d)
    print(f"senkron: {d.name}")


def cmd_task_add(args):
    st = _store(args)
    t = st.create_task(domain=args.domain, title=args.title, body=args.body,
                       kind=args.kind, depends_on=args.depends_on or [])
    print(json.dumps({"id": t["id"], "status": t["status"]}))


def cmd_claim(args):
    st = _store(args)
    t = st.claim(args.worker, args.domain)
    print(json.dumps(t, ensure_ascii=False) if t else "null")


def cmd_check(args):
    st = _store(args)
    report = evaluate_gates(st, args.task_id, code_digest=args.code_digest)
    for name in GATE_NAMES:
        g = report.by_name(name)
        mark = "PASS" if g["passed"] else "FAIL"
        print(f"[{mark}] {name}: {g.get('reason', '')}")
    sys.exit(0 if report.all_required_passed else 1)


def cmd_test_run(args):
    """Test'i GERÇEKTEN koşar; raporu dosyaya yazar, hash'ler, kaydeder."""
    st = _store(args)
    ws = Path(args.workspace)
    cmd = args.cmd
    if cmd[:1] == ["--"]:
        cmd = cmd[1:]
    cmd = " ".join(cmd) if isinstance(cmd, list) else cmd
    if not cmd.strip():
        print("komut boş", file=sys.stderr)
        sys.exit(2)
    report = ws / "runs" / f"task-{args.task_id}-tests.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          cwd=str(ws))
    report.write_text(f"$ {cmd}\nexit={proc.returncode}\n\n"
                      f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n")
    st.record_test_run(args.task_id, cmd, proc.returncode, str(report))
    print(f"exit={proc.returncode} report={report}")
    sys.exit(proc.returncode)


def cmd_review(args):
    st = _store(args)
    verdict = "APPROVE" if args.approve else "CHANGES_REQUESTED"
    st.record_review(args.task_id, args.reviewer, args.coder, verdict,
                     args.artifact, code_digest=args.code_digest)
    print(f"kaydedildi: {verdict} by {args.reviewer}")


def cmd_context(args):
    st = _store(args)
    if args.waive:
        st.mark_context_waived(args.task_id, args.waive, approved_by=args.approved_by)
    else:
        st.mark_context_updated(args.task_id, args.path)
    print("kaydedildi")


def cmd_contract_touch(args):
    st = _store(args)
    st.add_contract_touch(args.task_id, args.path, args.owner)
    kids = st.children_of(args.task_id)
    print(f"inceleme task'ları: {[(k['id'], k['domain']) for k in kids]}")


def cmd_finalize(args):
    st = _store(args)
    try:
        st.finish(args.task_id, "done")
        print("done")
    except GateNotPassed as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def cmd_stats(args):
    print(json.dumps(_store(args).stats()))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="factory")
    ap.add_argument("workspace")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("domain")
    dsub = d.add_subparsers(dest="cmd2", required=True)
    da = dsub.add_parser("add")
    da.add_argument("--name", required=True)
    da.add_argument("--profiles", nargs="+", required=True)
    da.add_argument("--depends-on", nargs="*", default=[])
    da.add_argument("--paths", nargs="*", default=None)
    da.add_argument("--responsibilities", nargs="*", default=[])
    da.add_argument("--critical", action="store_true")
    da.set_defaults(fn=cmd_domain_add)
    ds = dsub.add_parser("sync")
    ds.add_argument("name")
    ds.set_defaults(fn=cmd_domain_sync)

    t = sub.add_parser("task")
    tsub = t.add_subparsers(dest="cmd2", required=True)
    ta = tsub.add_parser("add")
    ta.add_argument("--domain", required=True)
    ta.add_argument("--title", required=True)
    ta.add_argument("--body", default="")
    ta.add_argument("--kind", default="feature")
    ta.add_argument("--depends-on", nargs="*", type=int, default=[])
    ta.set_defaults(fn=cmd_task_add)

    c = sub.add_parser("claim")
    c.add_argument("--worker", required=True)
    c.add_argument("--domain", required=True)
    c.set_defaults(fn=cmd_claim)

    ch = sub.add_parser("check")
    ch.add_argument("task_id", type=int)
    ch.add_argument("--code-digest", default=None)
    ch.set_defaults(fn=cmd_check)

    tr = sub.add_parser("test-run")
    tr.add_argument("task_id", type=int)
    tr.add_argument("cmd", nargs=argparse.REMAINDER)
    tr.set_defaults(fn=cmd_test_run)

    r = sub.add_parser("review")
    r.add_argument("task_id", type=int)
    r.add_argument("--reviewer", required=True)
    r.add_argument("--coder", required=True)
    r.add_argument("--approve", action="store_true")
    r.add_argument("--artifact", default=None)
    r.add_argument("--code-digest", default=None)
    r.set_defaults(fn=cmd_review)

    cx = sub.add_parser("context")
    cx.add_argument("task_id", type=int)
    cx.add_argument("--path", default=None)
    cx.add_argument("--waive", default=None)
    cx.add_argument("--approved-by", default=None)
    cx.set_defaults(fn=cmd_context)

    ct = sub.add_parser("contract-touch")
    ct.add_argument("task_id", type=int)
    ct.add_argument("--path", required=True)
    ct.add_argument("--owner", required=True)
    ct.set_defaults(fn=cmd_contract_touch)

    f = sub.add_parser("finalize")
    f.add_argument("task_id", type=int)
    f.set_defaults(fn=cmd_finalize)

    s = sub.add_parser("stats")
    s.set_defaults(fn=cmd_stats)

    args = ap.parse_args(argv)
    if getattr(args, "cmd", None) == "test-run" and args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    args.fn(args)


if __name__ == "__main__":
    main()
