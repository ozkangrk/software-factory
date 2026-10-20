# Cua Spaces / Cua Spaces SDK — derin araştırma ve yourforge entegrasyon değerlendirmesi

Tarih: 2026-10-20. Kaynaklar: github.com/trycua/cua (main, 2026-10-20), cua.ai docs
(quickstart, local-runtimes, add-a-computer, connect-your-agent, mcp-server),
libs/cua-spacesd/README.md, libs/cua/README.md, @trycua X thread (2026-10-19),
yerel DGX Spark üzerinde canlı probe.

## 1. Cua Spaces SDK nedir (2026-10-19 duyurusu)

Konum: "Her büyük lab kendi botunu kendi bilgisayarıyla veriyor (Grok Bot, Muse,
dots); hiçbiri BYO-agent izin vermiyor." Cua bilgisayarı verir, agent'ı sen getirirsin.

- Her bot kendi Space'ini alır: kendi ekranı ve app'leri olan bir desktop.
- Compute senin: laptop, ofis Mac mini, kendi AWS/GCP/Modal hesabın, veya
  E2B/Daytona/Modal gibi sandbox sağlayıcı.
- Agent bağımsız: Claude Code, Codex, Gemini CLI, Goose, OpenCode, **Hermes**
  veya kendi agent'ın — hepsi aynı Space ve aynı 33 Spaces MCP aracını kullanır.
- 10 satırla bot başlat/gönder/durum/durdur; `on: "local"` → başka makine adın.
- Tam bot örneği (Mac+iPhone): `samples/cua-bots-macos` (forklanabilir).

## 2. Mimari — üç katman

1. **cua SDK/CLI (Rust çekirdek, MIT)**: `cua-sdk` UniFFI ile Python (`pip install cua`),
   TypeScript (`@trycua/cua`, `/spaces` alt modülü), Swift, Kotlin'e üretiliyor.
   Embedded (`Cua.embedded()`) veya `cua daemon` (UDS `~/.cua/cua.sock`, 0600) üzerinden.
   Sandboxes (`cua sb`), Spaces (`cua spaces`), images, runtimes, cloud, MCP server.
2. **cua-spacesd (konuk daemon, FSL-1.1-MIT)**: tek binary, tek port **3211**'de
   `cua.env.v1` gRPC + gRPC-Web kontratı: System/Process/Filesystem/Computer/Windows/
   Accessibility/Driver/Stream/Presence/Teleport/Tunnel/Volume servisleri.
   Ek yüzeyler: `/health` (204, auth'suz), `/files` (HMAC imzalı URL, zorunlu expiry),
   `/mcp` (Streamable-HTTP MCP, cua-driver araç kaydı), `/tunnel` + `/hotspot`
   (reverse SOCKS), `/media` + QUIC :3212 (H.264 video, Opus audio, media wire v2).
   **Agent'sız sandbox da çalışır** — spacesd opsiyonel bir "ek"; olmadan runtime'ın
   QMP/VNC/SSH/ADB yolu kullanılır.
   Auth: non-loopback bind için token zorunlu (`Bearer` veya `x-cua-env-authorization`),
   constant-time karşılaştırma, token rotasyonu tüm ticket/URL'leri iptal eder.
   `GetCapabilities` yeteneği bildirir, desteklenmeyeni `limitation` string'i ile döner.
3. **cua-relay (crates/cua-relay, MIT; Dockerfile + K8s manifesti var)**:
   Makine tek **outbound WSS** (yamux) ile bağlanır → NAT'ta inbound port yok.
   Account modunda (`cua host setup`) relay, cua.ai hesabıyla makineyi doğrular ve
   relay-imzalı principal assertion forward eder; env token'lar asla host'u terketmez.
   **Kendi relay'ini kurabilirsin**: `cua-spacesd join --relay wss://relay.example
   --relay-token ...` + `CUA_RELAY_URL`.

Üretim hattı: Lume (Apple Silicon VM, macOS/Linux), QEMU (KVM/HVF/WHPX/TCG),
OCI container (Docker/Podman; gVisor `runsc` varsa otomatik), Lume VM, kubevirt
(capability'de listeleniyor). macOS VM yalnız Apple Silicon'da.

## 3. Agent entegrasyonu (bizim için kritik kısım)

- `cua agents setup` Hermes'i **resmi olarak destekliyor**: skill'ler `~/.hermes/skills`,
  MCP girdisi `~/.hermes/config.yaml` (`mcp_servers`). Hermes'te `/reload-mcp`.
- `cua daemon mcp` = stdio MCP server; tüm Spaces (33 araç) + sandbox + computer +
  skills araçları. **Granüler permission** sistemi fail-closed:
  `spaces:all|readonly`, `sandbox:all|readonly`, `computer:all|readonly`,
  `skills:*`. Bilinmeyen permission uyarıyla atlanır → hiç tool kalmaz.
- **Worker'lara agent yerleştirme**: `agent_start {space, agent, prompt}` ile Space
  içinde detached Claude Code / Codex thread'leri (`~/.spaces-agents/<run_id>/`),
  client gitse de yaşar; `agent_message`/`agent_status`/`agent_stop`.
  TypeScript'te `startThread(...)` + `placement: dedicated|shared`.
- **Agent keys**: provider API key'leri daemon'da tutulur, MCP aracı olarak
  okunamaz/ yazılamaz (agent göremez); Linux'ta daemon env'inden verilir.
  `CUA_SPACES_AGENT_CREDENTIALS_HOME=none` → credential kopyalamayı kapatır.
- **Persistent agents** (named agent + memory + routines) ve Cua Bots app'i var.

## 4. Lisans haritası (proprietary yourforge için kritik)

| Bileşen | Lisans |
|---|---|
| SDK/CLI/daemon, Lume, Cua Driver, CUA-S1, Cua Bench, proto kontratı, **cua-relay** | MIT |
| Cua Spaces app'leri, **cua-spacesd**, Keyvault, teleport, cua-volume, streaming client/codec/viewer | FSL-1.1-MIT (source-available) |

FSL-1.1-MIT: kullanım, self-host ve üzerine inşa **serbest**; "Competing Version"
(hosted/managed olarak Spaces veya Driver-Bench sunmak) yasak; her release 2 yıl
sonra MIT'leşir. → **Kendi ürünümüzde worker izolasyonu/agent-computer katmanı
olarak embedded kullanmak + self-host relay MIT kapsamında sorun değil**; Cua'nın
hosted Spaces'ine rakip hosted servis satarsak FSL devreye girer. COMMERCIAL.md'ye
bakılmalı.

## 5. DGX Spark'ta canlı doğrulananlar (2026-10-20, kanıt)

- Host: aarch64, `/dev/kvm` var, Docker 29.6.2 (runc+nvidia runtime), qemu-system-aarch64 kurulu. `cua` CLI kurulu değil.
- `ghcr.io/trycua/linux:24.04` **arm64 variantı var** (docker manifest inspect).
- arm64 container içinde `cua-spacesd 0.5.3` binary'si çalışıyor: `/health` → 204,
  gRPC-Web `GetCapabilities` → 200 + protobuf capability raporu (version 0.5.3,
  Ubuntu 24.04, runtime=container; `a11y` limitation: "AT-SPI needs a D-Bus session
  bus"; audio.per_app limitation bildirimli). **Yalıtılmış Linux worker + agent API'si
  Spark'ta kanıtlı çalışıyor.**

Kısıtlar:
- gVisor (`runsc`) arm64 desteği güvenilmez → container yolu `runtime=runc` ile
  (Docker namespace izolasyonu) ya da KVM varsa `--kind vm` QEMU ile gerçek VM.
- macOS Space Lume gerektirir → sadece M2 Pro MacBook (Apple Silicon), ~50 GB disk,
  ve Cua Spaces app'i macOS 26 istiyor; MacBook uyku modunda Space sunamaz.
- X11 desktop (XFCE/Xvfb + PipeWire + cua-driver) container'da tam masaüstü için
  dbus-run-session ile başlatılmalı; headless coding worker için desktop şart değil.

## 6. Bizim stack'e uyum değerlendirmesi

**Uygun (önerilen):**
- **Worker yalıtımı**: Multica worker'larını `cua sb create linux --runtime runc`
  (veya `--kind vm`, KVM'li Spark'ta) ile izole et; her job'a taze sandbox.
  Kanıt bazlı gate'lerimizle doğal uyum: `sb.screenshot()`, trajectory ve
  `cua-bench` çıktıları gate artefaktı olabilir.
- **"Agent'a kendi bilgisayarı"**: `agent_start` + `startThread` API'si Multica
  runtime'ına doğrudan oturur; Claude Code/Codex worker'ları Space içinde uzun
  ömürlü thread olarak yaşar, Multica yeniden başlasa da run'lar kalır.
- **Hermes MCP entegrasyonu hazır**: `cua daemon mcp` + `--permissions` ile
  worker'a sadece görebileceği araçları ver (fail-closed).
- **Cross-machine**: Spark (Linux host) + MacBook (macOS Space) aynı relay
  altında; Tailscale zaten var → relay'e hiç gerekmeden `--direct` (loopback/
  Tailscale/LAN allowlist'i default güvenli) veya relay'ı **biz kendimiz**
  `cua-relay` + K8s manifestiyle Spark'ta self-host edebiliriz (MIT).

**Dikkat edilecekler:**
- Account/relay bağımlılığı: `cua host setup` varsayılanı relay.cua.ai + cua.ai
  sign-in. Kendi relay + `--direct` ile %100 kendi donanımımızda tutulabilir;
  device enrollment 2FA akışı account moduna ait, direct modda yok.
- FSL-1.1-MIT: yourforge'u "hosted Spaces" olarak satmadıkça sorun yok; planı
  COMMERCIAL.md'ye karşı bir kez hukuki netleştirmek gerekir.
- Spaces app'inin telepin/Keyvault/presence özellikleri bizim için opsiyonel;
  çekirdek ihtiyacımız (sandbox lifecycle + spacesd + agent_start + MCP) MIT
  SDK + FSL daemon ikilisinde, daemon embedded kullanım için serbest.
- Sürüm hızı çok yüksek (791 release, v0.11.0 bugün) → pin'li sürüm + digest'li
  image kullanmalı (`ghcr.io/trycua/linux:24.04@sha256:...`).

## 7. Somut pilot planı (Spark üzerinde, ~1 gün)

1. `curl -fsSL https://cua.ai/install.sh | sh` yerine bağımsız kurulum: `pip install cua`
   (veya release'ten `cua` binary) + `cua runtime doctor`; runsc arm64 yoksa
   `cua config set default.runtime runc` / job bazında `--runtime runc`.
2. `cua sb create linux --runtime runc --name w1` → `cua sb exec w1 uname -a` →
   spacesd capability probe (bugün kanıtlandı).
3. Multica worker shim'i: job başına `create_space`(reuse=false) → repo klonla →
   `agent_start claude-code/codex` → gate kanıtlarını (`screenshot`, log tail,
   transcript) topla → `delete_space`. Factory gate'lerine artefakt şeması ekle.
4. MacBook'u ikinci host yap: Cua Spaces app + `cua host setup --direct` Tailscale
   IP üzerinden (veya Spark'ta self-host `cua-relay`).
5. Hermes'i kontrol düzleminden bağla: `cua agents setup --agents hermes` →
   `/reload-mcp` → worker'lara sadece `sandbox:all,computer:readonly` ver.

## 8. Sonuç

**Kullanılabilir — ve önerilir.** Cua Spaces SDK, yourforge'un eksik parçası olan
"agent'a kendi bilgisayarını ver" katmanını (VM/container izolasyonu + gRPC agent
API + MCP + cross-host placement) MIT/FSL lisansıyla, self-host edilebilir relay'la
hazır sunuyor; Hermes resmen desteklenen agent'lardan. Kanıt bazlı gate mimarimize
screenshots/trajectories ile doğal bağlanır. Ana tasarım kararı: relay'ı ve
placement'ı tamamen kendi kontrolümüzde tutmak (cua.ai relay + account yerine
`cua-relay` self-host veya Tailscale üzerinden `--direct`), ve hosted Spaces
satışına girmeyerek FSL sınırında kalmak.
