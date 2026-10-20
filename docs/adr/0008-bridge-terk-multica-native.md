# ADR-008: Bridge terk edildi — Multica native workflow ile doğrudan kullanım

Durum: kabul · Tarih: 2026-10-20 · Önceki: ADR-007 (köprü dönemi, geçersiz)

Bağlam: Kullanıcı kararı — "Bridge kullanmayacağım, doğrudan Multica
kullanacağım." Bridge (factory/multica.py) gate→issue senkronu yapıyordu
ama bakım yükü ve ölüm takibi (bridge düştü, kimse fark etmedi) operasyonel
gürültü üretiyordu.

Karar:
1. `python -m factory.multica` çalıştırılmaz; systemd/cron'a geri eklenmez.
   Kod repoda arşiv olarak durur (tarihsel referans + API kırlıkları).
2. Multica kendi native issueworkflow state machine'i ile kullanılır:
   dispatch/review/done geçişleri UI + native akış üzerinden.
3. Factory tarafı (registry, gates, A2A, plan-onayı) kendi başına kalır;
   Multica ile arasında otomatik senkron YOKTUR. Kanıt zorunluluğu
   factory gate'lerinde, insan onayı Multica'da — iki sistem paralel.
4. Upstream-follow (scripts/multica-follow.sh) fetch+reset+healthz kısmıyla
   devam eder; köprü sürdürme adımı anlamsız.
5. ADR-007'nin fork tetikleyicileri geçersiz; fork konusu yeni bir ADR ile
   değerlendirilir.

Sonuç: Bridge bakım yükü ve fail-open/senkron kayması riski sıfır;
karşılığında gate↔issue otomasyonundan vazgeçildi — review disiplini
insan + factory gate'leri tarafından ayrı ayrı korunur.
