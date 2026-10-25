# Picture ingestion: where the time goes, and a faster architecture

**Written:** 2026-09-27
**Question:** copying the same photos with `cp` or `rsync` is far faster than
importing them. Which parts of the import cost the time, and how should it be
re-architected, for any kind of storage, while keeping its two requirements:
**correct dates** and **no duplicates**?

**Evidence:**

- the running import on oldserver (execution 1, 17,868 files, 324 GB, from a
  USB hard disk; see ingestion-performance-oldserver.md);
- the code of `io_picture_import_workflow.py` as of commit `13bc080`;
- per-step benchmarks on sample CR3, JPEG and HEIC files, on an Apple
  M-series Mac, warm cache, one thread.

Figures are marked **measured** or **estimated** throughout.

---

## 1. Summary

The import is not slow because it is written in Python. It is slow for three
architectural reasons:

1. **It reads the source disk in the worst possible pattern.** Eight threads
   read eight different files at once from a disk that can serve one read at a
   time. Measured: **25–37 MB/s**, from a disk that should stream about
   **100–130 MB/s** (estimated, typical for this class of drive).
2. **It does the same expensive work more than once.** It read every file
   twice, once to hash and once to copy (fixed on 2026-09-27). It decodes each
   photo once per display size, and fully develops a RAW file for each size
   when the RAW's own embedded JPEG would do **16× faster** (measured).
3. **It runs its stages one after another, with barriers.** The CPU idles at
   ~7% while the disk works, and the source disk idles while the CPU renders.
   Total time is the **sum** of the stages, not the longest of them.

Neither requirement needs any of this. Dates come from a few kilobytes of
header plus the path. Duplicates need a hash of every byte, and the hash can
be computed on the fly as the file is copied; its CPU cost is ~2% of the time
the disk takes to read the file (measured).

**The target:** ingestion that runs at the source disk's sequential speed,
**close to `rsync`**, with derivatives made afterwards in the background.

| For oldserver's 324 GB | Estimated time |
|---|---|
| The code this import started with | **~7.5 h** (probe ~3.2 h, place ~3.5 h, render ~1 h) |
| With copy-while-hashing (committed 2026-09-27) | ~4.2 h |
| + one or two read streams on spinning disks, 4 MB read-ahead | ~1.5–2 h |
| Proposed architecture (§5) | **~1 h**, then rendering in the background |
| `rsync`, for comparison | ~1 h |

Only the probe rate is measured; everything else in this table is estimated
from it and from §3.

**On rewriting in C:** it is not the first lever. The CPU is ~90% idle during
the slow phase, and the heavy work (hashing, JPEG decode, RAW development) is
already C code called from Python. The gains are in the architecture: about
5–7× for this import, and the same kind of gain on other storage. Native code
earns its place later, in one specific spot (§6).

---

## 2. What the requirements actually need

| Requirement | Needs | Needs the whole file? |
|---|---|---|
| **Correct date** (`generate_new_filename`, `resolve_master_datetime`) | EXIF `DateTimeOriginal`, sub-seconds and offset; the folder and file-name patterns; the file's timestamps | **No.** The header only: tens of KB for JPEG, CR3 and HEIC. Some videos keep their metadata at the end, so for those the tail too. |
| **No duplicates** (`resolve`, and the re-check at place) | SHA-256 of the content, compared with the catalog and with the rest of the run | **Yes**, but only once, and it can be hashed while copying. |
| Everything else (name, folder, database rows) | the above | No |
| Derivatives (1200 px, 2048 px, thumbnails, video proxies) | decoding the image | For JPEG and HEIC, yes. For RAW, **no**: the embedded preview is enough (§3.4). |

So a correct import needs **one sequential read of each new file**. What it
does beyond that is the cost to remove.

---

## 3. Where the time goes

### 3.1 The stages today

`scan → probe → resolve → place → render`, with each stage finishing before
the next starts (`parallelism.md`).

| Stage | Work | Bound by | oldserver |
|---|---|---|---|
| scan | walk and stat | metadata I/O | **2 s**, measured |
| probe | `exiftool` over file headers, plus a full read to hash (and, since today, to copy) | **source disk** | 2 h 46 min for 282 GB, still running; **~28 MB/s** average, measured |
| resolve | duplicates and pairs, in SQL | CPU, trivial | seconds |
| place | copy every file again, 4 threads (a rename since today) | source and catalog disks | ~3.5 h, estimated |
| render | 1200 + 2048 + two thumbnails per image, 7 threads | CPU, plus catalog-disk reads | ~40–60 min, estimated |

### 3.2 The source disk: pattern, not speed

Measured on oldserver during probe:

| | 128 KB read-ahead (default) | 4 MB read-ahead |
|---|---|---|
| Throughput | 25.5 MB/s | 36.7 MB/s |
| Average request | 167 KB | 875 KB |
| Time per request | 12.6 ms | 46.5 ms |
| Disk busy | 99% | 99.7% |
| CPU busy (user + system) | ~8% | ~8% |

The disk is saturated, but with head movement, not data transfer. Eight
threads each read a different file, and the enclosure (USB Bulk-Only, queue
depth 1) serves one request at a time. So the head moves between files on
every request. Bigger read-ahead moves more data per trip (+44%), but cannot
remove the trips.

A plain copy reads **one file at a time, start to end**, so the head barely
moves. That, not the language, is why `rsync` is fast.

The same code does the opposite of what each kind of storage needs:

| Storage | What is fast | What the import does |
|---|---|---|
| Spinning disk (USB, SATA, NAS HDD) | 1–2 sequential streams, big reads | 8 interleaved streams: **slow** |
| SSD / NVMe | many parallel reads | 8 parallel: fine |
| Network share (NFS/SMB) | several parallel requests, to hide latency | 8 parallel: fine |
| Same disk for source and catalog | long alternating read and write bursts | interleaved reads and writes: **slow** |

The per-phase settings exist (`IMPORT_PROBE_CONCURRENCY`, …), but nothing
chooses them from the storage, and until today nothing passed them to the
container.

### 3.3 CPU cost per file

Measured on the Mac, one thread, warm cache, in ms per file:

| Step | CR3 (26 MB) | JPEG (5 MB) | HEIC (2 MB) |
|---|---|---|---|
| SHA-256, whole file | 12.7 | 2.5 | 1.1 |
| `exiftool` match tags (batched) | 40.1 | 6.0 | 11.5 |
| `exiftool` full metadata (batched) | 45.6 | 8.2 | 14.8 |
| PIL EXIF (standard images) | n/a | 0.6 | n/a |
| `exiftool` process start, per worker | 92.5 | | |

- **Hashing is negligible.** At ~2 GB/s it costs about 2% of the ~0.7 s oldserver's
  disk needs to read a 26 MB file. On oldserver's CPU, estimated at 400–500 MB/s
  without SHA instructions, still under 10%.
- **`exiftool` is the largest CPU cost in probe.** It runs as a Perl process
  behind a pipe, at 6–46 ms per file, and reads each header separately from
  the source disk: extra head movement on a spinning disk.

### 3.4 Render repeats itself

Measured, ms per file for one size (current code):

| | CR3 | JPEG | HEIC |
|---|---|---|---|
| `create_alternate_version`, 1200 | **1,115** | 196 | 342 |
| `create_alternate_version`, 2048 | **1,134** | 231 | 377 |

- **Every size decodes the original again** (`render_image_task` calls
  `create_alternate_version` once per size). For oldserver's catalog (1200 and
  2048) that is two full decodes per photo. Decoding once for both, with
  `draft`: **209 ms instead of 427 ms** per JPEG (measured).
- **RAW files are fully developed** (`rawpy.postprocess`, full size) for every
  size. Every CR3 sampled carries a **full-size 6000×4000 JPEG preview**
  (1.8 MB). Making the 1200px image from it: **67 ms instead of 1,115 ms, 16×
  faster** (measured). Half-size development, when a preview is too small:
  302 ms.
- Only CR3 goes through `rawpy`. Other RAW formats (NEF, ARW, DNG, RAF…) go to
  PIL, which cannot open most of them, so they get no derivatives.
- oldserver is spared most of this by grouping: 8,798 of its 8,799 RAW files have a
  JPEG twin, and a pair renders from the JPEG. A photographer who shoots RAW
  only would pay the full development on every file.

### 3.5 Barriers leave resources idle

Because each stage waits for the previous one to finish:

- while probe reads the disk for hours, the CPU is ~90% idle;
- while render works the CPU, the source disk is idle;
- **nothing appears in the gallery until probe has finished all 17,868 files**.

Barriers made the parallel import simpler to build and to resume
(`parallelism.md` §6). Their cost is that total time is the **sum** of the
stages.

---

## 4. What is already fixed

| Change | Effect |
|---|---|
| Copy while hashing (`d9c1d86`) | Place renames instead of re-reading: roughly −3.5 h on oldserver (estimated) |
| 4 MB read-ahead on oldserver's USB disks (host setting) | +44% probe throughput, measured |
| Report of the problem (`ingestion-performance-oldserver.md`) | |

---

## 5. Proposed architecture

Two stages that overlap, instead of five with barriers:

```
                    ┌──────────── INGEST (I/O-bound, near rsync speed) ────────────┐
 scan ──► plan ──►  │ per-device readers ──► hash + copy to staging ──► metadata   │──► commit ──► gallery
 (stat)  (order,    │ (1–2 on HDD,            (one sequential read;     (from the  │    (dedup,
         device     │  many on SSD/network)    header kept in memory)    header)    │     name,
         class)     └──────────────────────────────────────────────────────────────┘     rename)
                                                                                     │
                    RENDER (CPU-bound, background, newest first) ◄────────────────────┘
                    decode once → every size; RAW from its embedded preview
```

### 5.1 Plan: know the storage

At scan time, classify the source and the catalog. On Linux, map `st_dev` to
`/sys/dev/block/<major>:<minor>`:

| Class | How to tell | Readers |
|---|---|---|
| spinning disk | `queue/rotational = 1` | **1**, or 2 when the files are large |
| SSD / NVMe | `rotational = 0` | min(8, CPUs) |
| network | no block device; `nfs` or `cifs` in `/proc/mounts` | 4–8 |
| unknown (macOS, containers without `/sys`) | | start at 2 and adapt |

Then **adapt**: measure MB/s every few seconds, and try one reader more or
fewer. On a spinning disk this settles at 1–2. On an SSD it climbs. It works
on storage nobody tested. The `IMPORT_*` variables stay as overrides.

**Order** the reads by folder and name (the walk already sorts), which on a
freshly written disk is close to physical order. Where the filesystem
supports it, sort by the physical offset of each file's first block
(`FIEMAP`) for an extra gain on fragmented disks.

When **source and catalog are the same spinning disk**, read and write in
large alternating bursts (for example 64 MB) so the head is not dragged
between the two areas on every block.

### 5.2 Ingest: one read per file, in the right pattern

For each file, one reader does, in a single sequential pass:

1. read in large blocks (4–16 MB);
2. update the SHA-256;
3. write the block to the staging copy in the catalog (as since `d9c1d86`);
4. keep the **first ~1 MB** in memory (and the last ~1 MB for MP4/MOV,
   whose metadata may be at the end).

Metadata is then parsed **from those bytes in memory**, on a CPU worker. The
reader has already moved on, so the disk never waits for the parser.

- Use an in-process parser for the common formats, JPEG, HEIC, CR2/CR3,
  NEF, ARW, DNG, MP4/MOV (for example `pyexiv2`/exiv2, or a small native
  reader), with `exiftool` as the fallback for anything it cannot read, run
  on the staged copy rather than the source.
- The date rules (`resolve_master_datetime`) do not change: they get the same
  fields from a different reader. The golden comparison
  (`systems/backend/import_golden.py`) proves the dates match the current
  importer on a reference tree before switching.

### 5.3 Duplicates: decided without reading twice

- **Same path, size and time as a file already imported:** skipped at scan,
  as now.
- **Size unlike any file in the catalog:** cannot be a duplicate. Copy and
  hash in one pass. That is almost every new file.
- **Size matches a catalog file:** hash first, then copy only when the hash is
  new. This avoids writing duplicates, which matters when re-importing an
  overlapping phone backup.
- **Duplicates within the run:** by hash, as now. With streaming, the first
  file seen in the sorted walk wins, which stays deterministic.

The SHA-256 is still stored for every file, so later imports compare against
it exactly as today. **Duplicate detection is unchanged: same hash, same
decision.**

### 5.4 Commit as each file finishes

The writer thread (already batched) records each file as soon as its hash and
metadata are known: duplicate check, final name, rename out of staging,
database rows. Photos appear in the gallery **during** the import, not after
it.

RAW+JPEG grouping becomes incremental: when a file's match keys are known,
look up its partner in an index. Pairs almost always sit side by side in the
same folder, so the partner arrives within seconds. Render waits only for the
group decision of that one file.

### 5.5 Render: in the background, without repeating work

- **Decode once, make every size** (and the thumbnails) from that one decode:
  2× fewer decodes for oldserver's catalog (measured above).
- **RAW:** use the embedded preview when it is at least as large as the
  biggest size wanted (16× faster, measured). Otherwise develop at half size
  (3.7× faster than now), and at full size only for the full-resolution size.
  Cover every RAW type LibRaw reads, not only CR3.
- **Newest first**, as now, so the first gallery page fills first.
- **Throttle it** while ingest is still reading, when both share a spinning
  disk. Otherwise let it overlap: ingest is disk-bound and render is
  CPU-bound.
- Videos keep their own lane (ffmpeg is already multi-threaded).

### 5.6 Resume and stop

Nothing is lost. A file is either committed, or it has a staged copy that a
resume adopts (as since `d9c1d86`), or it is read again. Stop still finishes
the files in hand.

---

## 6. Where native code (C, Rust, Go) would help

| Part | Worth it? | Why |
|---|---|---|
| Orchestration (queues, database, naming) | No | The CPU is ~90% idle, and per-file Python overhead is a few ms against 100+ ms of disk time on a spinning disk |
| Hashing, copying | No | Already C (`hashlib`, OS I/O), and it releases the GIL |
| JPEG decode and resize, RAW development | No | Already C (Pillow/libjpeg-turbo, LibRaw). The gain is in doing less (§3.4) |
| **Metadata parsing** | **Yes** | `exiftool` (Perl, over a pipe) is the largest CPU cost in probe, at 6–46 ms per file. An in-process native parser (exiv2, or a small Rust reader) working on the in-memory header would take it to under 1 ms |
| A whole native "ingest core" | Later, maybe | Only if the fast-storage case matters: a million small files on NVMe, where per-file overhead rather than the disk sets the pace. Measure after §5 before deciding |

---

## 7. Plan

Ordered by gain for the effort. Each step keeps the requirements and is
checked with the golden comparison before merging.

| Step | Change | Effort | Expected gain |
|---|---|---|---|
| 1 | **Storage-aware concurrency:** classify each device (§5.1); spinning disk → 1–2 readers and 4 MB+ reads; pass the `IMPORT_*` overrides into the container | small | 3–4× on spinning disks (estimated) |
| 2 | **Render: decode once per photo; RAW from its embedded preview;** all LibRaw formats | small | 2× (JPEG), 16× (RAW), measured per file |
| 3 | **Header-in-memory metadata:** parse from the bytes the hash-and-copy pass read; `exiftool` as fallback | medium | removes the extra header reads from the source disk and most CPU in probe |
| 4 | **Streaming pipeline:** merge probe/resolve/place into ingest (§5.2–5.4); render overlaps | medium–large | total ≈ ingest time, not the sum; photos visible during import |
| 5 | **Adaptive tuning** (§5.1) and the same-disk burst mode | small | covers storage nobody has tested |
| 6 | Native metadata reader, if step 3's Python parser is too slow | medium | only if measured |

Steps 1 and 2 are independent of the rest and could ship this week. Step 4 is
the architectural change, and needs its own spec amending `parallelism.md`.

---

## 8. Implementation: the streaming import (2026-09-27)

Built as a **second ingestion script**,
`systems/backend/workflows/io_picture_import_v2_workflow.py`, beside the phased
one, which is unchanged apart from two hooks it now exposes (its image renderer
and its command-line entry point). Either script can run any import workflow.

| Plan step | Where | Status |
|---|---|---|
| 1. Storage-aware concurrency | `lib/storage_profile.py`: `classify`, `READERS`, `AdaptiveLimiter`; `IMPORT_*` now reach the container (`start-podman.sh`) | done |
| 2. Render once; RAW from its preview; all LibRaw formats | `lib/render_once.py` (also used by the phased import's `--phases render` through the v2 script) | done |
| 3. Metadata without a second disk trip | `_describe`: the same exiftool/PIL helpers, called on the source **straight after its one read**, so the header comes from the page cache | done, differently (below) |
| 4. Streaming pipeline, no barriers | `_StreamRun.phase_stream`: read → describe → commit per file, in source-path order; render lane in the background | done |
| 5. Adaptive tuning; same-disk bursts | `AdaptiveLimiter` hill-climbs on MB/s; 64 MB read/write bursts when source and catalog share a spinning disk | done |
| 6. Native metadata reader | | not done: only if step 3 is measured too slow |

**Where it departs from §5:**

- **Metadata is not parsed from an in-memory copy of the header.** Parsing
  it from bytes would need a second implementation of every tag the date
  rules read, which is exactly where the dates could start to differ. Calling
  the same tools on the same path, immediately after the read, costs no disk
  movement (the bytes are cached) and cannot disagree with the phased import.
- **Reads are ordered by path, not by physical block** (`FIEMAP`). Path order
  keeps each folder together, which is what matters on a freshly written disk.
- **In-run duplicates:** commits happen in source-path order, so the first
  copy of any content is the one with the smallest path, the phased import's
  rule exactly. A later same-content file is hashed and not copied.
- **Pairs and render:** a folder's files go to render once the whole folder is
  read, since its RAW+JPEG pairs are then known. The run's definitive grouping
  is recorded at the end, as before. A pair split across two folders would
  have both files rendered (wasted work, never a wrong record).

**Checked:**

- `test_import_v2.py`: the same decisions as the phased import on the same
  tree; end to end with 1 and 4 readers; a duplicate of the catalog is never
  written; each new file is read once; resume after a stop; the building
  blocks.
- **Golden comparison on 53 real files** (8 CR3, 30 JPEG, 15 HEIC), with
  `import_golden.py`: **zero differences** from the phased import in
  `file_contents` (hash, name, path, `master_datetime` and its source),
  `exif_data` (every field), derivatives (names, sizes, labels), groups and file
  operations.
- The same 53 files, warm cache, SSD (so the CPU side only): **8.6 s → 3.8 s**.
  The spinning-disk gains can only be measured on a spinning disk (§9).
- Launched as the backend launches it (subprocess, `main()`): completes,
  records its execution and all five phase rows.

**Using it:**

- **It is the default** since 2026-09-27: every new storage route's import
  workflow runs it (`app.py` `DEFAULT_INGEST_SCRIPT`), and it is "Ingest
  Files" in the script list; the previous one is "Ingest Files (phased)".
- Workflows created before then keep the phased script until switched: edit
  the workflow and pick `io_picture_import_v2_workflow.py`.
- Back to the phased import for new routes:
  `SHOEBOX_INGEST_SCRIPT=io_picture_import_workflow.py` in
  `deploy/shoebox.env`, then restart the backend.
- **Plan:** after a period of use, move `io_picture_import_workflow.py` into
  `workflows/legacy/`. The streaming script imports it (its writer, recording,
  grouping and entry point), so that move means first folding those parts
  into a shared module, not just moving a file.
- Overrides, also in `shoebox.env`: `IMPORT_READERS` (a fixed count, no
  tuning), `IMPORT_READ_CHUNK_MB`, `IMPORT_META_CONCURRENCY`,
  `IMPORT_RENDER_CONCURRENCY`, `IMPORT_STAGE_COPIES=0`.
- Progress uses the same five rows on the Import Progress page. Here probe,
  resolve, place and render advance together.

---

## 9. Still to measure

- **Single-stream speed of oldserver's source disk,** to confirm the "~100–130
  MB/s" estimate. After the current import finishes:
  ```bash
  sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'
  dd if=/media/usb2/<a large file> of=/dev/null bs=8M status=progress
  ```
- **The same benchmarks on oldserver's CPU** (§3.3, §3.4), to replace the
  M-series figures; the script is easy to rerun there.
- **The real place and render times** of the running import, from
  `import_phases`, once it finishes.
- **The streaming import on oldserver**, against those: the same source into a
  scratch catalog, with `iostat` alongside. The log's last line gives the MB/s
  the reader count settled at.
