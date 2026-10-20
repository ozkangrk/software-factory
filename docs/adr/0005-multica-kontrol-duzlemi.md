# ADR-005: Kontrol düzlemi olarak Multica (self-host) + kendi gate/domain katmanımız

Durum: kabul · Tarih: 2026-10-20

Bağlam: Factory'nin runner/kuyruk/review yüzeyini kendimiz yazmak istemiyoruz;
Claude Code ve Codex CLI kurulu (claude 2.1.295, codex), 2026'da sektör de
harness'i dışarıdan alıyor (OpenAI Symphony → Codex CLI, Anthropic managed
agents → Claude Code). Adaylar tarandı: Multica (53k⭐, aktif, günde commit),
Agent Orchestrator, Symphony-fork'ları, NEEDLE, h5i, Ruflo.

Karar:
- Multica self-host (Docker Compose/Helm) kontrol düzlemi: issue = task,
  agent = board'da teammate, run'lar kendi runtime'ımızda (kod makineyi
  terk etmez), review gate'leri, execution log, retry/timeout, CLI+API.
  Multica model taşımaz; 26 CLI sürer — claude, codex ve HERMES dahil.
- Worker'lar: Claude Code / Codex CLI. Lead/dispatch rolünde Hermes profili
  de sürülebilir (Multica hermes CLI'ını native destekler).
- Üstüne İNCE kendi katmanımız (~300-500 satır Python): domain registry
  (domain.yaml + CONTEXT.md + contracts/) ve fail-closed gate denetleyicisi.
  Multica'nın "review" state'i insan onayı demek; bizim kanıt-tabanlı
  gate'lerimiz (test exit code, farklı-profil APPROVE, contract_sync)
  denetleyici tarafından doğrulanmadan issue done OLAMAZ.
- Ruflo/claude-flow REDDEDİLDİ: topluluk değerlendirmesi "gerçek subprocess
  orchestrasyonu yapmıyor" + npm-audit'te critical bağımlılık açıkları.

Sonuç: Runner/kuyruk/UI/hesap-verimi bedavaya geldi; bizim yazdığımız tek şey
ayırt edici sır olan domain+contract+gate semantiği. Bedeli: Multica main
dalı hızlı hareket ediyor (haftada birkaç release) → pinned tag ile koş,
upgrade'leri test ederek al. Docker Compose ek altyapı yükü (121GB RAM
makinede uygun).

## Pilot planı (onaylanınca)

1. `install.sh --with-server` + `multica setup self-host` (pinned GHCR tag)
2. Runtime olarak bu DGX kaydedilir; claude + codex CLI provider olarak tanımlanır
3. İki domain: `backend` ve `frontend` (pilot ürün: edge-foundry paneli veya
   founder'ın seçeceği gerçek ürün) — her biri Multica'da agent + workspace
   bağlamı; domain.yaml/CONTEXT.md/contracts/ repo'da yaşar
4. Gate denetleyicisi: Multica API/webhook ile issue review'a geçince
   kanıtları doğrular; eksikse comment atıp in_progress'e iade eder
5. Çapraz akış testi: contract değişikliği task'ı → bağımlı domain'e
   otomatik inceleme task'ı
6. Ölçüm: cycle time, gate iade oranı (Multica usage analytics + events)

## Açık kalan

- Pilot ürün hangisi (edge-foundry paneli mi)?
- Deploy kapısı ilk 2 hafta insan onaylı kalacak (öneri: evet).
