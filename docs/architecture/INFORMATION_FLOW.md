# Information Flow

> **Updated for v5.6.8 (2026-10-12).** The previous version (v4.0) showed the
> browser calling `localhost` ports and workflows running as threads inside the
> backend. Every request now goes through the gateway, sign-in comes first,
> workflows run as queued subprocesses, and the face pipeline is six stages
> instead of two. Ports are shown for orientation only: nothing in the
> application uses them, only the gateway's names.

---

## 1. A request from the browser

```
Browser ── https://airlock.lan/... ──► airlock-gateway (nginx :443, private CA)
    │                                      │
    │   /                                   ├──► frontend :3000   (static React build)
    │   /api/..., /socket.io/...            ├──► backend  :5000
    │   /vision/..., /geoloc/...            ├──► vision :5003, geoloc :5002
    │   /gallery/..., <catalog>.airlock.lan ├──► album-gallery :3001
    │   http://shoebox.lan/...              └──► 301 to the same path on airlock.lan
    ▼
backend: auth check on every /api/ route except login, session and setup
    │   cookie airlock_session → sessions row in catalogs.db → user + per-catalog role
    │   no session → 401, and the UI shows the sign-in page
    ▼
route → CatalogDatabase(catalog) → workflow_results.db → JSON
```

Containers reach each other the same way, by name: `http://deepface.airlock.lan`,
`http://pets.airlock.lan`, `http://ollama.airlock.lan` (Ollama on the host).
The exception is transcode, which the backend calls directly at
`http://transcode.airlock.lan:5004`.

---

## 2. Running a workflow

```
User clicks Run (Operations page), or a finished workflow triggers its dependants
    │
    ▼
POST /api/catalogs/<id>/queue  →  workflow_queue row in workflow_results.db
    │
    ▼
workflow_scheduler (thread in the backend, holds scheduler_lease in catalogs.db)
    │  at most 2 running, never two whose write sets overlap
    ▼
subprocess: python -u workflows/<script>.py --execution-id N --target-dir … --catalog-id …
    │  writes workflow_executions + its own tables
    │  progress → backend → Socket.IO 'workflow_progress' → Operations / Import Progress pages
    ▼
on success: DEPENDS_ON (workflow_triggers.py) queues the next workflow in the chain
```

A stop request is cooperative: the script checks for it between items and
records itself as stopped. Restarting the backend container interrupts any
running subprocess.

---

## 3. Importing files (Import Files, `io_picture_import_v2_workflow.py`)

```
Storage route (a folder under STORAGE_ROOT_HOST, mounted at /storage) or IN/ (/input)
    │
    ▼
scan     walk and stat the source
probe    one sequential read per file: SHA-256 + copy into the catalog together;
         exiftool / PIL metadata read straight after, while it is in the page cache
         (a file whose size matches nothing in the catalog cannot be a duplicate)
resolve  duplicate decision per file; master_datetime decided here and never missing
place    commit file_contents, exif_data (+ exif_raw), geohash — photos appear
         while the import runs
         move mode: rename into the catalog where possible, else copy → verify → delete
         the source only after the commit; duplicates → <source>/DUPS/
render   in the background: every image rung from one decode (RAW from its embedded
         preview when large enough); videos → backend-transcode /transcode, /poster
         → alternative_versions, files under cache/Images and cache/Videos
    │
    ▼
Live Photo halves and RAW+JPEG pairs are grouped into one item.
Progress is reported per phase (import_phases) to the Import Progress page.
Storage alerts stop an import before the disk fills.
```

---

## 4. Faces

```
Find Faces            ai_auto_detect_faces_workflow.py
  │  per unscanned image: one prepared JPEG
  │  → deepface /api/upload (YuNet) → face_detections (source=Automatic) + crops (ai_face_contents)
  │  → pets /detect on the same JPEG, if the catalog has pets on
  ▼
Measure Faces         ai_embed_faces.py
  │  crops → deepface /api/embed → embedding + embedding_model_id
  ▼
Learn Tagged Faces    ai_ingest_manual_faces.py
  │  faces people have named or drawn → reference embeddings (face_contents)
  ▼
Match Faces by Appearance   ai_face_recognition_workflow.py
  │  chunk of faces × every reference as one matrix product (NumPy, in the workflow)
  │  → face_similarity_scores; people already named in a photo are not candidates
  │    for its other faces; embeddings from different models are never compared
  ▼
Re-rank Matches by Context  ai_contextual_recognition_workflow.py
  │  fuses appearance distance with events, time, who appears with whom,
  │  person priors and age plausibility (no deepface calls); clusters
  │  unidentified faces for Identify groups (skipped above 60,000 faces)
  ▼
face_best_match (kept current by triggers on every score, name or delete)
  │
  ▼
Identify people / Identify groups pages: confirm or reject in bulk
  → face_detections.person_id
```

The embedding model (VGG-Face or SFace) is set per catalog
(`catalog_properties.embedding_model`); `tools/migrate_embedding_model.py`
switches one, keeping every name people have given.

---

## 5. Pets (when enabled for the catalog)

```
Find Faces → pets /detect (YOLOX-Tiny) → pet_detections + crops
Measure and Match Pets  pe_identify_pets.py → pets /embed (animal CLIP) → pet_similarity_scores
Group Unnamed Pets      pe_cluster_pets.py → pet_clusters (trimmed to their core, rest regrouped)
Pets / Identify pets pages → a pet is a people row with kind = pet
```

Pets never enter the human reference set: faces live in `face_detections`,
pets in `pet_detections`, and they meet only in the `subject_appearances` view.

---

## 6. Events and locations

```
Create Events      ev_extract_events.py
  group files by master_datetime gaps → events, event_files

Extract Locations  ge_extract_locations.py
  cluster coordinates by geohash prefix → proposed locations + location_clusters
  (unconfirmed until a person confirms or rejects them)

Resolve Locations  ge_resolve_locations.py
  → file_locations: each photo's place, and where its coordinates came from;
    photos with no GPS inherit their event's location
  → place names from backend-geoloc GET /poi (Nominatim, OSMnx; cached)
```

The user's home location is never used to invent a photo's location (removed
in v4.13).

---

## 7. Describing a photo

```
Photos or Event page → "Describe" → https://airlock.lan/vision/describe
  → backend-vision → Ollama (OLLAMA_MODEL) → description shown on the page
```

## 8. Albums, deployments and transfers

```
Album prompt (natural language) → backend → albums /translate → Ollama
  → SQL WHERE clause → the album's photos (albums tables in workflow_results.db)

Publish → backend writes the album folder cache/Albums/<id>/ (derivatives,
          manifests), then cache/Deployments/<slug>/ (symlinks into it,
          optional .auth.json password)
  → album-gallery serves https://<catalog>.airlock.lan/<slug>

Pack     tools/pack_deployment.py → tarball with real files, compose.yml, Caddyfile
Transfer magic-wormhole between two installations → cache/Transfers → imported album
```

---

## 9. Where data lives

```
systems/backend/database/catalogs.db     catalogs, storage routes, users, sessions,
                                         access, alerts, user settings, scheduler lease
catalogs/<catalog>/workflow_results.db   everything about the catalog's files
catalogs/<catalog>/Images|Videos|Others  originals (not in reference mode)
catalogs/<catalog>/cache/…               derivatives, face crops, albums, deployments
systems/backend-deepface/face_labels.db  deepface's own label index
```

See [ARCHITECTURE.md](ARCHITECTURE.md#data-architecture) for the tables.
