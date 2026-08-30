# Compilation Output + Premiere Handoff — Rencana Matang

**Branch (usulan):** `feat/compilation-premiere-handoff`
**Status:** Rencana terkunci. Sudah dikerjakan: **D8 model wiring** (`cc/claude-opus-4-6`, commit `dd275e9`). Sisa backbone belum dimulai.
**Konteks:** Kelanjutan dari refocus VTuber (branch `refactor/vtuber-only-clip-judgment`, judgment sudah tajam & teruji). Ini mengubah **lapisan output**: dari "top-8 clips" menjadi "ekstrak banyak momen di atas ambang → serahkan ke Premiere sebagai timeline siap-poles" (gaya @TriticumClip: long-form compilation kurasi).

## Keputusan (dikonfirmasi user)

| # | Keputusan | Pilihan |
|---|---|---|
| D1 | Mode output | **Serahkan ke Premiere** — program ekstrak + susun timeline; user rakit/poles video long di Premiere. Tidak ada auto-render final. |
| D2 | Bentuk handoff | **1 master VOD + FCPXML** berisi in/out tiap momen. |
| D3 | Koneksi Premiere | **FCPXML dulu** (andal, tanpa plugin), **premiere-pro-mcp nyusul** (one-click). |
| D4 | Volume | **Semua momen di atas ambang kualitas** (durasi total mengikuti). |
| D5 | Subtitle loop (DIREVISI, all-in Premiere) | Loop dipicu dari Premiere: **MCP export AUDIO timeline → app ElevenLabs → proofread → caption EDITABLE (SRT/VTT) balik ke timeline + auto-apply Caption Track Style (Bangers/Bottom/warna) → render final.** ElevenLabs hanya lihat audio final (hemat token). **Word Pop per-kata = spike terpisah** (lihat D5b); jangan diblok olehnya. |
| D5b | Word Pop di Premiere | **Semi-auto (diputuskan).** Coba auto-apply; bila gagal, subtitle tetap jadi **text/graphic di track terpisah** → seluruh subtitle diselect, preset Word-Pop diterapkan **manual sekali** di Premiere. Tanpa overlay alpha. |
| D6 | Ambang skor | **6.0/10** default (`CLIP_FINDER_COMPILATION_THRESHOLD`), berlaku setelah fix `format_fit` sadar-mode + **chat on**. Turunkan ke 5.5 bila chat mati; 6.5 untuk lebih ketat. |
| D7 | Premiere setup klip | **One-click deterministik** (FCPXML + `import_fcp_xml`). Loop subtitle (D5) **butuh MCP** (export audio + import caption) — jalur MCP jadi bagian core untuk subtitle, dengan fallback manual bila MCP ngadat. |
| D8 | Model cari-momen | Dropdown UI: **Gemini (API)**, **Claude (9router non-Kiro = `cc/claude-opus-4-6`)**, **Codex (9router `cx/`)**. ✅ **SUDAH DI-WIRING** (commit `dd275e9`). |
| D9 | Alur aplikasi | Input (URL/instruction/lang/offset/model/nama-projek) → "Start Analyze" jalankan **paralel**: (a) create projek Premiere, (b) download audio-only→analisis momen, (c) download video penuh. Pengisian timeline nunggu (b)+(c) selesai. |
| D10 | "Trim" = non-destruktif | Master 2 jam **tidak dipotong fisik**; FCPXML menaruh hanya in/out momen di master → timeline berisi momen-momen saja. Sumber tetap utuh untuk re-trim. |
| D11 | Startup / launcher | **Launcher entry-point** menjalankan app + Premiere, handshake bridge dengan retry, manual reconnect sbg fallback. **Single-instance guard idempotent**: app/Premiere/bridge/9router yang sudah jalan tidak diduplikasi — reuse + reconnect. |

## Reverse-engineer environment (terverifikasi di laptop)

- **Premiere Pro 2023 (v23.2.0)** → **CEP-based** (UXP baru di PP2024+). Hanya Premiere (tanpa After Effects). Path: `C:\Program Files\Adobe\Adobe Premiere Pro 2023\Adobe Premiere Pro.exe`.
- **node v22.14**, **ffmpeg on PATH** ✓. CEP `PlayerDebugMode` masih off (perlu di-set untuk panel CEP).
- **Port/proses stack:** app web `run_web.py` (uvicorn, **:7860**), 9router proxy lokal **:20128** (dari config), Premiere exe (deteksi by process name).
- **`premiere-pro-mcp` (leancoderkavy)** cocok: **CEP file-IPC bridge**, dukung Premiere **2020–2026** (PP2023 ✓). MCP server (Node) menulis `.jsx`, panel CEP polling → `CSInterface.evalScript()` → DOM/QE. Tool relevan: `import_fcp_xml`, `import_media`, `add_to_timeline`, `insert_from_source`, `export_sequence`. **Kunci: ada tool `import_fcp_xml`** — jadi FCPXML kita jadi satu artifact yang melayani jalur manual DAN jalur MCP.

## Arsitektur baru (alur end-to-end)

```
1 URL
  │  yt-dlp
  ▼
Master VOD (1 file penuh)  ──────────────┐  (referenced, tidak dipotong per-klip)
  │  subtitle JP + chat/audio signals     │
  ▼                                        │
Judgment VTuber (sudah ada, tajam)         │
  │  score tiap momen                      │
  ▼                                        │
Seleksi AMBANG (bukan cap-8):              │
  - semua momen ≥ threshold                │
  - dedup/novelty (anti-repeat)            │
  - urut KRONOLOGIS                        │
  ▼                                        │
FCPXML generator  ◄──────────────────────┘  (in/out per momen di master, fps-akurat)
  │
  ├─► Jalur A (andal): user File>Import di Premiere → sequence jadi
  └─► Jalur B (nyusul): premiere-pro-mcp import_fcp_xml → one-click
  ▼
Premiere: user rakit/poles (cut/zoom/effect) → timeline FINAL
  ▼  klik "auto-subtitle" (loop all-in Premiere, D5)
MCP export AUDIO timeline (WAV) → app ElevenLabs → proofread
  ▼
caption EDITABLE balik ke timeline + auto Track Style (Bangers/Bottom)
  ▼  [Word Pop = spike/semi-auto, D5b]
render FINAL → deliverable
```

> Catatan token: clip-finding pakai subtitle YouTube JP (gratis). ElevenLabs
> baru jalan di TAHAP AKHIR pada hasil edit final — jadi hanya konten yang
> benar-benar tayang yang ditranskrip.

**Prinsip inti:** program **tidak memotong** puluhan file. Ia download **1 master**, lalu FCPXML menaruh tiap momen sebagai clip di timeline dengan titik **in/out** pada master. Editor bisa trim bebas, sedikit file, sound ikut.

## Alur Aplikasi (end-to-end user flow)

**Input (form):** YouTube URL · Instruction clip · Subtitle language (auto-sub YT) · Start offset (mm:ss) · Select AI model · (opsional) Nama projek Premiere.

**Klik "Start Analyze" → jalankan PARALEL:**
```
t0 ── Start Analyze
  ├─(A) create projek Premiere (nama random/custom)  → ~1 dtk, langsung ada
  ├─(B) download AUDIO-only (cepat) ─► analisis momen (subs JP + chat + audio signals)
  └─(C) download VIDEO penuh (2 jam, lama, jalan sendiri)
                │  (A) siap · (B) momen siap · (C) video siap
                ▼
        FCPXML (in/out momen di master) → import ke projek Premiere yg sudah aktif
                ▼
        Timeline berisi momen-momen saja (master 2 jam ter-"trim" non-destruktif)
```
- **Pisah download:** audio-only untuk analisis (cepat) + video penuh untuk Premiere (paralel). Analisis tak perlu nunggu 2 jam video.
- **Ketergantungan urutan:** projek kosong dibuat di awal; **pengisian timeline nunggu video penuh** (Premiere butuh file media di disk untuk link) + analisis selesai.
- **ElevenLabs TIDAK di sini** — analisis pakai subtitle YouTube (gratis). ElevenLabs hanya di loop subtitle akhir (D5).

## Launcher & Orchestration (D11)

**Launcher entry-point** (mis. `launcher.py` + shortcut/`.bat`) memastikan tiap komponen hidup **secara idempotent**, lalu handshake bridge:

| Komponen | Deteksi "sudah jalan?" | Sudah → | Belum → |
|---|---|---|---|
| Auto-Clipping app | HTTP health `127.0.0.1:7860` | reuse (buka browser) | start `run_web.py` |
| Premiere Pro | proses `Adobe Premiere Pro.exe` | jangan relaunch | launch exe |
| Bridge (Node MCP + panel CEP) | ping IPC/port bridge | reuse | start Node server |
| 9router proxy (bila model 9router dipakai) | health `:20128` | reuse | start proxy |

```
launcher (named-mutex: cegah dua launcher balapan)
  → ensure app · ensure Premiere · ensure 9router
  → handshake bridge (retry+backoff, timeout ~60s)
       ├─ online → siap
       └─ gagal  → tombol "Reconnect" (manual) + fallback file (master+FCPXML, File>Import manual)
```
- **Self-healing:** pakai **health-check hidup**, bukan lockfile/PID basi (auto-sembuh bila app pernah crash).
- **Nuansa wajib:** "proses Premiere hidup" ≠ "bridge siap" — cek bridge terpisah. Launcher **tak bisa memaksa panel terbuka dari luar**; jadi tetap perlu **panel di default workspace** (setup 1x) atau **startup-script `.jsx`** (auto, lebih hacky) agar bridge naik saat launch.
- **Idempotent:** menjalankan launcher dua kali = no-op bila semua hidup (cuma reconnect).

## Grill — celah yang harus ditutup di plan ini

1. **⚠️ Konflik `format_fit` (dari refocus Step 4).** `format_fit` sekarang menghadiahi durasi **180-600s** (asumsi klip = video final). Di mode compilation, **momen adalah bahan bangunan pendek** (~20-120s) yang dirakit jadi long-form. Jadi di mode ini `format_fit` harus menghadiahi durasi **momen** (~20-120s), BUKAN 180-600s. → Step 3: buat `format_fit` sadar-mode (compilation vs standalone) atau matikan bobotnya di mode compilation.
2. **Dedup jadi kritis.** Tanpa cap, momen mirip berulang akan merusak compilation. Novelty/dedup harus kuat (bukan sekadar diversify tag).
3. **Frame-accuracy FCPXML.** FCP7 XML berbasis frame; salah fps → semua klip melenceng. Wajib probe fps master via ffprobe dan konversi detik→frame konsisten.
4. **Linking master.** FCPXML `<pathurl>` = path absolut `file://` ke master. Master + .fcpxml harus berdampingan; kalau user pindah, Premiere relink.
5. **Ambang perlu kalibrasi.** Threshold (mis. 6.0/10) harus disetel di VOD nyata; terlalu rendah = compilation kembung, terlalu tinggi = kosong. Butuh eval.
6. **Risiko premiere-pro-mcp.** Third-party, early-stage, QE DOM tak-terdokumentasi, panel unsigned, mode debug. → FCPXML tetap **source of truth**; MCP hanya kenyamanan; pin versi; jangan jadikan jalur wajib.
7. **Compliance Hololive tetap.** FCPXML/handoff sertakan metadata sumber (judul+URL); blokir VOD members-only. (carry-over dari plan refocus Step 9.)
8. **Anti-slop otomatis teratasi.** Langkah edit di Premiere = lapisan orisinalitas → lolos "repetitive content". Arah ini justru menyehatkan monetisasi.

## Implementation Steps

### Step 1 — Seleksi berbasis ambang (ganti cap-8)
**Files:** `processors/clip_finder/selection.py`, `orchestrator.py`, `config.py`
- Tambah `select_above_threshold(clips, threshold, max_safety=60)` — semua momen ≥ threshold, urut kronologis, dedup diperkuat.
- `find_clips` terima `mode="compilation"` / param `threshold`; saat aktif, lewati `select_top_clips` (cap) dan pakai threshold.
- Config: `CLIP_FINDER_COMPILATION_THRESHOLD` (default 6.0), `CLIP_FINDER_COMPILATION_MAX` (safety).

### Step 2 — Download master VOD sekali
**Files:** `processors/clip_finder/downloader.py` (atau modul baru `processors/premiere/source.py`)
- Path "download full master" (bukan per-section). Reuse jika All-In workspace sudah punya source di disk.
- Probe fps + resolusi + durasi via ffprobe (dibutuhkan FCPXML).

### Step 3 — `format_fit` sadar-mode
**Files:** `processors/clip_finder/scoring.py`, `scoring_profiles.py`
- Di mode compilation, `format_fit` menghadiahi durasi momen ~20-120s (bahan compilation), bukan 180-600s. Implementasi: parameter `format="compilation"|"standalone"` atau bobot 0 di compilation.

### Step 4 — FCPXML generator (inti handoff)
**Files (baru):** `processors/premiere/fcpxml.py`, test `tests/test_fcpxml.py`
- Bangun FCP7 XML: `<sequence>` @ fps/resolusi master; per momen `<clipitem>` di V1 + linked A1, `<file>` → master (`<pathurl>` absolut), `<in>/<out>` (frame sumber), `<start>/<end>` (frame timeline, disusun berurutan).
- Nama clip = judul momen; `<marker>`/comment = reason + skor + highlight_type.
- Urut kronologis; konversi detik→frame dengan fps master.
- Unit test: XML valid, jumlah clipitem = jumlah momen, in/out benar untuk fps contoh (mis. 30/60/23.976).

### Step 4b — Subtitle loop all-in Premiere (D5)
**Files:** `web/routes/auto_subtitle.py` (reuse), + integrasi MCP + generator SRT/caption
- **Alur (dipicu dari Premiere):**
  1. MCP `export_sequence` → **export AUDIO timeline** (WAV; bukan render video penuh — hemat waktu).
  2. App: ElevenLabs pada audio itu (hanya konten final → hemat token).
  3. **Proofread gate** (tampilkan transcript untuk koreksi cepat).
  4. App hasilkan **SRT/VTT** → MCP import balik sebagai **caption track editable** di timeline.
  5. MCP auto-apply **Caption Track Style: Bangers + Bottom + warna** (font harus terinstall).
  6. User render final.
- **Word Pop (D5b):** SPIKE terpisah — coba MOGRT Word-Pop di Premiere (tanpa AE). Fallback: user apply preset sekali (semi-auto) / overlay alpha. **Tidak memblok Step 4b.**
- **Catatan token:** clip-finding tetap pakai subtitle YouTube JP (gratis); ElevenLabs hanya di audio final.
- **Ketergantungan:** loop ini butuh premiere-pro-mcp (export audio + import caption + apply track style). Sediakan fallback manual (user export audio sendiri → app → import SRT manual) bila MCP ngadat.

### Step 5 — Route + UI "Compilation handoff"
**Files:** `web/routes/clip_finder.py` (atau route baru), template + JS
- Mode compilation: jalankan pipeline → hasilkan `{master.mp4, compilation.fcpxml, manifest.json}`.
- Tombol download FCPXML + tampilkan daftar momen (waktu, judul, skor) untuk review (gate WAJIB review dari plan refocus Step 8).

### Step 6 — Integrasi premiere-pro-mcp (bridge + create_project + import)
**Files:** dokumen setup + skrip helper bridge
- Setup 1x: `PlayerDebugMode=1` (REG_SZ), install panel CEP ke `%APPDATA%\Adobe\CEP\extensions`, **panel di default workspace** (agar auto-load), jalankan MCP server (Node 20.19+).
- Kapabilitas dipakai: `create_project` (nama projek), `import_fcp_xml` (isi timeline), `export_sequence` (audio final untuk subtitle), `import_media`/`add_to_timeline` (caption balik).
- FCPXML tetap artifact tunggal (melayani jalur manual & MCP). Pin versi mcp.

### Step 7 — Launcher & single-instance orchestration (D11)
**Files (baru):** `launcher.py`, `+ .bat`/shortcut, health-check helpers
- Guard idempotent per komponen (tabel di §Launcher): app `:7860`, Premiere (process), bridge (IPC), 9router `:20128`.
- Named-mutex untuk launcher; handshake bridge retry+backoff+timeout; tombol "Reconnect" manual; fallback file (master+FCPXML) bila bridge gagal.
- Self-healing via health-check hidup (bukan lockfile basi).

### Step 8 — Wiring alur paralel + auto-connect (D9)
**Files:** `web/routes/*` (compilation), orchestrator, downloader
- "Start Analyze" fan-out paralel: (a) `create_project` (nama dari form), (b) download **audio-only** → analisis momen, (c) download **video penuh**.
- Saat (b)+(c) selesai → generate FCPXML → auto-`import_fcp_xml` ke projek aktif (yang dibuat di (a)).
- Field baru: **nama projek Premiere** (opsional; default random).

### Step 9 — Kalibrasi ambang + eval di VOD nyata
**Files:** `tests/`, dataset kecil
- Setel `CLIP_FINDER_COMPILATION_THRESHOLD` di 3-5 VOD; cek panjang compilation & kualitas momen (tak ada repeat, tak ada filler).

## Risiko & Rollback
- **premiere-pro-mcp rapuh, TAPI kini di jalur core** (loop subtitle D5 butuh MCP). Mitigasi: FCPXML klip tetap deterministik & MCP-independent; loop subtitle punya fallback manual (export audio sendiri → app → import SRT).
- **Word Pop tak bisa auto-editable di PP2023** (D5b) → spike + fallback semi-auto/overlay; jangan blok fitur.
- **FCPXML fps salah** → semua drift; mitigasi: probe fps + test multi-fps (Step 4).
- **Ambang salah** → compilation kembung/kosong; mitigasi: Step 7 kalibrasi + safety max.
- **Storage master** → 1 VOD penuh besar; pastikan reuse source All-In & cleanup policy.

## Keputusan final (semua terkunci)
- **D1 Output:** serahkan ke Premiere (user rakit/poles); tak ada auto-render final.
- **D2 Handoff:** 1 master VOD + FCPXML (in/out per momen).
- **D3/D7 Koneksi:** FCPXML deterministik dulu; premiere-pro-mcp untuk one-click + loop subtitle.
- **D4/D6 Volume & ambang:** semua momen ≥ **6.0** (setelah fix `format_fit` sadar-mode + chat on; 5.5 bila chat mati).
- **D5 Subtitle:** loop all-in Premiere — MCP export AUDIO → ElevenLabs (hanya audio final, hemat token) → proofread → **caption editable** balik + auto Track Style **Bangers/Bottom** → render final.
- **D5b Word Pop: semi-auto.** Bila auto-apply gagal → subtitle jadi **text/graphic di track terpisah**, seluruhnya diselect, preset Word-Pop diterapkan **manual sekali**. **Tanpa overlay alpha.**
- **D8 Model:** Gemini (API) + **Claude `cc/claude-opus-4-6`** (9router non-Kiro) + Codex (9router). ✅ **SUDAH DI-WIRING** (commit `dd275e9`).
- **D9 Alur:** Start Analyze paralel — create projek + audio-only→analisis + video penuh; timeline diisi saat semua siap.
- **D10 Trim:** non-destruktif (FCPXML in/out di master; sumber utuh).
- **D11 Launcher:** entry-point + single-instance guard idempotent (app/Premiere/bridge/9router) + handshake retry + manual reconnect + fallback file.
- **Gap antar-momen:** **rapatkan** (jump-cut, tanpa jeda).
- **Precondition:** panel bridge di default workspace (setup 1x) agar auto-load; atau startup-script `.jsx`.

## Sumber
- [premiere-pro-mcp (leancoderkavy) — CEP bridge, import_fcp_xml, dukung PP2020-2026](https://github.com/leancoderkavy/premiere-pro-mcp)
- Reverse-engineer lokal: PP2023 v23.2.0 (CEP), node v22.14, ffmpeg on PATH, tanpa After Effects.
- Adobe: Premiere mengimpor Final Cut Pro 7 XML (`.xml`) & CMX3600 EDL secara native (File > Import).
