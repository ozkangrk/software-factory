# ADR-007: Multica fork'lanmaz — API köprüsü + upstream-follow

Durum: kabul · Tarih: 2026-10-20

Bağlam: Kurucu "Multica'yı forklayıp üzerine geliştirelim, UI'larını
kullanalım, DAG'a çevirelim" önerdi + "Multica'yı sürekli canlıdan takip
edelim". Fork'un gerçek tetikleyicisi: gate'leri sunucu tarafı state
machine'e gömmek (in_review/done geçişini Go tarafında kanıta bağlamak).

Karar:
1. Fork YOK (şimdilik). Multica upstream klon olarak ~/multica/multica'da
   durur, tek satır değiştirilmez; factory repoları ayrı kalır.
2. Entegrasyon = dış API köprüsü (factory/multica.py):
   - Kimlik: PAT (mul_) + X-Workspace-ID; API alanları upstream Go
     router'ından (server/cmd/server/router.go, handler/*.go) doğrulandı.
     Yorum alanı 'content', issue status geçişi PUT /api/issues/{id}.
   - Factory→Multica: gate'ler yeşilse issue 'in_review'; kanıt kaybolursa
     'in_progress'a geri; gate tablosu issue yorumu (değişim imzalı, spam
     yok).
   - Multica→Factory: insan UI'da 'done' derse task kapanır; kanıt
     yerindeyse gate ile kapanır, değilse gate_bypass denet olayıyla
     (fail-open deliği bırakılmaz).
   - DAG'ın UI karşılığı: Factory task'ları Multica issue'larına meta
     eşlemesiyle bağlanır; sıralama registry'de kalır (Multica parent/child
     status'ları bilinçli olarak birbirinden bağımsız).
3. Upstream-follow: scripts/multica-follow.sh — upstream/main fetch +
   reset, healthz doğrulaması, köprüyü --interval ile sürdürme. Haftalık
   cron'a bağlanır. API kırılırsa köprü testleri (66) patlar → fark edilmez
   olamaz.
4. Fork'a geçiş tetikleyicileri (yeniden değerlendirme koşulu):
   - gate'lerin SUNUCU tarafında zorunlu olması gerektiğinde,
   - UI'da kalıcı behavior/status değişikliği 3+ kez istendiğinde,
   - köprü bakım yükü fork merge yükünü aştığında.

Sonuç: Upgrade bedava, merge yükü sıfır; kanıt zorunluluğu Factory'de
zorlanıyor, Multica gösterim + insan onay katmanı. Kanıt: 5 köprü testi +
canlı Multica'ya karşı uçtan uca tur (kırmızı→in_review reddi, yeşil→
in_review, UI done→factory done+audit).
