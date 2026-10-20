# Software Factory — Mimari

Tarih: 2026-10-20 · Durum: taslak v1 (onay bekliyor) · Sahip: gurkanozkan

## 1. Amaç

Kendi yazılım ürünümüzü üreten ve yöneten bir factory:

- Girdi: istek/spec/bug sinyali. Çıktı: deploy edilmiş, doğrulanmış değişiklik.
- Bir lead agent işi parçalar ve dağıtır; her domain'in kendi agent'ı vardır.
- Analiz, kodlama, review, test ayrı ve ZORUNLU aşamalardır; atlanamaz.
- Agent'lar arası bilgi tek yönlü akışla değil, sözleşmeler (contract) ve
  domain context dosyalarıyla dolaşır.
- Hermes fleet'i (20 profil) KULLANILMAZ. Kendi sade katmanımız: ~500-800 satır
  Python + SQLite + agent çağrısı olarak her rol için `hermes chat -p <profil> -q`
  (veya eşdeğeri OpenAI-uyumlu çağrı). Hermes sadece "beyin" sağlayıcı,
  kontrol düzlemi biziz.

## 2. Kavramlar

### Domain
Ürünün kalıcı sahipliği olan bir dilimi (ör. `auth`, `billing`, `ui-shell`,
`data-pipeline`). Domain = şunların paketi:

```
domains/<name>/
  domain.yaml        # sahiplik tanımı (aşağıda)
  CONTEXT.md         # domain agent'ının yazdığı, tek doğruluk kaynağı
  contracts/         # dışarıya export edilen API/şema/ADR — tek temas yüzeyi
  workspace/         # kod, testler, geçici artefaktlar
```

`domain.yaml`:
```yaml
name: billing
owner_profile: qwen-local        # hangi model/profil koşar
responsibilities:
  - faturalama mantığı
  - abonelik durum makinesi
paths:
  - src/billing/**
  - src/api/billing/**
exports: contracts/              # diğer domainler sadece burayı okur
depends_on: [auth]               # statik bağımlılık grafiği (çakışma tespiti)
budget:
  max_parallel_workers: 1        # GPU headroom politikası (max 3 toplam)
```

### Lead Agent
Tek orkestratör. İşleri BİLMEZ, yönlendirir. Görevleri:
1. Gelen isteği alır, impact analizi yapar (hangi domainler?).
2. İş planı çıkarır: task'ları domain'lere böler, bağımlılık grafiğini kurar.
3. Task'ları registry'ye yazar (state=ready).
4. Worker'ları tetikler / worker'lar claim eder.
5. Gate'leri zorlar: geçersiz teslimi done yapmaz, iade eder.
6. Çapraz domain çatışmalarını çözer (contract değişikliği = her iki tarafın
   onay task'ı gerekir).

Lead agent'ın kendisi de bir profildir (GLM-5.3 gibi hafif bir model yeterli;
ağır işi domain worker'ları yapar).

### Domain Agent (worker)
Registry'den kendi domain'inin task'ını claim eder. Bir task üzerindeki roller
(analyst → coder → reviewer → tester) AYNI domain agent'ının içinde sıralı
geçişlerdir — ayrı süreçler değil, ayrı ÇIKTILARDIR:

```
task workdir:
  01-analysis.md     # ne değişecek, hangi dosyalar, hangi contract'a dokunur
  02-implementation/ # kod diff'i
  03-review.md       # bağımsız reviewer çıktısı (farklı profil/seed)
  04-test-report.md  # gerçek komut çıktısı, exit code'lu
  RESULT.json        # makine-okur özet → registry'ye yazılır
```

### Roller
| Rol | Kim | Kural |
|---|---|---|
| analyst | domain agent | 01-analysis.md yazmadan kod yazılamaz |
| coder | domain agent | analysis'e bağlı; contract'u izinsiz değiştiremez |
| reviewer | FARKLI profil | aynı domain agent'ının çıktısını onaylayamaz; cross-review (bkz. §5) |
| tester | domain agent + harness | test'i çalışan kodun gerçek çıktısı kanıtlar, iddia değil |
| lead | lead agent | gate'ler, dispatch, çatışma çözümü |

## 3. Ortak Context — 4 katman (hiçbiri tek mutable blob değil)

1. **domain/CONTEXT.md** — domain agent'ının sahiplendiği living doc:
   mimari kararlar, bilinen tuzaklar, güncel durum. Tek yazan domain agent;
   başkaları sadece okur. Lead, task kapanışında "CONTEXT güncellendi mi?"
   kontrolü yapar.
2. **contracts/** — domain'ler arası TEK temas yüzeyi. API şemaları, olay
   şemaları, ADR'ler. Değişiklik = contract task'ı = bağımlı domain'lerin
   hepsine otomatik inceleme task'ı (impact kuralı, §5).
3. **SQLite registry** — işin durumu, kanıtları, geçmişi. Her task'ın tüm
   artefakt yolları, model/profil/version'ları burada → replayability.
4. **Hindsight (:7847)** — "neden" bilgisi: geçmiş kararlar, kök nedenler,
   tercihler. Worker'lar task alırken ilgili domain için recall yapar.
   Zorunlu değil ama mevcut ve bedava.

## 4. Registry (kontrol düzlemi)

CS10 registry'nin kanıtlanmış kalıbı (claim/heartbeat/retry/systemd) aynen
alınır, üstüne gate state'i eklenir:

```
tasks:
  id, domain, title, body, kind(feature|bug|contract|analysis)
  status: triage|ready|claimed|analyzing|coding|reviewing|testing|
          review|gated|done|blocked|archived
  parent_id, depends_on(JSON)        # DAG
  claimed_by, claimed_at, heartbeat_at
  attempts, max_attempts(5), backoff(120s)   # CS10 dayanıklılığı
  artifacts(JSON)                    # 01..04 + RESULT.json yolları
  gate_evidence(JSON)                # gate geçişlerinin kanıtları
gates:
  id, task_id, name(reviewer_approved|tests_passed|context_updated|contract_sync)
  required, passed, evidence, checked_at, checked_by
events: id, task_id, ts, actor, kind, payload   # tam olay geçmişi
```

Worker protokolü (CS10'dan): registry düşerse 5×120sn retry; registry
systemd service; kalp atışı tek başına ilerleme sayılmaz (artefakt ilerlemesi
kontrol edilir — fleet_sweeper dersi).

## 5. Gate'ler — fail-closed, kanıta dayalı

Bir task `done` OLAMAZ; ta ki:

1. **analysis_done** — 01-analysis.md var ve non-empty; dokunulan contract'lar listelenmiş.
2. **reviewer_approved** — reviewer çıktısı `APPROVE`; reviewer, coder'dan
   FARKLI profil/seeds. Kritik domainlerde (auth, billing, ödeme) çift
   reviewer: ikisi de farklı (cross-domain review: komşu domain'in reviewer'ı).
3. **tests_passed** — harness gerçekten çalıştırdı, exit code 0, rapor
   04-test-report.md'de komut + çıktı kanıtı ile. "Testler geçti" iddiası
   kanıt değildir; RESULT.json'daki `test_command` + `test_exit_code` +
   rapor dosyası hash'i kanıttır.
4. **context_updated** — domain CONTEXT.md task kapanışında güncellenmiş
   (ya da "gerek yok" gerekçesi reviewer onaylı).
5. **contract_sync** — contract değiştiyse: bağımlı TÜM domain'lere
   inceleme task'ı açılmış ve hepsi done.

Gate ihlali = kart `gated` state'ine düşer, lead'e düşer, tekrarlanırsa
`blocked` olur. (Kanban ghost/stuck sweeper'ın factory karşılığı: gate
denetleyicisi periyodik tarar.)

## 6. Çapraz agent akışları

- Serbest agent-chat YOK. Akış = task + artefakt + olay kaydı.
- Domain A, Domain B'ye ihtiyaç duyarsa: B'ye `contract` türünde task açar
  (parent = A'nın task'ı). B'nin agent'ı kendi CONTEXT.md'sine bakıp karar
  verir. A, B'nin içine yazamaz.
- İki domain aynı anda aynı contract'ı değiştirirse → lead sıraya sokar
  (contract lock, registry'de tek satır).
- Analiz aşaması çapraz olabilir: lead, büyük işler için `analysis` türü
  bağımsız task açar; analist domain'ler okur, plan yazar; lead planı
  alt-task'lara böler.

## 7. Dispatch & çalıştırma

- Scheduler döngüsü (tek python süreci, systemd): ready task'ları DAG
  sırasıyla domain'lere atar; `max_parallel` (varsayılan 2, sert cap 3 —
  yerel GPU politikası) aşamaz; taşan iş lead'in kuyruğunda bekler.
- Worker çağrısı: `hermes chat -p <domain-profil> -q "<task prompt + artefakt
  yolları + context dosyaları>"` + workspace mount. Timeout yok; stall,
  artefakt-ilerlemesi ile ölçülür.
- Failover: profil/model ölürse CS10 failover kalıbı (ikincil modele geç,
  `--force` ile geri dönüş).

## 8. Ölçüm (factory olmak bunu gerektirir)

- cycle time (ready→done), kabul oranı (done / done+blocked), kaçak defect,
  review yükü, kabul başına maliyet, gate iade oranı.
- Hepsinin ham verisi `events` tablosunda zaten; tek SQL view yeter.

## 9b. Kontrol düzlemi seçimi (ADR-005, 2026-10-20 güncellemesi)

Runner/kuyruk/review yüzeyi kendimiz yazmıyoruz: **Multica self-host**
(multica-ai/multica, Apache-2.0+takı, Docker Compose/Helm). Issue = task,
agent = board'da teammate; Multica 26 CLI sürer (claude, codex, hermes dahil)
ve model taşımaz; run'lar kendi runtime'ımızda, execution log + retry +
review gate + CLI/API hazır. Bizim yazdığımız tek şey §2–5'teki domain +
contract + kanıt-tabanlı gate semantiği (~300-500 satır denetleyici, Multica
API'sine bağlanır). Ruflo/claude-flow reddedildi (tiyatro + kritik npm
açıkları). Ayrıntı: `adr/0005-multica-kontrol-duzlemi.md`.

## 9. Ne KULLANILMIYOR ve neden

- LangGraph / LangGraph-Platform / CrewAI: kontrol düzlemini onların
  scheduler'ına vermek = gate kültürümüzü (fail-closed, kanıt) onların
  state makinesine yamamak. ~600 satırla kendi state makinemiz daha
  denetlenebilir. (Karar ADR-002.)
- Hermes kanban fleet'i: zengin ama 20 profilli, kendi ürünümüzün ritmine
  göre ağır; fleet'i araştırmada bırakıyoruz. (ADR-001.)
- Merkezi "herkesin yazdığı" ortak wiki: context çöplüğü olur; bunun yerine
  domain-sahipliği + contract export. (ADR-003.)

## 10. Uygulama sırası (kod aşamasına geçince)

1. `registry/` — store.py + server.py (CS10'dan evrim, gate tabloları ile) — 1 gün
2. `dispatcher` + worker runner (hermes chat sarmalayıcı) — 1 gün
3. gate denetleyicisi + sweeper — 0.5 gün
4. domain scaffold (`factory domain add auth ...`) — 0.5 gün
5. pilot: 2 domain, 3 gerçek task (biri çapraz contract'lı) — 1 gün
6. ölçüm view'ları + README — 0.5 gün

## Açık sorular (kod öncesi onay bekleyen)

- İlk domain seti ve ilk ürün hangisi? (Fable araçları? edge-foundry paneli?
  yeni bir ürün?)
- Reviewer havuzu: tüm domain'ler ortak reviewer profilinden mi çeksin
  (merkezi kalite) yoksa her domain'in kendi reviewer'ı mı (domain bilgisi)?
  Öneri: ortak havuz + kritik domain'de komşu-domain reviewer.
- Deploy kapısı: done sonrası deploy otomatik mi, insan onaylı mı?
  Öneri: ilk 2 hafta insan onaylı, sonra risk bazlı.
