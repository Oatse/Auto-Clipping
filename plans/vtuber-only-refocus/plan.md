# VTuber-Only Refocus — Rencana Matang (v2, Research-Backed)

**Branch:** `refactor/vtuber-only-clip-judgment`
**Fokus:** Cara program menilai sebuah moment layak diklip, ditajamkan untuk niche VTuber, dan dihubungkan ke bentuk editan + kepatuhan legal agar layak jadi **core sebuah channel** (bukan sekadar tool).

> v2 ini menggantikan v1. v1 hanya menajamkan penilaian teknis. v2 menambahkan fondasi riset (persona audiens, taksonomi moment, bentuk editan, batas legal, risiko monetisasi) dan menutup celah yang ditemukan saat grill.

## Implementation Status

Fase 1 — **inti penentuan moment (SELESAI, teruji, ter-commit di branch di atas):**
- [x] Step 0 — Bug fix penilaian: punchline re-anchor lintas boundary, scorer-fallback jadi loud, tie-break cap-10. `986e752`
- [x] Step 1 — VTuber mode selalu aktif (gate `is_vtuber_mode` dibuang). `0f968e8`
- [x] Step 2 — Prompt deteksi VTuber-native (collab-first, chat = suara audiens). `b768cb7`
- [x] Step 3 — Chat driver primer + persona EN-market + warning sadar-chat. `56a6927`
- [x] Step 4 — 5 profil → 1 VTuber + dimensi baru: `interaction_dynamic`, `en_translatability`, `format_fit`. `977dba4`
- [x] Step 5 — Bersihkan routes/UI dari multi-niche (coerce, bukan 400). `727ea9e`
- [x] Step 10a — Regression test invariants penilaian (`tests/test_judgment_invariants.py`).

Fase 2 — **surface produk (BELUM; masing-masing subsistem sendiri):**
- [ ] Step 6 — Novelty lintas-video (butuh publish-history store yang belum ada).
- [ ] Step 7 — Judgment → bentuk editan (format tier → render, emotional beat → telop, EN sub).
- [ ] Step 8 — Anti-slop human-refine gate (**keputusan: WAJIB review**) + lapisan orisinalitas.
- [ ] Step 9 — Compliance Hololive baked-in (deskripsi patuh, blokir members-only).
- [ ] Step 10b — Eval precision@k atas dataset VOD berlabel (butuh data nyata).

---

## 0. Ringkasan Eksekutif

Arsitekturmu **sudah VTuber-first**, jadi ini bukan bangun-dari-nol — ini **membuang generalitas multi-niche + menajamkan judgment berdasarkan bukti nyata tentang apa yang dijadikan klip oleh komunitas VTuber.**

Tiga temuan riset yang mengubah rencana:

1. **Kategori klip #1 adalah relationship/collab (talent × talent), bukan momen "loud" satu orang.** → judgment harus menilai dinamika interpersonal; fitur `multi_pov` naik dari "nice-to-have" jadi **jantung produk**.
2. **Bertahan-monetisasi menuntut orisinalitas.** YouTube men-demonetisasi klip "subtitle + SFX saja" sebagai *repetitive content*; komunitas menolak AI-slop. → produk harus jadi **assist + human-refine + lapisan orisinalitas**, bukan auto-slop.
3. **Translasi EN adalah moat.** 49% audiens internasional memilih EN; virality VTuber digerakkan klip tertranslasi. → "translatability ke EN" jadi dimensi penilaian, bukan afterthought.

---

## 1. Hasil Grill terhadap v1 (celah yang ditutup di v2)

| # | Celah v1 | Ditutup di v2 |
|---|---|---|
| G1 | Prioritas kategori salah — fokus momen "loud/quotable" solo | §3.2 taksonomi + dimensi baru `interaction_dynamic` (§4) |
| G2 | Judgment tidak terhubung ke bentuk editan | §3.3 spec editan + `format_fit` tier durasi (§4) |
| G3 | Risiko demonetisasi/AI-slop tidak ditangani | §3.5 + Step 8 (human-refine gate + originality) |
| G4 | Kepatuhan legal absen | §3.4 + Step 9 (compliance baked-in) |
| G5 | Translasi EN tidak dinilai | dimensi `en_translatability` (§4) |
| G6 | Persona generik, tier durasi & dedup lintas-video absen | §3.1 persona, §4 `format_fit`, Step 6 cross-video novelty |
| G7 | Tidak ada metrik sukses/eval | §6 definisi "done" + eval harness |

---

## 2. Batas Repo (titik keputusan penilaian moment)

- **Deteksi** — `processors/clip_finder/prompts.py::build_detection_prompt()` (~b63). Taksonomi VTuber & aturan buildup/full-cycle/dead-air digate `is_vtuber_mode(instructions)` (`heuristics.py`).
- **Sinyal** — `orchestrator.py::extract_signals()` (~b104); chat di-skip diam-diam saat gagal (~b134). `SignalKind.CHAT_CLIP_INTENT` = prediktor presisi tertinggi.
- **Skoring** — `scoring.py` + `prompts.py::_RATER_PERSONA` (~b250) & `build_scoring_prompt()` (~b300). 8 dimensi (5 generik + 3 VTuber-native).
- **Bobot/ranking** — `scoring_profiles.py` (5 profil). `list_profile_names()` dipakai UI.
- **Model** — `models/clip.py`; `ClipScore.total_for()` (~b193) = fungsi keputusan akhir; `Clip.score_profile` (~b336).
- **Sejalan-fokus (dipertahankan & diperkuat):** `multi_pov.py`, `cross_matcher.py` (collab/satu-momen-banyak-POV), `translator/` (EN sub), `subtitle_effects` + `subtitle_renderer` (bentuk editan).

---

## 3. Fondasi Riset

### 3.1 Persona Audiens (data)

| Atribut | Data | Implikasi ke judgment |
|---|---|---|
| Gender | ~70–80% pria | — |
| Usia | 68% umur 18–24, 22% umur 25–34 | Bahasa/meme muda, mobile-first, tempo cepat |
| Bahasa | 49% internasional pilih **EN**, 31% JP | EN sub wajib; dimensi `en_translatability` |
| Motivasi | Parasosial, overlap anime, "waifuism", akses via klip tanpa nonton full-stream | Personality > event; "topeng terlepas" bernilai tinggi |
| Peran klip | Backbone komunitas, gerbang discovery, translasi = jangkauan | Klip harus **self-contained** untuk penonton non-follower |

**Persona rater (baru, menggantikan yang generik):** *"Editor channel klip VTuber untuk pasar EN. Penonton: pria 18–24, melek anime, sudah menonton ribuan highlight, bosan dengan yang generik. Mereka datang untuk kepribadian, bukan event — baris yang akan mereka quote, momen topeng talent terlepas, interaksi/collab yang bikin ngakak. Momen bisa keras, dramatis, dan tetap tak berharga bagi mereka. Klip harus lucu/mengena bahkan bagi orang yang belum pernah menonton streamer itu."*

### 3.2 Taksonomi Moment — apa yang benar-benar diklip (prioritas)

Diurutkan dari volume klip tertinggi (riset: relationship content = mayoritas besar klip):

1. **Relationship / Collab (PRIORITAS TERTINGGI)** — interaksi talent × talent, banter collab, cerita tentang sesama talent, interaksi manager. *"Vast majority of VTuber clips."* → dinilai oleh dimensi baru **`interaction_dynamic`**.
2. **Comedic / Intense moments** — momen lucu, kacau, reaksi hebat (peta ke `emotional_intensity`, `quotability`).
3. **The mask slipping / juxtaposition** — avatar imut × perilaku tak-terduga/cursed; kepribadian asli bocor (peta ke `character_moment`).
4. **Iconic trope performance** — catchphrase, kebiasaan khas, in-joke (peta ke `quotability`, `novelty`).
5. **Relatability / emotional storytelling** — cerita otentik/menyentuh yang membangun koneksi (peta ke `emotional_intensity`, `replayability`).

Highlight-type enum yang ada (`karma_arc`, `genuine_reaction`, `clutch_play`, `chaotic_plea`, `other`) **diperluas** dengan `collab_dynamic` dan `wholesome`/`emotional` agar cocok taksonomi di atas.

### 3.3 Bentuk Editan (data — dari panduan clipper 2026)

Ini menghubungkan judgment ke output (celah G2). Judgment harus tahu bentuk akhir yang ditarget.

- **Tier durasi (format):** Shorts 15–60s (akuisisi) · Standard 3–10min (watch-time) · Compilation 10–20min (existing fans). → dimensi **`format_fit`** memilih tier per-moment (scream 5s → Shorts; bit collab 4min → Standard).
- **Telop/subtitle:** ≤20 karakter per layar; variasi warna/ukuran per emosi (besar=kejutan, merah=komentar); font legible di mobile. → app sudah punya `subtitle_effects` + `subtitle_renderer`; judgment menandai emotional beat untuk styling.
- **Pacing:** potong senyap ke 0.5–1s; buang pengulangan. → sudah ada `boundary.py` + `timing/sanitizer`.
- **SFX:** "pon/don" pendek di transisi.
- **Buildup:** mulai klip **10–15s sebelum chat-spike peak** (memvalidasi aturan BUILDUP & `coincidence_bonus` app).
- **Thumbnail/metadata:** ekspresi wajah ekspresif; ≤10–15 char, ≤3 warna.

### 3.4 Batas Legal / Compliance (data — wajib untuk core channel)

| Aturan | Hololive | Nijisanji |
|---|---|---|
| Izin klip | Boleh dalam guideline, tanpa pra-registrasi | **Wajib pra-registrasi form (per Mei 2025)** |
| Atribusi sumber | **Wajib**: URL + judul stream di awal deskripsi | Wajib |
| Members-only / konser / konten berbayar | **Dilarang** tanpa izin | Dilarang |
| Content ID / auto-ID sebagai karya sendiri | **Dilarang** | Dilarang |
| Monetisasi | Boleh (ikuti guideline) | Ikuti guideline |
| Takedown | COVER bisa takedown atas kualitas/thumbnail/konten | — |
| Konteks Jepang | **Tidak ada fair use** — izin harus eksplisit | Sama |

→ Step 9: generator deskripsi patuh-otomatis (URL+judul+kreator+link guideline), **blokir VOD members-only**, dan flag agensi Nijisanji agar user pra-registrasi.

### 3.5 Risiko Strategis: AI-Slop & Demonetisasi (eksistensial)

- YouTube men-demonetisasi klip "subtitle/SFX saja" sebagai **repetitive content**; menindak "AI slop".
- Komunitas VTuber aktif menolak channel klip AI low-effort (mis. kritik kreator terhadap AI clip channels).
- **Konsekuensi desain:** produk **tidak boleh** jadi auto-slop end-to-end. Harus: (a) **human-refine gate** (AI mengusulkan, manusia menyetujui/menyunting), (b) **lapisan orisinalitas** (narasi/komentar/animasi/tema editorial), (c) kurasi kualitas (buang yang lemah, jangan spam). Ini juga menyelaraskan dengan sikap komunitas.

---

## 4. Model Judgment yang Direvisi (inti)

Dimensi skor VTuber-only final. Tanda `[+]` = baru dari riset.

**Rubrik LLM (0–10):**
- `retention_hook` — kekuatan 3 detik pertama.
- `emotional_intensity` — payoff emosi genuine (bukan performed).
- `completeness` — setup → climax → aftermath.
- `replayability` — layak ditonton ulang.
- `shorts_friendly` — self-contained tanpa konteks luar.
- `quotability` — baris/noise yang di-repeat komunitas.
- `character_moment` — topeng terlepas / kepribadian asli.
- `novelty` — beda dari kandidat lain (+ lintas-video, §Step 6).
- **`[+] interaction_dynamic`** — kualitas kimia collab/interpersonal (menjawab G1: kategori #1).
- **`[+] en_translatability`** — apakah momen *landing* di EN setelah translasi; pun JP yang tak tertranslasi → rendah (menjawab G5).

**Deterministik (0–10):**
- `audio_peak_db` (lemah — loud ≠ bagus), `chat_spike_ratio`, `duration_fit`, `coincidence_bonus` (audio∩chat), `clip_intent_score` (chat minta klip = bukti terkuat).
- **`[+] format_fit`** — seberapa cocok momen ke tier durasi target (Shorts/Standard/Compilation) (menjawab G2/G6).

**Tabel bobot VTuber tunggal** (menggantikan 5 profil). Prinsip: chat-intent & interaction & quotability & character dominan; loudness lemah. Angka final di-tune saat Step 4 dengan eval harness §6.

---

## 5. Implementation Steps

> Urutan direkomendasikan: **Step 0 (bug fix) → 1 → 2 → 3 → 4/5 (satu-arah) → 6 → 7 (editan) → 8 (anti-slop) → 9 (legal) → 10 (eval)**. Step 0 & 7–9 bisa berdiri sendiri jika ingin nilai cepat tanpa refactor.

### Step 0: Perbaikan bug penilaian (independen, kerjakan duluan)
**Commit:** `fix(clip-finder): preserve punchline, harden scorer fallback, keep top-clip ranking`
**Files:** `boundary.py`, `scoring.py`, `models/clip.py`
- `boundary._copy()` tidak menyalin `punchline_offset`/`score_profile` → salin + hitung ulang offset relatif `start` baru.
- `_llm_rubric` gagal → semua 5.0 senyap → buat terlihat (log + flag job).
- Cap `total` 10.0 bikin klip terbaik seri → simpan skor mentah pre-clamp sebagai tie-breaker.

### Step 1: VTuber mode selalu aktif
**Commit:** `refactor(clip-finder): make VTuber detection mode unconditional`
**Files:** `detector.py`, `prompts.py`, `heuristics.py` (deprecate `is_vtuber_mode`)

### Step 2: Prompt deteksi VTuber-native + taksonomi §3.2
**Commit:** `feat(clip-finder): rewrite detection prompt around VTuber taxonomy`
**Files:** `prompts.py`; `models/clip.py` (perluas `HighlightType`: `collab_dynamic`, `emotional`)
- Instruksi default → bahasa clipper (chat meledak, topeng terlepas, banter collab). Pertahankan kontrak JSON.

### Step 3: Chat sebagai driver primer + persona §3.1
**Commit:** `feat(clip-finder): chat-first judgment + EN-market rater persona`
**Files:** `orchestrator.py`, `scoring_profiles.py`, `prompts.py`
- Chat gagal → tandai job "chat-missing" + turunkan kepercayaan (jangan hilang senyap).
- Ganti `_RATER_PERSONA` → persona EN-market §3.1.

### Step 4: Runtuhkan 5 profil → 1 VTuber + dimensi baru §4
**Commit:** `refactor(clip-finder): collapse to VTuber-only weights; add interaction/translatability/format_fit`
**Files:** `scoring_profiles.py`, `scoring.py`, `prompts.py`, `models/clip.py`
- Hapus PODCAST/NEWS/GAMING/ASMR. Tambah `interaction_dynamic`, `en_translatability`, `format_fit` ke `ClipScore` + prompt + bobot.

### Step 5: Bersihkan model/serialisasi/UI
**Commit:** `refactor: remove multi-niche surface from model and UI`
**Files:** `models/clip.py`, `web/routes/clip_finder.py`, `web/services/*`, `clip_finder.html`, `clipfinder.js`
- `score_profile` vestigial (selalu "vtuber"); job lama di-coerce (jangan raise). Hapus UI profile selector.

### Step 6: Novelty lintas-video (dedup channel)
**Commit:** `feat(clip-finder): cross-video novelty to avoid channel/competitor overlap`
**Files:** `clip_finder/selection.py`, `cache.py`
- Simpan fingerprint momen terklip; turunkan `novelty` jika mirip klip yang sudah pernah dikeluarkan channel (riset: "avoid scene overlap").

### Step 7: Judgment → bentuk editan (jembatan G2)
**Commit:** `feat(clip-finder): map moments to edit form (duration tier, emotion beats)`
**Files:** `clip_finder/*`, `subtitle_effects`, `subtitle_renderer`, `translator/`
- `format_fit` memilih tier; emotional beat menandai styling telop; punchline → SFX/zoom cue; EN sub via `translator`.

### Step 8: Anti-slop — human-refine gate + orisinalitas (jembatan G3)
**Commit:** `feat: human-in-the-loop curation gate + originality layer`
**Files:** `web/routes/clip_finder.py`, UI, `ae_export`/render
- AI **mengusulkan**, user meninjau/menyetujui/menyunting sebelum export. Dukung lapisan orisinalitas (komentar/tema/animasi) agar lolos "repetitive content".

### Step 9: Compliance baked-in (jembatan G4)
**Commit:** `feat: auto-compliant description + members-only guard + agency flag`
**Files:** `downloader.py`, `web/routes/*`, template deskripsi
- Auto deskripsi patuh (URL+judul+kreator+link guideline); blokir VOD members-only; flag Nijisanji (butuh pra-registrasi).

### Step 10: Eval & tuning bobot (jembatan G7)
**Commit:** `test(clip-finder): judgment eval harness + weight tuning`
**Files:** `tests/*`, dataset kecil VOD berlabel
- Lihat §6.

---

## 6. Definisi "Done" & Eval (agar bisa disebut matang)

- **Dataset emas:** 5–10 VOD VTuber (mix solo + collab, JP + EN) dengan klip "ground-truth" yang benar-benar viral/di-request chat.
- **Metrik:** precision@k (dari top-N pick, berapa yang tumpang-tindih ground-truth), coverage recall momen collab, dan cek "tidak ada loud-but-boring di top-3".
- **Tuning:** setel bobot §4 sampai precision@k naik pada dataset; hindari over-fit dengan 1 VOD hold-out.
- **Gate rilis:** tidak ada regresi pada `tests/test_chat_clip_intent.py`, punchline bertahan melewati boundary, scorer-fail tidak senyap.

## 7. Dampak Test

| File | Dampak |
|---|---|
| `tests/test_scoring_profiles.py` | Disederhanakan drastis / sebagian dihapus |
| `tests/test_clip_finder.py` | Update prompt & default VTuber; 3 dimensi baru |
| `tests/test_chat_clip_intent.py` | Diperluas (chat driver primer) |
| (baru) `tests/test_judgment_eval.py` | Harness precision@k |

## 8. Risiko & Rollback

- **Satu-arah:** hapus 4 profil menghilangkan multi-niche. Step 0–3 + 7–9 sudah memberi mayoritas nilai **tanpa menghapus apa pun** — bisa mulai dari situ.
- **Job lama:** deserialisasi `score_profile` lama harus coerce, jangan raise.
- **Backend LLM ganda:** uji prompt minimal di Gemini + satu backend Kiro.
- **Dimensi baru menaikkan token skoring:** batasi via ringkasan per-kandidat (sudah ada pola di `_signals_summary_for`).

## 9. [NEEDS CLARIFICATION]

1. **Target format utama channel:** Shorts, Standard, atau Compilation? Menentukan bobot `format_fit` & default durasi.
2. **Agensi target:** Hololive (bebas), Nijisanji (perlu pra-registrasi), indie, atau campuran? Menentukan seberapa keras guard legal Step 9.
3. **Enum `ScoringProfile`:** simpan bernilai tunggal (aman kompat) atau hapus total? Rekomendasi: simpan `VTUBER` saja.
4. **Human-refine gate (Step 8):** wajib untuk setiap klip, atau opsional/auto-approve di atas ambang skor? Rekomendasi: wajib review, auto-draft.
5. **Urutan:** kerjakan Step 0 + 7–9 (nilai cepat, non-destruktif) dulu, atau langsung full refocus 1–5?

## 10. Sumber Riset

- [Categorizing Hololive Content (taksonomi konten/klip)](https://zhaoliu30.substack.com/p/categorizing-hololive-content)
- [KIRARI — Panduan lengkap 切り抜き 2026 (editan, durasi, legal, monetisasi, AI)](https://www.kirari.io/blog/vtuber-clip-guide)
- [hololive Derivative Works / Clip Guidelines (aturan resmi)](https://hololivepro.com/en/terms/)
- [Anime Feminist — juxtaposition & viralitas VTuber](https://www.animefeminist.com/idols-gone-viral-how-hololive-vtubers-both-subvert-and-reinforce-expectations-of-idol-femininity/)
- [VTuber statistics 2026 (demografi audiens)](https://zipdo.co/vtuber-statistics/)
- [Konvoy — Virtual Characters & Live Streaming (bahasa/pasar)](https://www.konvoy.vc/newsletters/virtual-characters-and-live-streaming)
- [VTuberNewsDrop — sorotan channel klip AI (sentimen komunitas)](https://vtubernewsdrop.com/akuma-nihmune-calls-out-ai-generated-vtuber-clip-channels/)
- [Melon Sour — Walkthrough clip & sub VTuber (konvensi editan)](https://www.melonsour.com/post/clip-sub-vtubers/)
