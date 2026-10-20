# ADR-006: Ajanlar-arası çağrı ve lead orkestrasyonu — registry-denetimli araçlar + MCP

Durum: kabul · Tarih: 2026-10-20

Bağlam: Kurucu isteği: ajanlar birbirini çağırabilmeli, lead agent hepsini
orkestre etmeli. Ruflo tarzı "herkes herkesi arar" peer modeli reddedilmişti
(tiyatro + güvenlik). LangGraph'a geçmek de ADR-002'ye aykırı.

Karar:
1. Ajanlar-arası çağrı = SERBEST RPC değil; registry üzerinden yetki denetimli
   araç çağrısı. Altı araç: request_task (başka domain'e çocuk iş),
   report_result (kendi task'ına kanıt bırak), ask_domain (hedef domain'in
   CONTEXT.md + contract'larını salt-okur oku), request_contract_change
   (yalnız contract sahibi + aktif worker), submit_plan, approve_plan.
2. Kimlik = aktif claim. Claim'i olmayan çağrı yapamaz; kimsenin task'ına
   yazamaz. Her çağrı events tablosuna denet kaydı düşer.
3. Lead beyin (lead.py) tek döngü: istek→LLM plan→(onay kapısı)→dispatch→
   gate sweep. 'lead' domain'i sistem içidir; ajan claim'i yapamaz.
4. Kilitlenme önlemleri: plan-kind parent DAG kökü sayılır (onay çocukları
   serbest bırakır); cross/contract children gerçek parent'ının done'ını
   bekler; parent kendi child'ını beklemez (cross, contract_sync'i
   tetiklemez).
5. Sunum: aynı dispatch() hem MCP stdio JSON-RPC (factory.mcp — Claude Code
   `claude mcp add factory`), hem CLI (`factory <ws> call <araç> <json>`).

Sonuç: A2A "kendi kendine konuşan swarm" değil, defter-merkezli ekip: her
çağrı kanıt bırakır, gate'ler yine fail-closed. 61 test. Bedeli: LLM
ajana araç verirken prompt tarafı (Claude Code'a MCP config) ayrıca
kalibre edilecek; Hindsight köprüsü ask_domain'e henüz gömülmedi.
