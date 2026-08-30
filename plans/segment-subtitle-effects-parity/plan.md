# Segment Subtitle Effects + Preview/Render Position Parity

**Branch:** `codex/segment-subtitle-effects-parity`  
**Description:** Menambahkan efek per-segmen (wave horizontal/vertical dan shake soft/medium/expert) serta menyamakan koordinat preview dengan hasil render FFmpeg.

## Implementation Status

- [x] Step 1 — kontrak data, normalisasi, dan displacement deterministik.
- [x] Step 2 — persistence API dan pipeline render.
- [x] Step 3 — panel efek, preview playback, dan drag berbasis content-rect video.
- [x] Step 4 — ASS/FFmpeg frame-sampled effect rendering dan posisi `\\an5\\pos`.
- [x] Step 5 — automated tests, browser smoke QA, dan FFmpeg render smoke QA.

## Goal

Pengguna dapat memilih satu segmen subtitle di timeline/preview, menerapkan efek teks, melihat efek tersebut saat video diputar, lalu mendapatkan gerakan yang sama ketika video dirender. Posisi hasil drag pada preview disimpan sebagai koordinat relatif terhadap frame video yang sebenarnya, bukan terhadap wrapper/letterbox.

## Temuan Repo yang Menjadi Batas Implementasi

- `models/transcript.py` sudah menyimpan `pos_x`, `pos_y`, dan `pos_override`, tetapi `processors/subtitle_renderer.py::_build_ass_content()` belum menggunakannya untuk ASS `Dialogue`.
- Preview menggunakan `.subtitle-overlay` yang memenuhi `.video-preview-wrap`; video memakai `object-fit: contain`, sehingga koordinat saat ini dapat ikut menghitung area letterbox.
- Renderer aktif saat ini selalu melewati ASS/FFmpeg. Jalur Pycaps masih membuat JSON debug, tetapi bukan jalur render utama.
- `web/static/js/effects.js` memiliki `shake` timeline yang menggeser seluruh frame video. Itu tidak boleh dipakai untuk efek teks per-segmen.
- `PUT /api/jobs/{job_id}/transcript` sudah menyimpan segmen dan `style_config`; render mengirim `style_config.transcript`, sehingga field efek per-segmen dapat ikut terbawa jika kontraknya dipertahankan.
- ADR-0001 menetapkan timing ElevenLabs sebagai sumber kebenaran; efek tidak boleh mengubah `start`, `end`, atau `words[].start/end`.

## Kontrak Data yang Diusulkan

Tambahkan field opsional pada setiap segmen:

```json
{
  "effect": {
    "type": "wave",
    "axis": "horizontal",
    "strength": "medium"
  }
}
```

Nilai yang diterima:

- `effect: null` atau field tidak ada: tidak ada efek.
- `type: "wave"`, `axis: "horizontal" | "vertical"`, `strength: "soft" | "medium" | "expert"`.
- `type: "shake"`, `strength: "soft" | "medium" | "expert"`.

Server menormalisasi nilai yang invalid ke `null`, mengisi default yang eksplisit, dan tidak pernah menyimpan parameter arbitrary dari client tanpa clamp. Preset strength memetakan ke amplitude/frequency deterministik; tidak ada `Math.random()`/random server karena preview dan render harus menghasilkan displacement yang sama pada waktu yang sama.

### [NEEDS CLARIFICATION]

Rencana ini mengartikan “wave text” sebagai gerakan sinusoidal satu blok/baris subtitle yang dipilih (horizontal atau vertical). Jika yang dimaksud adalah setiap huruf bergelombang dengan fase berbeda, renderer perlu fase tambahan (layout per-karakter dan biaya render lebih tinggi). Rekomendasi untuk PR pertama: gunakan line-level wave agar parity dengan font/layout browser dan ASS dapat diverifikasi secara deterministik.

## Implementation Steps

### Step 1: Bentuk data efek dan koordinat kanonik

**Commit:** `feat(subtitles): define per-segment effect and normalized position contract`  
**Files:**

- `models/transcript.py`
- `models/subtitle_effects.py` (baru)
- `processors/subtitle_effects.py` (baru; normalisasi preset dan fungsi keyframe bersama sisi Python)
- `tests/test_subtitle_effects.py` (baru)

**What:**

- Tambahkan `effect` pada `TranscriptSegment.to_dict()`/`from_dict()` tanpa mengubah bentuk JSON segmen lama.
- Buat normalizer yang hanya menerima `wave`/`shake`, axis yang valid, dan tiga strength; nilai amplitude/frequency diturunkan dari preset yang terversi.
- Definisikan koordinat posisi sebagai pusat teks dalam persen frame video (`pos_x`, `pos_y` 0–100), dengan clamp dan fallback kompatibel terhadap data lama.
- Definisikan fungsi displacement berbasis `local_time = video_time - segment.start`, durasi segmen, dan konstanta preset. Fungsi ini harus mengembalikan offset X/Y yang sama untuk preview dan render.
- Pastikan fungsi efek tidak menyentuh timing segmen/kata.

**Testing:**

- Unit test normalisasi: null/legacy, tipe invalid, axis invalid, strength invalid, dan clamp.
- Unit test displacement: deterministik, bernilai nol di luar segmen, axis wave hanya mengubah satu sumbu, serta amplitude shake expert > medium > soft.
- Round-trip `TranscriptSegment.to_dict()` → `from_dict()` mempertahankan posisi dan efek.

### Step 2: Persistensi API dan alur render mempertahankan efek

**Commit:** `feat(api): persist segment effects through transcript autosave and render`  
**Files:**

- `web/routes/auto_subtitle.py`
- `web/services/pipeline_runner.py`
- `web/services/transcript_sync.py` (hanya bila diperlukan untuk mempertahankan field saat sync text)
- `web/services/job_models.py` (hanya bila metadata style perlu diketikkan)
- `tests/test_pipeline_runner.py`
- `tests/test_subtitle_effects.py`

**What:**

- Validasi/normalisasi `effect` ketika menerima `segments` dari autosave dan ketika membaca `style_config.transcript` pada render.
- Pastikan edited transcript yang dikirim oleh `render.js` tetap dipakai sebagai sumber segmen render, termasuk `effect`, `pos_x`, `pos_y`, dan `pos_override`.
- Jangan mereset efek saat text sync, split, merge, duplicate, undo, atau reload; duplicate mewarisi efek secara eksplisit dan split memakai efek parent hanya jika aturan UX itu disetujui.
- Simpan kontrak ke `source_transcript.json` tanpa membuang field unknown yang memang didukung, dan pertahankan kompatibilitas job lama.

**Testing:**

- API test autosave → reload transcript → field efek/posisi tetap ada.
- Pipeline test dengan `style_config.transcript` memastikan `TranscriptSegment` hasil render memiliki efek dan posisi yang sama.
- Regression test memastikan timing dan `words` tidak berubah oleh normalisasi efek.

### Step 3: UI pemilihan efek per segmen dan preview real-time

**Commit:** `feat(editor): add selected-segment text effect controls and preview`  
**Files:**

- `web/templates/_partials/editor_right.html`
- `web/static/css/editor.css`
- `web/static/js/state.js`
- `web/static/js/segmentEffects.js` (baru, controller UI/preset)
- `web/static/js/subtitleEngine.js`
- `web/static/js/preview.js`
- `web/static/js/timeline.js`
- `web/static/js/styleControls.js` (hanya wiring jika panel memakai callback style)

**What:**

- Tambahkan panel “Selected segment effect” dengan `None`, `Wave Horizontal`, `Wave Vertical`, `Shake`, dan strength `Soft`, `Medium`, `Expert`; panel disabled ketika tidak ada segmen terpilih.
- Tampilkan effect badge pada blok timeline/transcript agar target segmen jelas; click pada subtitle line/segment tetap memilih indeks yang sama.
- Terapkan efek hanya pada segmen terpilih/segmen yang memiliki field `effect`, bukan pada semua subtitle dan bukan pada timeline camera-shake.
- Gunakan `requestAnimationFrame` dengan fungsi displacement kanonik dan `transform` yang tidak merusak transform posisi (`translate(-50%, -50%)` + offset). Saat video pause/seek, posisi dihitung dari timestamp aktual, bukan akumulasi delta.
- Saat drag subtitle, hitung content rect dari intrinsic video aspect ratio + `object-fit: contain`; konversi pusat teks ke `pos_x/pos_y` frame-relative 0–100. Jangan memakai wrapper letterbox sebagai frame.
- Autosave setelah perubahan effect dan setelah drag posisi; update preview serta timeline tanpa mengubah `start/end/words`.

**Testing:**

- Browser smoke test: pilih segmen A, pilih wave/shake, play/seek/pause, lalu pastikan hanya A bergerak dan effect tetap saat reload.
- Browser smoke test letterbox: gunakan video landscape pada preview portrait/terbatas, drag ke empat kuadran, dan verifikasi titik pusat tersimpan frame-relative.
- Keyboard/mouse regression: timeline select, undo/redo, duplicate, split/merge, dan autosave tidak kehilangan field efek.

### Step 4: Render ASS/FFmpeg dengan posisi dan keyframe efek per segmen

**Commit:** `feat(renderer): render per-segment effects with canonical positions`  
**Files:**

- `processors/subtitle_renderer.py`
- `processors/subtitle_effects.py`
- `tests/test_subtitle_renderer.py` (baru)
- `tests/fixtures/` (fixture ASS/video minimal bila diperlukan)

**What:**

- Pertahankan renderer ASS yang sudah aktif; jangan menghidupkan kembali jalur Pycaps sebagai renderer kedua.
- Untuk segmen tanpa custom position/effect, pertahankan alignment/margin dan animasi global lama agar output existing tidak berubah.
- Untuk segmen dengan `pos_override`, emit `\\an5\\pos(x,y)` memakai `pos_x/pos_y * PlayResX/PlayResY`; anchor center harus sama dengan CSS preview.
- Untuk segmen dengan effect, ganti satu Dialogue event dengan frame-sampled Dialogue events pada FPS video (atau FPS yang diprobe), masing-masing membawa `\\pos` hasil fungsi displacement dan interval frame yang tidak overlap. Gunakan `local_time` yang sama dengan preview.
- Gabungkan style, stroke, glow, background, extra stroke, dan entry animation yang ada; efek baru hanya mengubah translasi subtitle target. Segmen lain tetap satu event biasa.
- Clamp posisi setelah offset agar teks tidak keluar frame dan log jumlah effect event yang dibuat. Bersihkan/escape text tetap memakai helper ASS yang ada.
- Pastikan filter `shake` di `effects.js`/renderer tetap camera-level; beri nama internal berbeda agar tidak tertukar dengan segment text `shake`.

**Testing:**

- Unit test ASS: custom position menghasilkan `\\an5\\pos` yang benar; segment effect menghasilkan event per frame; segmen lain tidak menerima tag/offset.
- Unit test keyframe boundaries: event pertama/terakhir mengikuti `[start,end]`, tidak overlap, dan posisi mengikuti formula canonical pada sample timestamp.
- Regression test output tanpa effect dibandingkan dengan baseline ASS existing.
- Render fixture singkat dengan FFmpeg dan inspeksi `subtitles.ass`/frame output pada timestamp yang sama dengan preview.

### Step 5: Integrasi, parity audit, dan manual QA gate

**Commit:** `test(subtitles): verify preview-render parity and document effect contract`  
**Files:**

- `tests/test_subtitle_effects.py`
- `tests/test_subtitle_renderer.py`
- `tests/test_pipeline_runner.py`
- `docs/adr/0006-segment-effect-and-position-parity.md` (baru)
- `CONTEXT.md` (glossary bila istilah effect menjadi bagian produk)

**What:**

- Dokumentasikan bahwa `TranscriptSegment` adalah source of truth untuk timing, posisi custom, dan effect; global timeline FX tetap terpisah.
- Tambahkan acceptance matrix untuk default position, custom drag position, wave X/Y, shake soft/medium/expert, overlapping speakers, original/refined transcript, dan old jobs tanpa field baru.
- Jalankan satu scenario browser end-to-end: load video → pilih segmen → apply effect → drag posisi → autosave → reload → render → download.
- Ambil screenshot/frame pada timestamp tetap dan bandingkan pusat teks preview vs output render dalam toleransi maksimal 1% frame dimension (target akhir: ≤1 native pixel setelah pembulatan ASS).

**Testing / Manual QA Gate:**

- `pytest` untuk seluruh test suite terkait transcript/renderer/pipeline.
- Start web app dan lakukan scenario end-to-end di atas pada video dengan letterbox dan minimal dua segmen.
- Verifikasi segmen yang tidak dipilih tidak bergerak, audio/timing tidak berubah, efek tetap setelah reload, output dapat diputar, dan tidak ada error pada log FFmpeg.

## Non-goals

- Tidak mengubah timing ElevenLabs atau algoritma sanitizer.
- Tidak mengubah camera-level timeline `shake` menjadi text effect.
- Tidak menambahkan klaim per-karakter wave sebelum keputusan [NEEDS CLARIFICATION] di atas disetujui.
- Tidak menjadikan After Effects export sebagai renderer utama; bila parity AE juga dibutuhkan, itu perlu scope terpisah setelah ASS/FFmpeg lulus QA.
