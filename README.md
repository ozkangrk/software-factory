# factory

Kanıt-tabanlı gate + domain denetleyicisi — software factory kontrol katmanı.
Multica (veya herhangi bir runner) üstünde, "task bitti" iddiasını kanıta
bağlayan ince katman: analiz → kod → bağımsız review → gerçek test → context
→ contract sync. Hepsi fail-closed; kanıt yoksa `done` yok.

Mimari ve kararlar: `docs/ARCHITECTURE.md`, `docs/adr/`.

## Kurulum

```bash
uv sync                      # dev: uv run pytest -q
uv pip install .             # ya da: pip install .
```

## Hızlı başlangıç

```bash
WS=./factory-workspace
factory $WS domain add --name auth --profiles coder-auth
factory $WS domain add --name billing --profiles coder-bill --depends-on auth --critical
factory $WS task add --domain auth --title "JWT rotasyonu"
factory $WS claim --worker w1 --domain auth

# teslim zinciri (sırasıyla; hepsi kanıt bırakır):
factory $WS check 1                          # gate raporu; geçmeyense exit 1
factory $WS test-run 1 -- pytest -q          # testi GERÇEKTEN koşar, raporlar
factory $WS review 1 --reviewer r-x --coder coder-auth --approve \
        --artifact $WS/03-review.md
factory $WS context 1 --path domains/auth/CONTEXT.md
factory $WS finalize 1                       # gate'ler geçmeden done OLAMAZ
```

## Gate'ler (fail-closed)

| Gate | Kanıt |
|---|---|
| analysis_done | 01-analysis.md var ve boş değil |
| reviewer_approved | farklı profilden APPROVE; artefakt yerinde; kritik domainde 2 bağımsız onay; kod digest'i değişince bayatlar |
| tests_passed | gerçek koşu + exit 0 + rapor dosyası; rapor hash'i kayıtla birebir |
| context_updated | CONTEXT.md güncellendi ya da reviewer onaylı muafiyet |
| contract_sync | contract'a dokunulduysa tüm bağımlı domain inceleme task'ları done |

## Deploy

Docker:
```bash
docker build -f deploy/docker/Dockerfile -t factory .
docker compose -f deploy/docker/docker-compose.yml up
```

Kubernetes (tek-yazar SQLite → gatekeeper replicas=1; worker'lar Job olarak
`factory /data check` çalıştırır):
```bash
kubectl apply -f deploy/k8s/factory.yaml
```

## Multica entegrasyonu (plan)

Multica issue `review` state'ine geçtiğinde webhook/loop bu repo'nun
`check`+`finalize` çağrısını çalıştırır; exit 0 olmadan issue done olmaz.
Worker'lar Claude Code/Codex CLI; kanıt dosyaları task workspace'inde durur.

## Ajan araçları (birbirini çağırma)

Ajanlar serbest chat yapmaz; registry-denetimli araçları çağırır (ADR-006):
`request_task` (başka domain'e çocuk iş), `report_result`, `ask_domain`
(hedef domain'in CONTEXT.md+contract'ları), `request_contract_change`
(sahibi+aktif worker), `submit_plan`, `approve_plan`. Kimlik = aktif claim.

Claude Code'a MCP olarak bağlanır:
```bash
claude mcp add factory -- python -m factory.mcp --workspace $WS
```
CLI'dan da aynı dispatch: `factory $WS call request_task '{"caller":...}'`
Araç listesi: `factory $WS tools`.

Lead beyni (plan→onay→dispatch→gate sweep):
```bash
factory-lead $WS --runner 'claude -p' --once    # veya --interval ile döngü
```

## Multica köprüsü (UI = Multica, kanıt = Factory)

Factory task'larını Multica issue'larına bağla, sonra döngüyü sürdür:
```bash
link_issue(store, task_id, "<multica-issue-uuid>")   # veya meta'ya elle
MULTICA_TOKEN=*** MULTICA_WORKSPACE_ID=... \
  python -m factory.multica $WS --interval 30
```
Gate'ler yeşilse issue `in_review` olur; kanıt kaybolursa geri çekilir;
sen UI'da `done` dersen Factory task'ı kapanır (kanıtsızsa `gate_bypass`
denet olayıyla). Multica'yı upstream'e bağlı tutmak için:
`scripts/multica-follow.sh` (ADR-007: fork yok, API köprüsü).

## Test

```bash
uv run pytest -q     # 38 test: gate semantiği, claim/lease, domain, DAG
```
