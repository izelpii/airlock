# Catalog Data Directory & Input Mount

All catalog data (databases, output files, workflow results) lives under a
single host directory that is mounted into the backend container at `/catalogs`.
Source files for import workflows are served from a second mount at `/input`.

## Current configuration

| Mount | Host path | Container path |
|---|---|---|
| Catalog data | `<repository>/catalogs/` (`CATALOGS_HOST_DIR`) | `/catalogs/` |
| Workflow input | `<repository>/IN/` (`INPUT_HOST_DIR`) | `/input/` |

Each catalog occupies one subdirectory:

```
<repository>/catalogs/
├── my-first-catalog/        ← one directory per catalog
│   ├── workflow_results.db
│   └── output/
└── another-catalog/
    ├── workflow_results.db
    └── output/
```

When creating a catalog in the UI, set **Target Directory** to
`/catalogs/<your-catalog-name>` (the container-side path).

## Porting to a new machine

1. Copy (or re-create) both directories (`catalogs/` and `in/`) on the new host.
2. Update the paths in these two files:

   | File | Variable / key | What it controls |
   |---|---|---|
   | `systems/backend/start-podman.sh` | `CATALOGS_HOST_DIR` | Catalog data root |
   | `systems/backend/start-podman.sh` | `INPUT_HOST_DIR` | Workflow input files |
   | `systems/backend/backend-pod.yaml` | `catalogs` volume `hostPath` | Same, for pod mode |
   | `systems/backend/backend-pod.yaml` | `input` volume `hostPath` | Same, for pod mode |

3. Rebuild and restart the backend — no other changes needed.

## Creating a new catalog

1. Create the subdirectory on the host first:
   ```bash
   mkdir <repository>/catalogs/<catalog-name>
   ```
2. In the UI, create a catalog with **Target Directory** set to:
   ```
   /catalogs/<catalog-name>
   ```

## Using the input directory in a workflow

Place the files you want to import inside the host `in/` directory:
```
<repository>/IN/
├── photo-dump-2024/
│   ├── IMG_0001.jpg
│   └── IMG_0002.heic
└── scans/
    └── scan001.tif
```

When creating a workflow in the UI, set **Source Directory** to `/input` or a
subdirectory such as `/input/photo-dump-2024`.  The `io_picture_import_workflow.py`
script will read from that path inside the container.
