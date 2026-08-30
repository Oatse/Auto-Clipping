# Compilation Output + Premiere Handoff — Rencana Matang

**Branch (usulan):** `feat/compilation-premiere-handoff`
**Status:** Rencana review — **belum menyentuh kode.**
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
| D8 | Model cari-momen | Dropdown UI: **Gemini (API)**, **Claude (9router, non-Kiro)**, **Codex (9router, `cx/`)**. Perlu ID model 9router untuk Claude non-Kiro. |

## Reverse-engineer environment (terverifikasi di laptop)

- **Premiere Pro 2023 (v23.2.0)** → **CEP-based** (UXP baru di PP2024+). Hanya Premiere (tanpa After Effects).
- **node v22.14**, **ffmpeg on PATH** ✓. CEP `PlayerDebugMode` masih off (perlu di-set untuk panel CEP).
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

### Step 6 — Integrasi premiere-pro-mcp (nyusul, opsional)
**Files:** dokumen setup + skrip helper
- Setup: set `PlayerDebugMode=1` (REG_SZ), install panel CEP ke `%APPDATA%\Adobe\CEP\extensions`, jalankan MCP server (Node 20.19+).
- Jalur: program hasilkan FCPXML → MCP tool `import_fcp_xml` → sequence muncul one-click. (FCPXML tetap artifact tunggal.)
- Pin versi mcp; treat sebagai kenyamanan, bukan wajib.

### Step 7 — Kalibrasi ambang + eval di VOD nyata
**Files:** `tests/`, dataset kecil
- Setel `CLIP_FINDER_COMPILATION_THRESHOLD` di 3-5 VOD; cek panjang compilation & kualitas momen (tak ada repeat, tak ada filler).

## Risiko & Rollback
- **premiere-pro-mcp rapuh, TAPI kini di jalur core** (loop subtitle D5 butuh MCP). Mitigasi: FCPXML klip tetap deterministik & MCP-independent; loop subtitle punya fallback manual (export audio sendiri → app → import SRT).
- **Word Pop tak bisa auto-editable di PP2023** (D5b) → spike + fallback semi-auto/overlay; jangan blok fitur.
- **FCPXML fps salah** → semua drift; mitigasi: probe fps + test multi-fps (Step 4).
- **Ambang salah** → compilation kembung/kosong; mitigasi: Step 7 kalibrasi + safety max.
- **Storage master** → 1 VOD penuh besar; pastikan reuse source All-In & cleanup policy.

## Keputusan yang sudah dikunci
- Subtitle (D5): loop all-in Premiere — MCP export AUDIO → ElevenLabs → proofread → **caption editable** balik + auto Track Style **Bangers/Bottom**; render final. ElevenLabs hanya audio final (hemat token).
- Word Pop (D5b): **spike** (belum dijamin di PP2023); fallback semi-auto/overlay.
- Ambang (D6): **6.0** (setelah fix format_fit + chat on; 5.5 bila chat mati).
- Premiere setup klip (D7): one-click deterministik (FCPXML + import_fcp_xml), tanpa AI.
- Model (D8): Gemini (API) + Claude (9router non-Kiro) + Codex (9router) — selectable di UI.

## Keputusan final (semua terkunci)
- **Gap antar-momen FCPXML:** **rapatkan** (jump-cut, tanpa jeda).
- **Model D8:** Claude non-Kiro = **`cc/claude-opus-4-6`** (9router). ✅ **SUDAH DI-WIRING** — config `CLIP_FINDER_CLAUDE_MODEL`, routing orchestrator, valid_models (clip_finder + multi_pov), dropdown UI, hint JS. Alias UI: `claude-opus-4.6`.
- **Word Pop (D5b): semi-auto.** Kalau auto-apply gagal, subtitle tetap dibuat sebagai **text/graphic di track terpisah** sehingga seluruh subtitle bisa diselect dan preset Word-Pop diterapkan **manual sekali** di Premiere. **Tidak** pakai overlay alpha.

## Sumber
- [premiere-pro-mcp (leancoderkavy) — CEP bridge, import_fcp_xml, dukung PP2020-2026](https://github.com/leancoderkavy/premiere-pro-mcp)
- Reverse-engineer lokal: PP2023 v23.2.0 (CEP), node v22.14, ffmpeg on PATH, tanpa After Effects.
- Adobe: Premiere mengimpor Final Cut Pro 7 XML (`.xml`) & CMX3600 EDL secara native (File > Import).
