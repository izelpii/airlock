# Pet recognition — implementation spec

**Status:** implemented in v5.2.0 (2026-10-09), with patches through v5.2.9
and the grouping and paging fixes of v5.4.24 – v5.4.30 (released in v5.5.0).
Checked against the code at v6.4.4 (Airlock Photos, formerly Shoebox): the
design below holds, and the places where the build departed from it are marked
*Now:* in the text. The main ones:

- **Pets are on by default for a new catalog**, switched on the catalog form,
  not on the Pets page (§10.1). A catalog with no recorded setting is off.
- The two workflows are named **Measure and Match Pets** and **Group Unnamed
  Pets** (§6).
- Automatic naming also requires closeness to the pet's **centre**, and groups
  are trimmed to their core, after single-link grouping chained 7,378 animals
  into one (§6.1, §6.2).
- The model weights are **not baked into the image**; they are bind-mounted
  from `systems/backend-pets/weights/` (§4.2).
- No `/pets/` path on the gateway; the service is `pets.airlock.lan` (§4.5).
- Not built: manual pet boxes, splitting a group, an auto-assign audit table,
  hiding the menu entries when pets are off, a `*_PETS_MODE` setting.
- Pets are also found by **Search** (v6.0.0), whose people-and-pets filter reads
  `pet_detections`; new albums are no longer built from words (v6.1.9) (§9).
- The pet schema (empty tables, the two `people` columns, the view) is
  ensured by `models.py` on every catalog open, pets on or off; "off" means no
  rows, no model and no container.

**Date:** 2026-10-08.
**Supersedes:** the one-page requirement that previously occupied this file; its
asks are carried in §1.1 and nothing has been dropped.
**Read first:** pet-recognition-discussion.md.
That document argues the choices and records three corrections made while
arguing them. This one states the result and says how to build it. Where the two
disagree, this file is wrong and should be fixed.

---

## 1. Scope

### 1.1 In scope — the asks, and where each is answered

| Ask | Section |
|---|---|
| A new page under **Content** for Pets, with People's architecture and UI | §8.1, §8.2 |
| Workflows for recognition | §5, §6 |
| A new backend, or pet recognition folded into an existing one | §4 |
| The APIs needed | §7 |
| New catalog data structures, following the application's own conventions | §3 |
| Group unknown pets for naming, without a card each | §3.5, §8.3 |
| Photographs link to pets as they link to people, for albums and search | §3.6, §9 |
| Optional models — nothing runs for a household with no pets | §10 |

### 1.2 Out of scope, deliberately

- **Pets in videos.** Faces are not detected in videos either; that capability
  is planned, and §5.5 says what this design must do now so that pets arrive
  with it rather than after it.
- **Breed identification.** `species` is recorded (§3.1) because the detector
  gives it away. Breed is a different model and nobody asked.
- **Birds, horses and livestock.** The detector will report them (§4.2); there
  is no identity model for them worth having, so they are detected and not
  identified. See §15.1.
- **Re-using the human face chain for pets.** §3.8 I1 makes this structurally
  impossible, on purpose.

### 1.3 Decisions carried from the discussion

| # | Decision | Consequence here |
|---|---|---|
| D1 | Detection is **catalog-wide** | §5; the backlog run is the one expensive thing this feature does |
| D2 | A pet is a **`people` row** in the same family category as its owners | §3.1; `category` is the household, `kind` is the species |
| D3 | Pets get **their own page**, one card per *named* pet | §8.1 |
| D4 | A photograph **links to a pet as it links to a person** | §3.6 one view, §9 one query pattern |
| D5 | The models are **optional and off by default** | §10; off must cost zero. *Now:* on by default for a new catalog (§10.1) |
| D6 | Unknown animals are **grouped detections, never cards** | §3.5; a `people` row means somebody named it |
| D7 | Detection **shares the decode** with `Auto Detect Faces` | §5; a second pass would re-read 373,020 originals |

---

## 2. Measured baseline

`oldserver`, `family` catalog, 2026-10-08. Every number below is measured, not
estimated, and §12 is the budget they imply.

| | |
|---|---|
| Files in the catalog | 373,020 |
| Face detections | 445,706 |
| People named | 7 |
| `Auto Detect Faces`, longest run | 5.35 h for 194,433 files |
| Effective throughput | ~10 files/s, all threads |
| Of which inference | **~2 %** — the rest is read, decode, downsample, re-encode, POST |

That last row is quoted from
[ai_auto_detect_faces_workflow.py:36-48](../../systems/backend/workflows/ai_auto_detect_faces_workflow.py#L36)
and is the single reason this design looks the way it does.

---

## 3. Data model

All of it lives in each catalog's `workflow_results.db`, beside
`face_detections`. Nothing goes in `catalogs.db`.

### 3.1 `people` gains two columns

```sql
ALTER TABLE people ADD COLUMN kind    TEXT NOT NULL DEFAULT 'person';  -- 'person' | 'pet'
ALTER TABLE people ADD COLUMN species TEXT;                            -- 'dog' | 'cat' | NULL
CREATE INDEX IF NOT EXISTS idx_people_kind ON people(kind);
```

`category` is **not** touched. It is the household — "my pet can be part of my
family, but my sister's cat can be part of her family" — and it already carries
free text for humans ([models.py:744-746](../../systems/backend/models.py#L744)).
A pet takes a category exactly as a person does.

`kind` defaults to `'person'` so every existing row is correct without a
backfill. `species` is NULL for people and may be NULL for a pet whose detection
was ambiguous.

**Everything else on `people` works unchanged**: `name`, `date_of_birth`,
`description`, `profile_photo_file_id`, `people_photos`, `tag_person`. A pet
gets a profile picture and a per-year timeline for nothing.

### 3.2 `pet_detections`

Mirrors `face_detections` ([models.py:363-378](../../systems/backend/models.py#L363))
column for column, because the crop, rescale and display code that will be
copied alongside it depends on that shape.

```sql
CREATE TABLE IF NOT EXISTS pet_detections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id        INTEGER NOT NULL,
    x              INTEGER NOT NULL,
    y              INTEGER NOT NULL,
    w              INTEGER NOT NULL,
    h              INTEGER NOT NULL,
    confidence     REAL,
    person_id      INTEGER,            -- FK -> people.id; NULL until named
    source         TEXT DEFAULT 'Automatic',   -- 'Automatic' | 'Manual'
    species        TEXT,               -- the detector's class: 'dog', 'cat', ...
    image_width    INTEGER,            -- the DETECTION coordinate space
    image_height   INTEGER,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (file_id)   REFERENCES file_contents (id) ON DELETE CASCADE,
    FOREIGN KEY (person_id) REFERENCES people (id)        ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_pet_det_file   ON pet_detections(file_id);
CREATE INDEX IF NOT EXISTS idx_pet_det_person ON pet_detections(person_id);
```

**`person_id`, not `pet_id`.** It is a foreign key to `people.id` just as its
counterpart is, and §3.6's view only works if both columns mean the same thing.
A reviewer will want to rename it; the comment in `pet_schema.py` must say why
not.

**`image_width` / `image_height` are not optional.** Boxes come back in the
detection image's coordinate space (1600 px by default, §5.1), not the
original's. `face_detections` stores these two for exactly this reason and
`extract_and_save_ai_face()` rescales from them. Omitting them produces crops
that are subtly offset — the failure mode `documentation/fixes/` already has
nine files about.

### 3.3 `ai_pet_contents` — the crops and their embeddings

Mirrors `ai_face_contents`, including the `embedding TEXT` column that
[migrate_add_ai_face_embedding.py](../../systems/backend/migrate_add_ai_face_embedding.py)
added to its counterpart. Embeddings are **persisted, not recomputed**: a 512-d
vector is ~2 KB of JSON, perhaps 60 MB across a large catalog, and re-embedding
every crop on every scoring run would make §6.1 cost what §5 costs.

```sql
CREATE TABLE IF NOT EXISTS ai_pet_contents (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path        TEXT    NOT NULL,       -- relative to the catalog root
    file_name        TEXT    NOT NULL,
    file_size        INTEGER,
    sha256_hash      TEXT,
    year             INTEGER,
    month            INTEGER,
    day              INTEGER,
    pet_detection_id INTEGER NOT NULL UNIQUE,
    source_file_id   INTEGER NOT NULL,
    embedding        TEXT,                   -- JSON array, 512 floats
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (pet_detection_id) REFERENCES pet_detections (id) ON DELETE CASCADE,
    FOREIGN KEY (source_file_id)   REFERENCES file_contents (id)  ON DELETE CASCADE
);
```

Crops are written to `<target_dir>/cache/pets-ai/`, beside the
`cache/faces-ai/` that `Auto Detect Faces` already uses. `cache/` is the right
parent: cache-storage-spec.md governs it, and a crop is
derived data that can be rebuilt from the original.

### 3.4 `pet_similarity_scores`

Mirrors `face_similarity_scores`, which `face_recognition_schema.py` defines.

```sql
CREATE TABLE IF NOT EXISTS pet_similarity_scores (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    pet_detection_id INTEGER NOT NULL,
    person_id        INTEGER NOT NULL,     -- the candidate pet
    cosine_distance  REAL    NOT NULL,
    execution_id     INTEGER,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (pet_detection_id, person_id),
    FOREIGN KEY (pet_detection_id) REFERENCES pet_detections (id) ON DELETE CASCADE,
    FOREIGN KEY (person_id)        REFERENCES people (id)         ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pss_detection ON pet_similarity_scores(pet_detection_id);
CREATE INDEX IF NOT EXISTS idx_pss_person    ON pet_similarity_scores(person_id);
```

Scores are **candidates, not decisions.** `pet_detections.person_id` is written
only by a person, or by §6.1 above a threshold the user can see. The distinction
is the one location-spec.md §4.1 draws between a measurement
and a judgement.

### 3.5 `pet_clusters` and `pet_cluster_members`

Mirrors `face_clusters` / `face_cluster_members`, the v4 group-recognition
tables, including `resolved_person_id` as the promotion.

```sql
CREATE TABLE IF NOT EXISTS pet_clusters (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    centroid            TEXT    NOT NULL,   -- JSON array, 512 floats
    member_count        INTEGER NOT NULL DEFAULT 0,
    cohesion            REAL,
    max_member_distance REAL,
    species             TEXT,               -- the modal species of its members
    resolved_person_id  INTEGER,            -- set when somebody names it
    resolved_at         TIMESTAMP,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (resolved_person_id) REFERENCES people (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS pet_cluster_members (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id           INTEGER NOT NULL,
    pet_detection_id     INTEGER NOT NULL UNIQUE,
    distance_to_centroid REAL    NOT NULL,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cluster_id)       REFERENCES pet_clusters (id)   ON DELETE CASCADE,
    FOREIGN KEY (pet_detection_id) REFERENCES pet_detections (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pcm_cluster ON pet_cluster_members(cluster_id);
```

**A cluster is not a pet.** No `people` row exists for an unnamed animal (D6).
Resolving a cluster is a single `UPDATE` plus a write to each member's
`person_id`, exactly as [app.py:8216](../../systems/backend/app.py#L8216) does
for faces, and that is the only automated path by which a `people` row acquires
pet detections.

### 3.6 `subject_appearances` — one surface to search

```sql
CREATE VIEW IF NOT EXISTS subject_appearances AS
    SELECT file_id, person_id, 'person' AS kind, id AS detection_id
      FROM face_detections WHERE person_id IS NOT NULL
    UNION ALL
    SELECT file_id, person_id, 'pet' AS kind, id AS detection_id
      FROM pet_detections  WHERE person_id IS NOT NULL;
```

A view, not a table: it stores nothing, so §3.8 I1 survives it, and there is no
second copy of the truth to drift. `JOIN people` works through it because a pet
*is* a `people` row, which makes "photos with Rex" and "photos with Izelpi" the
same query with a different name.

**Identified detections only.** An unknown has no identity to link *to*, and
unknowns are met as groups (§8.3), not found by searching.

### 3.7 `pet_schema.py` — one definition

A module beside [location_schema.py](../../systems/backend/location_schema.py),
written for the same reason and in the same shape: `ensure_pet_schema(db)`,
idempotent, creating and upgrading, called from **every** path that can be the
first to touch a catalog database —

1. `models.py` on catalog open,
2. the detection ride-along (§5),
3. `pe_identify_pets.py`,
4. `pe_cluster_pets.py`.

Four paths is why this is a module and not four copies. `face_similarity_scores`
had three definitions that drifted, and a catalog created by the wrong one was
permanently missing a column; `location_schema.py`'s docstring records that
history. Adding a column here means editing one list.

### 3.8 Invariants

The implementation is wrong if any of these stops holding, and each should have
a test (§13).

- **I1 — `face_detections` never contains a pet.** This is what keeps the human
  reference set clean *structurally* rather than by a guard: every query that
  builds that set starts `FROM face_detections` and joins outward
  (discussion §6), so a pet with no rows there
  cannot enter it. Nothing may ever write a pet box to `face_detections`.
- **I2 — a `people` row is never created by a model.** Only a person naming
  something creates one (D6).
- **I3 — pet and face embeddings never meet in one table.** A distance between a
  512-d pet vector and a VGG-Face vector is not a number that means anything.
- **I4 — `pet_detections.person_id` is written only by a person or by an
  above-threshold match, never by clustering.** Clustering groups; it does not
  decide.
- **I5 — with pets off, no pet table is read or written and no pet model is
  loaded** (§10).

---

## 4. The service: `backend-pets`

### 4.1 Why a new service rather than an existing one

| Candidate | Verdict |
|---|---|
| `backend-deepface` | **No.** It already carries TensorFlow for DeepFace. The pet embedder's natural form is `transformers` + `torch`; putting a second framework in that image doubles a build that already fails on oldserver's disk, and couples the face pipeline's restarts to pet work. |
| `backend-vision` | **No.** It is an Ollama client, not a model host, and its one job is per-photo descriptions a user asks for. |
| `backend` (the main Flask app) | **No.** One worker, eight threads, a 300 s timeout. [discussion §2] A model holding a lock in that process is how the geocode lock starved the server in October. |
| **new `backend-pets`** | **Yes**, on port **5006** — the next free one after albums at 5005. |

It carries **no deep-learning framework**: both models are exported to ONNX and
run under `onnxruntime`, which is the precedent YuNet already sets inside
`backend-deepface` — a ~1 MB ONNX file in OpenCV's DNN module, no TensorFlow
involved, warmed at startup. Follow that, not DeepFace.

Build it with the [accelerated services](accelerated-services-spec.md)
framework so `SHOEBOX_PETS_MODE` can later move it to a GPU without any caller
changing. Register it in `start-all.sh`'s `BUILDABLE` list and give it a
`--build=pets` alias beside `transcode`.

*Now:* it is registered and `--build=pets` works, but it runs only as a
container; there is no `AIRLOCK_PETS_MODE`. `start-all.sh` starts it only when
some catalog has pets switched on.

### 4.2 The models

| | Detection | Identity |
|---|---|---|
| Model | **YOLOX-Tiny** (Megvii), COCO classes — chosen in §12 | CLIP ViT-B/32 fine-tuned for animal ID |
| Source | [YOLOX](https://github.com/Megvii-BaseDetection/YOLOX) | [AvitoTech/CLIP-ViT-base-for-animal-identification](https://huggingface.co/AvitoTech/CLIP-ViT-base-for-animal-identification) |
| Form | ONNX, 5 M params, 20 MB | ONNX export, 0.2 B params, 512-d output |
| **Licence** | **Apache 2.0** | Apache 2.0 |
| Classes used | `dog`, `cat`, and `bird`/`horse` recorded but not identified | n/a |
| Input | **416 px, fixed by the export** — not configurable, see §12 | 224×224 crop |

**Not YOLO11, and this is settled rather than open — see §4.2.1.** The
discussion and the first draft of this spec both named YOLO11n, which is the
better detector on paper. It is AGPL-3.0, and Shoebox is proprietary. They
cannot ship together.

#### 4.2.1 Why the detector had to change

[license.txt](../../license.txt) licenses Shoebox as *"Personal &
Non-Commercial Use Only"*, "Copyright © 2005-2026 Christian E. Loza. All rights
reserved", granting each user a *"non-transferable, non-sublicensable"* licence
with "all rights not expressly granted … reserved".

AGPL-3.0 requires the opposite of every one of those words: that whoever
receives the work gets the complete corresponding source, under AGPL, with the
right to modify and redistribute it. **A proprietary EULA and an AGPL component
in one distributed work are not reconcilable**, and Ultralytics is explicit that
this is the intended effect — AGPL-3.0 "requires that the complete corresponding
source of your application, including anything that links to the model **or
serves it over a network**, is made available under the same license", and an
[Enterprise Licence](https://www.ultralytics.com/license) is what they sell to
those who cannot do that.

Three details that make this worse than it first looks:

1. **Exporting to ONNX changes nothing.** The obligation attaches to the model,
   not to the Python package, so the §4.1 decision to avoid the `ultralytics`
   library does not avoid the licence.
2. **§4.1's separate service does not insulate Shoebox either.** AGPL's §13
   network clause reaches a program that users interact with remotely, and
   Ultralytics reads "serves it over a network" as covered. Putting the model
   behind `backend-pets`' HTTP boundary is an architecture decision, not a
   licensing one.
3. **Private single-household use triggers nothing at all.** If Shoebox were
   never conveyed to anyone and no one else ever reached it over a network,
   AGPL would impose no obligation. But Shoebox has an end-user licence
   agreement written for people who download and install it, multi-user
   authentication, and the ability to pack and publish a deployment to a
   server — so it is built to be distributed, and designing a feature that is
   only lawful while it never is would be a trap for a later release.

So the detector is Apache-2.0 from the start. Permissive COCO detectors that
fit this design:

| Model | Licence | Note |
|---|---|---|
| **YOLOX-Tiny** | Apache 2.0 | **The choice, by measurement** — §12. 416 px, 91 ms/image, same detection rate as YOLOX-S |
| ~~YOLOX-Nano~~ | Apache 2.0 | **Rejected by measurement** — twice as fast and finds half the animals (§12) |
| RTMDet-tiny | MIT | Comparable, from MMDetection; heavier dependency story |
| RF-DETR | Apache 2.0 | More accurate, transformer, heavier — overkill for "is there a dog" |
| LibreYOLO | MIT | A permissive YOLO reimplementation; newer and less proven |

**The honest trade:** YOLOX-Nano scores materially lower than YOLO11n on COCO
mAP. That metric is dominated by small and crowded objects, and a dog in a
family photograph is neither — it is large, central and unoccluded. The claim
that this costs us little is plausible and **unverified**, so §12's benchmark
counts animals found against a human count rather than trusting any mAP figure.
If YOLOX-Nano turns out to miss animals, go up to YOLOX-Tiny or RTMDet before
reconsidering a licence.

A stack-wide view of this question, including the parts of Shoebox that would
block a commercial relicensing regardless of pets, is in
commercial-licensing-audit.md. The short of it:
the face identity model is a bigger problem than the pet detector was.

**The pet embedder is Apache 2.0** and fine. One thing to glance at rather than
assume: it was trained partly on PetFace, which is published for research. The
publisher released the weights under Apache 2.0, and Shoebox's own licence is
non-commercial, so the two are aligned — but if Shoebox's licence ever becomes
commercial, this is a second thing to re-check, not just the detector.

Weights are **baked into the image**, not downloaded at runtime. A household
behind a slow line should not discover a missing model the first time a workflow
runs, and `backend-deepface` already warns rather than silently downloading
([backend-deepface/app.py:173](../../systems/backend-deepface/app.py#L173)) —
which is a warning that fires too late. Bake them; fail the build if absent.

*Now:* not done that way. The image carries no weights; `start-podman.sh`
bind-mounts `systems/backend-pets/weights/`, warns at start for a missing
file, and the endpoint that needs it answers 503 (`/health` says which are
present). `tools/fetch_pet_weights.sh` fetches the detector and
`tools/export_animal_clip.py` exports the identity model to ONNX.

### 4.3 Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/health` | — | `{status, models: {detect, embed}, present, loaded, …}` |
| `POST` | `/detect` | multipart JPEG + `max_dim` | `{detections: [{x, y, w, h, confidence, species}], width, height, size}` |
| `POST` | `/embed` | multipart JPEG crop | `{embedding: [512 floats], dims, model}` |
| `POST` | `/embed-batch` | multipart, up to N crops | `{results: [{index, embedding} or {index, error}], dims, model}` |

`/detect` takes the **already-prepared** JPEG and returns boxes in that image's
coordinate space, together with the `width`/`height` it used, so the caller can
persist them into §3.2 without guessing. This mirrors `backend-deepface`'s
`/api/upload` contract deliberately.

`/embed-batch` exists because §6.1 embeds thousands of crops in a row and a
round trip per 224 px crop is almost all overhead.

### 4.4 Concurrency, and the lesson already paid for

**No module-level lock may be held across inference.** In October,
`geocode.nearby()` held one across a 60 s HTTP call in an eight-thread server
and the backend appeared to be down. The fix there — a semaphore that reserves a
slot, with the slow work outside the lock — is the pattern to copy.

- `PETS_CONCURRENCY` (default 2) bounds concurrent inference.
- A queue wait, then a clear 503, rather than an unbounded wait.
- Models loaded once at startup and warmed with a synthetic image, as
  `backend-deepface` warms YuNet.
- Memory budget: both ONNX models under ~400 MB resident. Measure and record it;
  oldserver is not a large machine.

### 4.5 Gateway and naming

Add `upstream shoebox_pets { server ${UPSTREAM_HOST}:5006; }` to
shoebox.conf.template, a
`/pets/` location block, and `pets.shoebox.lan` to the certificate names in
`make-certs.sh`. `backend-pets` is reached **only** from the backend and its
workflows, never from the browser — unlike `backend-vision`, which the frontend
calls directly. Routing it through the gateway anyway keeps the service naming
uniform and costs nothing.

*Now:* the template is
[airlock.conf.template](../../systems/gateway/airlock.conf.template), with
`upstream airlock_pets` and a server block for `pets.airlock.lan` (plain HTTP,
like the other service names). The `/pets/` location block was removed: `/pets`
is a frontend route, and the block made reloading or bookmarking the Pets page
return the detector's 404.

---

## 5. Detection: riding along with `Auto Detect Faces`

### 5.1 The contract

Inside the existing per-file loop of
[ai_auto_detect_faces_workflow.py](../../systems/backend/workflows/ai_auto_detect_faces_workflow.py),
`prepare_detection_jpeg()` already produces `(jpeg_bytes, width, height)` at
`DETECTION_MAX_DIM` (1600 px). That tuple is POSTed to `backend-deepface`. With
pets enabled it is **also** POSTed to `backend-pets/detect`, and the boxes are
written to `pet_detections` with the same `image_width`/`image_height`.

> **Principle.** Pet detection never opens a file. It is handed a decoded image
> by whatever already opened it.

This is the whole point: the ~97 ms of read, decode, downsample and re-encode is
paid once for both detectors. A separate pass would pay it again over 373,020
files — about ten hours of re-reading the easystore to rebuild an image that had
already been produced and discarded.

### 5.2 What it must reuse, or it silently does the wrong thing

- **`get_alternative_version_for_detection()`** — a RAW+JPG pair detects against
  the JPG. Not reusing this re-decodes 8,590 RAW files on oldserver for nothing, and
  that number is measured, not hypothetical.
- **The `image_groups` exclusion** in `get_catalog_images()`: one scan per
  photograph, never both halves of a pair.
- **The per-file watermark**, so a resumed run does not redo finished files. Pets
  need their *own* watermark: a file may have been face-scanned before pets were
  switched on, and must still get a pet pass. A column on `file_contents`
  (`pets_scanned_at`) or a `NOT IN (SELECT file_id FROM pet_detections)`
  population query — the first is cheaper and matches how faces track it.
- **A consecutive-failure limit** — but *not* the face one, and this is where
  the implementation deviates from what this section first asked for. The
  original text said the run must *abort* if `backend-pets` is down, by analogy
  with `MAX_CONSECUTIVE_FAILURES`. That is wrong here, and the reason is the
  asymmetry between the two detectors: faces are the feature people already
  depend on, animals are new. Aborting means a pet service nobody noticed had
  died silently stops faces being detected at all.

  So pets get their own counter (`PETS_MAX_CONSECUTIVE_FAILURES`, default 10).
  Past the limit the ride-along switches **itself** off for the rest of the run,
  says so once, and faces continue to completion. Nothing is marked
  `pets_scanned_at` for a file the detector never saw, so fixing the service and
  re-running picks up exactly where it stopped — which is the property the
  abort was reaching for, without taking the face pass down to get it. The run
  summary prints the mid-run shutdown explicitly, so "hours of progress against
  zero results" is still impossible to mistake for success.

### 5.3 The gate

```python
pets_on = get_catalog_property(catalog_db_path, 'pets_enabled') == '1'
```

Read **once per run**, not per file. With pets off: no HTTP session opened, no
schema ensured, no table touched, and the workflow's runtime is unchanged
(§3.8 I5).

### 5.4 Progress and stopping

`Auto Detect Faces` keeps one progress counter over files, and a file is done
when both detectors have answered. Stop mode stays `'item'`: a stop between
files leaves earlier files fully scanned for both faces and pets, which is the
property `'item'` promises. **Do not add a second counter** — two progress bars
for one pass over one population is how a stalled run becomes unreadable.

### 5.5 What the coming video capability requires of this now

Faces in videos are planned. When frames arrive, §5.1's principle applies
harder: pulling a frame from H.264 costs a seek, a parse and the decode of
everything since the last keyframe. Two obligations, both nearly free now and
awkward later:

1. **One frame extractor, two consumers.** Whatever picks frames picks them
   once. Sampling separately for pets would find the dog at 0:14 and the child
   holding him at 0:12, with no way in the schema to say they were together.
2. **Whatever time column video faces need, `pet_detections` takes the same
   one** — frame index, PTS, millisecond offset. Add it to one table only and
   §3.6's view cannot carry it, so "the moment Rex appears" becomes
   unanswerable through the surface built to answer it.

Write the detector as `detect(image_bytes, width, height) -> boxes`, knowing
nothing about files. That is also what makes it testable without a catalog.

---

## 6. Workflows

Two new scripts. Detection is not one of them — it is §5.

### 6.1 `pe_identify_pets.py` — "Identify Pets" (now "Measure and Match Pets")

**Reads** `pet_detections` without an embedding; **writes** `ai_pet_contents`
(crop + embedding) and `pet_similarity_scores`; may write
`pet_detections.person_id` above a threshold.

Phases, each resumable:

1. **crop** — for each detection with no `ai_pet_contents` row, cut the box from
   the display copy, rescaling from `image_width`/`image_height` exactly as
   `extract_and_save_ai_face()` does. Write to `cache/pets-ai/`.
2. **embed** — batch crops to `/embed-batch`, store `embedding` as JSON.
3. **score** — for each unidentified detection, cosine distance to every
   *named* pet's reference set (the embeddings of detections whose `person_id`
   is set). Upsert into `pet_similarity_scores`.
4. **auto-assign** — where the best match is below `PET_MATCH_DISTANCE` **and**
   the second best is at least `PET_MATCH_MARGIN` further away, write
   `person_id` and record it in an audit table, mirroring `auto_tag_audit`. Both
   thresholds are configurable and both default conservatively: a wrong
   auto-assignment is worse than an unnamed dog, because the user has to find it
   to undo it.

   *Now:* `PET_MATCH_DISTANCE` 0.22 and `PET_MATCH_MARGIN` 0.08, plus a third
   condition, `PET_MATCH_CENTRE_DISTANCE` 0.28: the animal must also be that
   close to the pet's centre (the mean of all its photographs), not merely to
   one of them. With one pet the margin is always met, and the nearest-photograph
   rule alone chained thousands of other animals onto her. There is no audit
   table for pets.

Scoring is **not** restricted by species. It was, on the reasoning that "a cat
crop scored against dog references is wasted work and a source of error", and
that was wrong twice: `sim = q_m @ ref_m.T` computes every pair regardless, so
the filter saved nothing, and the species label is wrong often enough to cost
real matches. See §12.2.

**Why there is no `pe_ingest_manual_pets.py`.** Faces need one because a manual
tag arrives as a region with no crop or embedding, so
`ai_ingest_manual_faces.py` exists to build the reference set. Pets do not: every
detection is embedded in phase 2 whether named or not, so naming one *is*
adding it to the reference set. One fewer workflow, and worth a comment in
`defaults.json` so nobody adds it for symmetry.

### 6.2 `pe_cluster_pets.py` — "Group Unknown Pets" (now "Group Unnamed Pets")

**Reads** `ai_pet_contents.embedding` for detections with `person_id IS NULL`;
**writes** `pet_clusters`, `pet_cluster_members`.

- Agglomerative grouping on cosine distance, one group per animal, with
  `cohesion` and `max_member_distance` recorded so §8.3's slider means
  something — the same mechanism
  [IdentifyGroups.js:25](../../systems/frontend/src/views/IdentifyGroups.js#L25)
  already uses.

  *Now:* crops closer than `PET_CLUSTER_DISTANCE` (0.28) are linked, and each
  finished group is **trimmed to its core** — no member further than that from
  its centre — with the rest grouped again. Linking alone chained 7,378 of
  `family`'s 7,471 animals into one group; trimmed, the one dog is a clean
  5,021.
- Groups below `PET_CLUSTER_MIN_MEMBERS` (default 3) are not offered: a group of
  one is a detection, and offering it as a group to name wastes the user's
  attention.
- **Rebuilds unresolved groups; never touches resolved ones.** A group with
  `resolved_person_id` set is a decision and is left alone, exactly as an
  accepted location cluster is.
- `species` on the group is the modal species of its members, and members
  disagreeing is a signal the group is wrong — log it.

Stop mode `'item'`: the delete-and-rebuild is scoped per group.

### 6.3 Registration — all five places

A workflow registered in four of these is correct and inert. This was learned by
doing it (location-spec.md §7.1).

**1. `workflows/defaults.json`** — bind-mounted, read fresh, no rebuild needed.
`"default": false` for both, because D5 says off by default; §10 creates them.

```json
{ "script": "pe_identify_pets.py", "name": "Measure and Match Pets",
  "description": "Crop each detected animal, measure it, and score it against the pets you have named.",
  "type": "AI / ML", "ingestion": false, "default": false, "order": 80 },
{ "script": "pe_cluster_pets.py", "name": "Group Unnamed Pets",
  "description": "Group animals that look like the same one, ready to be named.",
  "type": "AI / ML", "ingestion": false, "default": false, "order": 90 }
```

**2. `workflow_scheduler.py` → `WORKFLOW_WRITE_SETS`**

```python
'pe_identify_pets.py': {'ai_pet_contents', 'pet_similarity_scores', 'pet_detections'},
'pe_cluster_pets.py':  {'pet_clusters', 'pet_cluster_members'},
```

Kept apart on purpose, as `ge_extract_locations` and `ge_resolve_locations` are:
disjoint write sets are what let the two run independently. Note that
`ai_auto_detect_faces_workflow.py`'s write set must **gain `pet_detections`**,
since §5 has it writing that table.

**3. `workflow_scheduler.py` → `WORKFLOW_STOP_MODES`**

```python
'pe_identify_pets.py': 'item',
'pe_cluster_pets.py':  'item',
```

**This map also derives `_is_subprocess_workflow`.** Omit an entry and the
workflow runs on the threading path, where `workflows/` is not on `sys.path`,
and dies with `No module named 'lib'`. That is a field error that has already
happened twice.

**4. `workflow_triggers.py` → `DEPENDS_ON` + `DEPENDENCY_REASONS`**

```python
'pe_identify_pets.py': {'ai_auto_detect_faces_workflow.py'},   # pet_detections
'pe_cluster_pets.py':  {'pe_identify_pets.py'},                # the embeddings
```

Reasons, in the table's own idiom:

```python
('pe_identify_pets.py', 'ai_auto_detect_faces_workflow.py'):
    'pet_detections — animals are found during the face pass, which shares the decode',
('pe_cluster_pets.py', 'pe_identify_pets.py'):
    'ai_pet_contents.embedding — there is nothing to group until the crops are measured',
```

**5. `workflow_triggers.py` → `TRIGGERS`** — the one that actually enqueues.

| Event | Enqueues | Quiet |
|---|---|---|
| `import_completed` | `pe_identify_pets.py`, `pe_cluster_pets.py` | existing (0 s) |
| `pets_detected` (new; raised when `Auto Detect Faces` completes) | `pe_identify_pets.py`, `pe_cluster_pets.py` | 0 s |
| `pets_identified` (new) | `pe_cluster_pets.py` | 0 s |
| `pets_changed` (new; a pet named, renamed, merged or deleted) | `pe_identify_pets.py`, `pe_cluster_pets.py` | 120 s (`QUIET_PETS_CHANGED`) |

(As built; the design had each event enqueue only the first workflow, and
`pets_identified` wait 60 s.)

`pets_detected` is **not** conditional on that run having written any
`pet_detections` rows, although an earlier draft of this table said it was. No
result dict survives the workflow subprocess for the scheduler to read, and the
pet chain is only *registered* on a catalog with pets switched on — so on every
other catalog `fire()` finds nothing to enqueue and it costs nothing. On a
pets-on catalog the cost of being wrong is two workflows that query, find
nothing new and finish, which is the same bargain `faces_detected` already
makes. `Auto Detect Faces` therefore raises two triggers on completion, and
`triggers_for_completed_workflow()` returns a tuple for that reason.

`pets_changed` matters: naming a pet changes the reference set, and without a
re-score every photograph of Rex taken before he was named stays unidentified —
the same silent loss that a missing `events_changed` trigger would have caused
for inherited locations.

---

## 7. APIs

### 7.1 The shape of this section's one decision

A pet is a `people` row (D2), so **the people endpoints are extended with a
`kind` parameter rather than duplicated under `/pets`.** Two URL families for
one table would mean two implementations of create, update, delete, photos,
profile photo and tags, and they would drift. New endpoints appear only for
things that have no people equivalent: detections, groups, species and the
switch.

The frontend hides this: `petAPI` in `services/api.js` wraps the people calls
with `kind: 'pet'` so `Pets.js` never writes the word "people" (§8.5).

### 7.2 Extended — existing endpoints, one new parameter

| Method | Path | Change |
|---|---|---|
| `GET` | `/api/catalogs/<cid>/people` | `?kind=person\|pet\|all`, **default `person`** |
| `POST` | `/api/catalogs/<cid>/people` | body accepts `kind`, `species`; `kind` defaults to `person` |
| `GET` | `/api/catalogs/<cid>/people/<pid>` | returns `kind`, `species` |
| `PUT` | `/api/catalogs/<cid>/people/<pid>` | accepts `species`; **`kind` is immutable** |
| `GET` | `/api/catalogs/<cid>/people/<pid>/detected-photos/<year>` | for a pet, reads `pet_detections` |

**`?kind` defaults to `person`, and that default is the load-bearing part.**
Every existing caller — the People page, the face tagger, the album editor, the
user-linking dialog — keeps its present behaviour with no change and, crucially,
**cannot accidentally offer a pet as an identity for a human face.** That is the
one filter §3.8 I1 depends on (discussion §6). An endpoint defaulting to `all`
would put the trap back.

**`kind` is immutable after creation.** Changing a dog into a person would
orphan its `pet_detections` into a row that the face chain now considers a
reference. Return 400.

### 7.3 New — pets as a subject

| Method | Path | Returns / does |
|---|---|---|
| `GET` | `/api/catalogs/<cid>/pets/<pid>/detections?year=&limit=&offset=` | one page of the animal's boxes, newest first, with crop URLs, file ids and real totals |
| `GET` | `/api/catalogs/<cid>/pet-detections/<did>/crop` | the crop image; `Cache-Control: immutable`, as the image endpoints already do |
| `POST` | `/api/catalogs/<cid>/pet-detections/<did>/assign` | `{person_id}` — name one detection |
| `DELETE` | `/api/catalogs/<cid>/pet-detections/<did>/assign` | unname it; the detection survives |
| `POST` | `/api/catalogs/<cid>/pet-detections` | a manual box: `{file_id, x, y, w, h, species, person_id}`, `source='Manual'`. *Not built.* |
| `GET` | `/api/catalogs/<cid>/images/<fid>/pet-detections` | every animal in one photograph, for the viewer's overlay *(added)* |
| `PUT` | `/api/catalogs/<cid>/pets/<pid>/profile-photo` | the starred card picture, §8.1 *(added)* |

### 7.4 New — groups

Mirrors `/faces/clusters` ([app.py:8027](../../systems/backend/app.py#L8027),
[8126](../../systems/backend/app.py#L8126)).

| Method | Path | Returns / does |
|---|---|---|
| `GET` | `/api/catalogs/<cid>/pets/clusters?min_members=&max_members=&species=` | unresolved groups, biggest first, each with sample crops (*now* filtered by size, not cohesion) |
| `POST` | `/api/catalogs/<cid>/pets/clusters/<clid>/assign` | `{person_id}` **or** `{name, species, category}` to create the pet and resolve in one call |
| `POST` | `/api/catalogs/<cid>/pets/clusters/<clid>/reject` | not an animal, or not worth naming; it stays out of future offers |
| `POST` | `/api/catalogs/<cid>/pets/clusters/<clid>/split` | *optional, §15.3; not built* |

The `{name, species, category}` form is the one the UI will use most: naming a
group is a single action that creates the `people` row, sets `kind='pet'`,
resolves the group and writes `person_id` to every member. One request, because
anything else leaves a half-named animal if the second call fails.

### 7.5 New — the switch

| Method | Path | Returns / does |
|---|---|---|
| `GET` | `/api/catalogs/<cid>/pets/settings` | `{enabled, detections, named_detections, pets, embedded, unresolved_groups, species}` |
| `PUT` | `/api/catalogs/<cid>/pets/settings` | `{enabled: true}` sets `pets_enabled` **and creates the two workflows** (§10). *Removed in v5.2.2:* the switch is `pets_enabled` on `POST`/`PUT /api/catalogs`, §10.1 |

`GET` returning the counts lets the page say "4,812 animals found, 2 named, 7
groups waiting" rather than offering a bare toggle.

### 7.6 Auth

Everything above is under `/api/`, so [auth.py](../../systems/backend/auth.py)'s
default-deny applies and **nothing is added to `PUBLIC_PATHS`**. Reads need a
viewer; `assign`, `reject` and `settings` need an editor, enforced where
`_enforce_catalog_role()` already enforces the rest.

---

## 8. Frontend

### 8.1 `Pets.js` — the page

Under **Content**, directly after People, at `/pets`. A copy of
[People.js](../../systems/frontend/src/views/People.js) with four differences,
and no more than four — a near-copy that drifts is worse than a shared
component, so if a fifth appears, extract instead.

1. Lists `kind=pet`.
2. The create/edit modal has a **species** select (`dog`, `cat`, `other`) where
   People has none, and keeps name, date of birth, description, category and
   tags.
3. The category select is the **same** `peopleCategoryAPI` vocabulary, so a pet
   joins `Family` beside its owners (D2). It is not a separate list.
4. A header strip showing the §7.5 counts, with a link to Identify pets when
   groups are waiting, and the enable switch when pets are off. *Now:* with
   pets off it points at the catalog settings instead (§10.1).

**One card per named pet.** No cards for unknowns, ever (D6).

**The card's picture**, added 2026-10-09, mirroring People with one deliberate
difference. A person's profile photo is the whole file plus a face box the
frontend crops to at render time; a pet already *has* a crop on disk, written
by the identify pass and served by `/pet-detections/<id>/crop`, so the card
points straight at it. Re-cropping an animal that has already been cropped
would be work in exchange for a worse picture.

Two sources, in order:

1. **The starred one.** A star on any photograph in `PetDetail`'s gallery makes
   it the card picture.
2. **Otherwise the most confident detection that has a crop on disk.** A card
   with no picture is worse than one with a picture nobody chose, so a pet has
   a face from the moment its group is named rather than waiting to be curated.
   A detection with no crop is never chosen: `/pet-detections/<id>/crop` 404s
   without an `ai_pet_contents` row, and a broken image is worse than the paw.

**The star stores the FILE, not the detection** — `people.profile_photo_file_id`,
the column a person already uses. A detection id is not stable: a detection run
deletes and re-inserts a file's automatic boxes, so a stored detection id would
dangle afterwards and the card would silently lose its picture for no reason the
user could see. The detection is resolved from (file, pet) when the card is
drawn, so the choice follows the photograph through re-detection.

### 8.2 `PetDetail.js` — one animal

`/pets/:catalogId/:petId`, from
[PersonDetail.js](../../systems/frontend/src/views/PersonDetail.js): the
profile photo, the per-year photo strip from `people_photos`, and the detected
photographs for each year — reading `pet_detections` through §7.3 instead of
`face_detections`. Tags and category edit exactly as a person's do.

### 8.3 `IdentifyPets.js` — the groups

`/identify-pets`, from
[IdentifyGroups.js](../../systems/frontend/src/views/IdentifyGroups.js), which
is only 130 lines because the work is in the endpoint. Groups of unnamed
detections, biggest first, each showing a handful of crops, a cohesion slider
with the same scale as Identify groups, and two actions: **name it** (an
existing pet, or a new one in the same dialog) and **not an animal**.

*Now:* the slider sets the smallest group shown (*at least N photographs per
group*, 1–12); each group shows its cohesion as a number.

Naming a group is one request (§7.4) and the group leaves the list immediately.

### 8.4 Routing and menu

```jsx
<Route path="/pets" element={<Pets />} />
<Route path="/pets/:catalogId/:petId" element={<PetDetail />} />
<Route path="/identify-pets" element={<IdentifyPets />} />
```

Sidebar: **Pets** in the `Content` group after People — the owner's words were
"a new page in contents" — and **Identify pets** in the `Face recognition`
group beside Identify groups. That group's label becomes **Recognition**, since
it will then hold both faces and animals; its `defaultOpen` gains
`/identify-pets` and `Content`'s gains `/pets`.

Both entries are **hidden when pets are off for every accessible catalog.** An
empty page behind a menu item teaches people to ignore the menu.

*Now:* not built. Both entries show whenever a catalog is open; with pets off
the Pets page says so and points at the catalog settings.

### 8.5 What is reused verbatim

`UserAvatar`'s circular-crop treatment for pet crops, the card grid and modal
pattern from People, `CreatableSelect` for tags, the catalog selector and
`useCatalog`, and the cohesion slider. Anything new is listed in §8.1-8.3 and
nothing else should appear.

### 8.6 Before committing frontend work

`./tools/lint-frontend.sh` — a Babel compile and an eslint `no-undef` pass in
~14 s. It exists because a rename left two undefined identifiers and broke a
build that had been eyeballed. Bracket-balance checking does not catch that.

---

## 9. Search and albums

### 9.1 The album filter generator

[album_filters](../../systems/backend/models.py#L968) stores `nl_text` beside a
`sql_fragment`, and the fragment is **written by a model** from the schema
description in
[backend-albums/app.py:130-200](../../systems/backend-albums/app.py#L130). That
prompt is where pets become searchable; nothing else needs to change for
"photos with Rex" to work.

Add to the table list:

```
subject_appearances (alias: sa)   -- people AND pets appearing in files
  file_id      INTEGER   -- FK → file_contents.id
  person_id    INTEGER   -- FK → people.id  (a pet is a people row)
  kind         TEXT      -- 'person' or 'pet'
```

Add to QUERY PATTERNS, **replacing** the `face_detections` person pattern so the
model is not offered two ways to do one thing:

```
Person or pet in photo:
  fc.id IN (SELECT sa2.file_id FROM subject_appearances sa2
            JOIN people p2 ON p2.id = sa2.person_id
            WHERE p2.name = 'Izelpi')

Any pet in photo:
  fc.id IN (SELECT sa3.file_id FROM subject_appearances sa3
            WHERE sa3.kind = 'pet')
```

Keep the `face_detections` pattern **only** for queries that need a box or
`source = 'Manual'`, and say so in the prompt in those words. A model given two
equivalent paths will use both, inconsistently, across fragments nobody reads.

### 9.2 Existing albums

Unaffected. Their stored fragments still reference `face_detections`, which
still means what it meant. Nothing is rewritten, and an album built before pets
existed returns the same photographs after.

### 9.3 Now: Search, and albums made with tools

The §9.1 prompt change is in `backend-albums`. Since v6.1.9 the New album
dialog no longer offers albums built from words (new albums are made with
tools; those already made with words still open in their editor), so the main
way to find "photos with Rex" is **Search** (v6.0.0): its people-and-pets
filter and the names in each result read `pet_detections` alongside
`face_detections` (`search.py`).

---

## 10. Optionality

Nothing new is invented; two existing mechanisms carry it.

**Existence is the switch.** `workflows` has no `enabled` column
([models.py:129-138](../../systems/backend/models.py#L129)) — a workflow exists
for a catalog or it does not. So `"default": false` in `defaults.json`, and
`apply_pets_enabled(..., True)` creates the two rows from those templates. This
is precisely how storage routes create their import workflow
(`_create_import_workflow_for_route`, [app.py:1671](../../systems/backend/app.py#L1671)).

**`catalog_properties.pets_enabled`** ([models.py:172](../../systems/backend/models.py#L172),
helpers at [1266/1286](../../systems/backend/models.py#L1266)) gates the
detection ride-along (§5.3).

| State | What happens |
|---|---|
| **On (the default for a new catalog)** | Two workflows created, `pets_enabled=1`, schema ensured, detection begins on the next face pass, the chain runs in the normal queue with normal dependencies and normal stop semantics. |
| **Off** | No pet workflow rows. `Auto Detect Faces` unchanged in behaviour and runtime. No pet schema ensured by the workflow. `backend-pets` never started. |
| **Off again** | Workflow rows deleted; `sync_default_workflows()`'s `default_workflows_seen` watermark means deletions stick rather than being re-seeded next start. Data is kept — turning it off is not a request to delete the pets. |

Per catalog, not per deployment: homeserver holds `family` and `second-catalog` and
the right answer differs. An environment variable would have forced both.

**"Off" costs zero, not nearly zero**, and §13 has the test that says so.

### 10.1 Where the switch lives — changed 2026-10-09

It was a toggle on the Pets page with an endpoint of its own
(`PUT /catalogs/<id>/pets/settings`). It is now part of the catalog, on the
catalog form beside the face recognition metric, and **on by default**.

Three consequences, each deliberate:

- **One writer.** `apply_pets_enabled()` is the only thing that writes
  `pets_enabled`, called from `POST /catalogs` and `PUT /catalogs/<id>`. The
  old endpoint is gone rather than kept alongside: two endpoints that can set
  one property are two endpoints that can disagree about it, and there is a
  test asserting the second door stays shut.
- **On by default for a NEW catalog, and only a new one.**
  `pets_default_for_new_catalog()` holds the three rules — an explicit value
  wins, a re-attached catalog keeps its own recorded answer (the same rule
  `ingest_mode` follows, so re-registering a folder cannot silently flip a
  decision stored with the data), and otherwise on. A catalog that existed
  before the switch has no property, and **absence still means off**: reading
  it as "on" would have started the detector and added two workflows to every
  catalog already on disk.
- **The Pets page keeps its counts and loses its toggle.** The counts were
  always the better half of that card — a switch with no feedback is a switch
  nobody trusts — and when pets are off the page now points at the catalog
  settings instead of offering a control that lives elsewhere.

---

## 11. Migration and rollout

All schema changes are additive, and `ensure_pet_schema()` plus the two
`ALTER TABLE`s in §3.1 are idempotent. There is no data migration: existing
catalogs gain columns that default correctly and tables that start empty.

| Step | Where |
|---|---|
| 1 | Merge with pets **off everywhere**. Every existing deployment is unaffected by construction (§10). |
| 2 | Build `backend-pets` on a Mac. `./start-all.sh --build=pets`. |
| 3 | Run §12 on 500 photographs from a real catalog. **Gate.** |
| 4 | Enable on `second-catalog` first — 24,275 files (measured 2026-10-08), so the backlog is minutes, not hours. |
| 5 | Enable on `family` only after step 4 looks right. 373,020 files; start it in the evening. |

**oldserver's disk, before step 5.** `sda` is failing with uncorrectable sector
errors and `/media/storage` is RAID 0 across it, so a backlog run is hours of
reading a dying disk. STORAGE_MIGRATION.md
plans the replacement. **Do the SSD migration first, or accept that this run is
a stress test of a disk that is already failing.** This is not a reason to
delay the code; it is a reason not to press the button on `family` yet.

---

## 12. The gate — run 2026-10-09, and what it changed

> **RETRACTED IN PART, 2026-10-09 (later the same day). The three detection
> rates in the table below were measured through a channel-order bug and do not
> mean what they say.** `backend-pets` fed YOLOX **RGB**; YOLOX is trained on
> **BGR**, `cv2.imread` order, unnormalised. The boxes were unaffected — box
> regression reads shape — but the class head produced confident rubbish, and
> "images with an animal" was really "images where the model hallucinated one".
>
> The first run against real photographs found it: **seven detections in six
> hundred `family` photographs, every one a person.** A child's face as "dog"
> at 0.84. A sleeping boy as "cat". Same six photographs after swapping two
> channels: `person`, every time, at equal or higher confidence — and `person`
> is not a class this service wants, so all seven are correctly discarded.
>
> What this invalidates:
>
> - **The three detection rates.** 6.0 %, 5.4 % and 2.7 % are hallucination
>   rates, not hit rates.
> - **"`yolox_nano` misses half the animals."** It may simply have
>   hallucinated less. The nano-vs-tiny comparison has to be re-run.
> - **"The postprocess is right … a sensible dog/cat split."** The decode was
>   right and the split was not sensible; it was the split a model makes when
>   shown the wrong colours. The 0.88-0.96 confidences I read as a good sign
>   were the model being confidently wrong, which is the one thing a
>   confidence number cannot tell you.
>
> What survives: the latency figures (unaffected by colour), the fixed-shape
> finding, and the cost arithmetic. §12.1 below is the re-measurement.
>
> The methodological error is worth naming on its own, because it is not about
> colour. **I counted detections and never looked at one.** A benchmark that
> measures how often a detector fires, without checking what it fired at,
> cannot distinguish a working model from a broken one — and it reported the
> broken one as a success.

**Measured on homeserver**, 300-500 prepared JPEGs sampled at random from `family`'s
cache (the `_alt.jpg` derivatives, which are what the ride-along will actually
be handed), through `backend-pets`' own `detect()` so that the postprocess was
under test too.

| model | input | ms/image | images with an animal | verdict |
|---|---|---|---|---|
| `yolox_s` | 640 px | **711** (1 thread) | 5.4 % (27/500) | too slow |
| `yolox_tiny` | 416 px | **91** (4 threads) | **6.0 %** (18/300) | **chosen** |
| `yolox_nano` | 416 px | **41** (4 threads) | 2.7 % (8/300) | finds half |

### What passed

**The postprocess is right.** This was flagged in §4.2 as the part most likely
to be wrong against a real export, and it held: 0.88-0.96 confidences, a
sensible dog/cat split, and the both-shapes decode correctly identified which
output the real file produces. Spot-checkable — the benchmark prints its most
confident hits by path.

**The detection rate is plausible.** 5-6 % of a family catalog containing a dog
or a cat is the right order, and `yolox_tiny` and `yolox_s` agree on it, which
is the useful signal: two different models finding the same proportion is
evidence about the catalog rather than about either model.

### What failed, and what the spec had wrong

**`yolox_nano` was the recommendation and it should not have been.** §4.2 chose
it on parameter count — 0.9 M against tiny's 5 M. It is twice as fast and finds
**2.7 % where the other two find 5.4-6.0 %**, so it misses roughly half the
animals. A detector that does not see the dog does not save 50 ms; it costs the
feature. Parameter count is not a proxy for whether a model can see a spaniel.

**Input size is not a free parameter.** §4.2 said "320 px or 640 px, §12
decides". The official YOLOX ONNX exports are **fixed-shape**: asking `yolox_s`
for 320 returns `Got: 320 Expected: 640` from onnxruntime, not a faster answer.
`app.py` now reads the size out of the graph and overrides whatever was
configured, because discovering this as an InvalidArgument mid-request helps
nobody. A genuinely variable size would need a re-export with dynamic axes.

**711 ms, against the ~56 ms quoted from vendor figures.** An order of
magnitude, on a 2011-vintage CPU. Every performance number in this document
before today was somebody else's measurement on somebody else's hardware, which
is exactly why this section exists.

### The cost, with real numbers

`Auto Detect Faces` measured **5.35 h for 194,433 files** — ~99 ms/file, of
which ~2 ms is YuNet and ~97 ms is read, decode, resize and POST. Scaled to
`family`'s 373,020 files that baseline is **~10.3 h**.

Adding `yolox_tiny` at 91 ms, with the decode shared:

| | per file | family backlog |
|---|---|---|
| today | 99 ms | 10.3 h |
| with pets | ~190 ms | **~19.6 h** |

So the ride-along roughly **doubles** the face pass on the one-off backlog, and
thereafter sees only newly imported files — a few hundred at a time. That is a
long overnight, not a week, and it is affordable. With `yolox_s` it would have
been ~84 h, which is not.

**Caveat on the arithmetic:** 91 ms is one request at a time with four
onnxruntime threads. The face pass already runs its per-file work concurrently,
so the *effective* added cost inside that pipeline should be lower than 91 ms —
but that is a projection and §5 should report its real runtime on
`second-catalog` (28,053 files, so ~1.5 h) before `family` is attempted.

---

## 12.1 The re-measurement, after the BGR fix — 2026-10-09

Same day, same host, `yolox_tiny` at 416 px, this time **looking at every
crop**.

**2,000 `family` photographs → 35 detections.** Sorted by confidence they
separate almost cleanly:

| confidence | detections | what they are |
|---|---|---|
| 0.35 – 0.54 | 10 | **five children** (a child on a swing from above, a blurry toddler with a pink bow, a person crouching in snow, two more), and five real animals (two dogs lying down, a goat, an armadillo twice) |
| 0.57 – 0.91 | 25 | **every one a real animal.** No exceptions. |

So `PETS_CONF` moves from 0.35 to **0.55**. The cut costs five real animals and
removes five children, and the trade is not symmetric: a missed dog reappears
in the next photograph of it — this sample alone held 34 shots of one chocolate
Labrador and one goat herd — while one child on the Pets page is what makes
somebody stop trusting the feature.

Two things the crops showed that no counter would have:

- **All three `cat` detections in the sample were children.** There were no
  cats. The cat class is the less reliable of the two on this model.
- **`species` is a hint, not a fact.** Twenty-odd goats at a petting zoo came
  back as `dog`, because COCO has no goat. §3.2 already treats species as
  advisory and the person naming the animal decides; this confirms it has to
  stay that way rather than becoming a filter.

**Throughput, measured end to end through the ride-along** (not through
`detect()` in isolation): **169 ms/file** on files whose faces were already
done, so decode + detect with nothing shared. That is higher than the 91 ms
detect-only figure and in line with it once the ~97 ms decode is added back —
and inside a real run the decode is shared with the face pass, so the marginal
cost stays close to 91 ms.

### Still outstanding

- **Re-run the nano/tiny/s comparison** against ground truth. The choice of
  `yolox_tiny` currently rests on a benchmark that has been retracted; it is
  working well, but "working well" is not "better than the alternatives".

---

## 12.2 Recall, and why 0.55 was still wrong — 2026-10-09

§12.1 measured the threshold on the 35 crops the detector had **already
produced** at 0.35. That is a measurement of precision. It never looked at what
was being *missed*, which is the same error as §12's retracted benchmark
wearing different clothes: both validated what the detector found and neither
asked what it did not.

What found it: one photograph, offered as "a clear image of a dog" — a large,
sharp, well-lit white shepherd filling half the frame, on a white leather sofa.

```
cat 0.515   box (145, 251) 981 x 546
dog 0.450   box (150, 279) 969 x 521     <- 5 px apart, the same animal
```

At 0.55, **nothing**. Three frames of that dog minutes apart:

| frame | scores |
|---|---|
| standing, clear | `dog 0.924` |
| lying down, head turned | `cat 0.560`  `dog 0.352` |
| lying down, head turned | `cat 0.515`  `dog 0.450` |

Before changing anything, the channel order was re-checked against an oracle
rather than re-argued: 64 local photographs in which YuNet has already found a
face, so each certainly contains a person. BGR gives the higher `person` score
in **58 of 64**. §12's fix stands; this is a hard image, not another bug.

Three changes followed.

**`PETS_CONF` 0.55 → 0.45.** All eight animal photographs in the local catalog
are then found; the confident dogs at 0.59–0.92 are untouched. The price is
some of the children at 0.52–0.54 returning.

**Cross-class NMS.** Standard practice is per-class NMS, because a person in
front of a car is two objects. Cat and dog are not: one animal is not both, so
they must compete, and the highest confidence takes the box. Without it the
hedged frames above each produce *two* detections — two crops, two embeddings,
and two candidate animals for clustering to decide were different pets.

**The species gate in `phase_score` removed.** This is the one that would have
quietly undone the other two. With it, the two `cat`-labelled frames of a dog
named from the other six are never compared against it, so they stay
unidentified for ever — Rex found in six photographs and absent from two
near-identical ones, with nothing saying why.

The general rule, now measured rather than assumed: **the embedding is the
evidence and the species label is not.** It has a 25 % error rate on a single
animal, it is recorded beside the box as a hint (§3.2), and it may not gate
anything. Clustering already grouped on embeddings alone and only takes the
modal label; its "mixes species" warning has been reworded, because a mixed
group is now as likely to be one hedged animal as two wrongly joined.

### Still unmeasured

The **identity** half. `animal_clip_vit_b32.onnx` does not exist yet:
AvitoTech's model is published as PyTorch and needs a one-off ONNX export on a
machine with torch. Detection works without it; identification does not, and
its 224 px cost is unmeasured.

*Now:* the export is `tools/export_animal_clip.py`, and its output is what
`backend-pets` loads for identity (§4.2).

## 13. Testing

Mirroring where the existing suite's scars are.

**Schema** — `ensure_pet_schema()` is idempotent; running it twice changes
nothing; it creates on a database that has never seen pets; it upgrades one
created by an older version. The `location_schema` tests are the template.

**Invariants (§3.8)** — one test each, and they are the important ones:

- I1: after a full simulated run, `SELECT COUNT(*) FROM face_detections fd JOIN people p ON p.id = fd.person_id WHERE p.kind = 'pet'` is **0**.
- I2: no code path but an explicit name/assign call inserts into `people`.
- I5: with pets off, the detection pass opens no HTTP session, ensures no pet
  schema, and touches no pet table. Assert on the absence, not on the timing.

**Launch contract** — the test that was missing when `No module named 'lib'`
reached the field: each new script is launched the way the scheduler launches
it, **without** the test adding `workflows/` to `sys.path`. Removing the
`WORKFLOW_STOP_MODES` entry must make this test fail.

**Registration** — every script in `defaults.json` appears in all four other
maps, and vice versa. A table-driven test over the five places would have
caught the inert `TRIGGERS` omission, and would catch the next one.

**Coordinate rescaling** — a box detected at 1600 px crops the right region of
a 6000 px original, and of its RAW sibling's JPG. `documentation/fixes/` has
nine files about this class of bug; one test is cheaper than a tenth.

**Fixtures** — any test using `client` must also request `catalog`. Two separate
bugs in this repository were tests that passed alone and failed in the suite for
exactly that reason.

**Frontend** — `tools/lint-frontend.sh`, plus a render test per new page in the
style of `Videos.test.js` and `Alerts.test.js`.

---

## 14. Staged delivery

Each stage is useful alone, and each has an exit.

| Stage | Contents | Exit criterion |
|---|---|---|
| **0** | §12's benchmark. No production code. | A number, and a decision about input size, written into §12. |
| **1** | `pet_schema.py`, §3.1 columns, `backend-pets` with `/health` + `/detect`, the §5 ride-along behind the switch, `pet_detections` rows. No UI but a count. | `Auto Detect Faces`'s new runtime is acceptable on `second-catalog`. |
| **2** | `/embed`, `pe_identify_pets.py`, crops and embeddings, scoring, manual naming of a detection. Two pets named by hand. | Equal error rate on *these* animals is defensible. Published figures are for a 695,091-individual gallery; a 3-pet household is a far easier problem and the EER (~7 %) transfers better than the top-1 (65 %). |
| **3** | `pe_cluster_pets.py`, `IdentifyPets.js`, `Pets.js`, `PetDetail.js`, the §7 APIs, menu and routes. | A household can find its dog. |
| **4** | §3.6's view and §9's prompt change. | "Photos with Rex" returns photographs in an album filter. |

Stage 4 is small and should not be deferred: the view plus two prompt lines is
what makes the whole thing searchable, and it is the part the household actually
benefits from. Stage 3 without Stage 4 is a gallery nobody can query.

---

## 15. Open items

1. **Birds and horses.** The detector reports them free. Record as detections
   with `species` and no identity, or discard them? Recording costs rows and
   offers groups nobody will name. **Recommendation:** discard at the service
   boundary for now, keep the classes configurable, since the cost of changing
   one's mind later is a re-run.
2. **How many pets.** Sets the reference-set size, and with two animals the
   embedder has a very easy job. Does not change what is built.
3. **Splitting a group.** Two pets of the same breed will land in one group.
   `POST /clusters/<id>/split` is in §7.4 as optional; faces do not have it
   either. Defer until it is felt.
4. ~~**YOLO11's licence is AGPL-3.0**~~ — **resolved, see §4.2.1.** The
   detector is YOLOX (Apache 2.0). The first draft treated AGPL as a
   distribution-only concern and said it was "nothing" for a household; that
   understated it twice — AGPL §13 also reaches users who merely interact with
   the software over a network, and Shoebox is proprietary (`license.txt`), so
   the two licences cannot occupy one distributed work at all. What remains is
   to confirm at Stage 0 that a permissive detector finds the animals (§12).
5. **Auto-assign thresholds.** §6.1's defaults are guesses until Stage 2
   measures them. They should start so conservative that auto-assignment almost
   never fires, and be loosened against evidence.
