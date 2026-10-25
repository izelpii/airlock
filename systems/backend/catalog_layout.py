"""
Every directory a catalog uses, created when the catalog is.

So the tree exists before any file does, and each part can be put on the
storage that suits it - for example the catalog (database and cache) on an
SSD with Images/ and Videos/ bind-mounted from spinning disks:

    /mnt/hdd/airlock/family/Images  /mnt/ssd/catalogs/family/Images  none  bind  0 0

A mount point has to exist before it can be mounted on. Before this, most of
these directories appeared only when the first file needed them.

Also a command, for a catalog created before this existed:

    python catalog_layout.py /path/to/catalog [--reference]

Deliberately NOT run for every catalog at startup: if a catalog's drive is not
mounted, creating its directories would make an empty tree on the disk below
the mount point, where the next import would then quietly write.
"""

import os
import sys

# Originals, filed by type: catalog.Catalog._get_file_type's categories, the
# first level of every calculateDir path. Not used by a catalog that indexes
# files in place (ingest_mode 'reference'), whose originals stay in its library.
ORIGINALS = ('Images', 'Videos', 'Others')

# Everything else, relative to the catalog directory.
DERIVED = (
    'cache/Images',       # display sizes and thumbnails (lib/utilities.py, render_once.py)
    'cache/Videos',       # video posters and playable proxies
    'cache/Albums',       # published album folders (app.py _deploy_album)
    'cache/Deployments',  # published galleries (albums-enhancement-spec.md)
    'cache/Transfers',    # album transfers being sent (wormhole_transfer.py)
    'cache/faces',        # face crops (ai_auto_detect_faces_workflow.py)
    'cache/avatars',      # people's profile photos
    'transfers',          # album transfers received (wormhole_transfer.transfers_root)
    'tmp/faces',          # manually tagged faces (ai_ingest_manual_faces.py)
)


def catalog_dirs(ingest_mode='copy'):
    """The relative directories a catalog in this mode uses."""
    return (() if ingest_mode == 'reference' else ORIGINALS) + DERIVED


def ensure_catalog_dirs(target_directory, ingest_mode='copy'):
    """
    Create the catalog's directory tree. Idempotent; never touches what exists.
    Returns the relative paths it created.
    """
    created = []
    for rel in catalog_dirs(ingest_mode):
        path = os.path.join(target_directory, rel)
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            created.append(rel)
    return created


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if len(args) != 1:
        print('usage: python catalog_layout.py /path/to/catalog [--reference]')
        sys.exit(2)
    target = os.path.abspath(args[0])
    if not os.path.isfile(os.path.join(target, 'workflow_results.db')):
        print(f'{target} is not a catalog (no workflow_results.db).', file=sys.stderr)
        sys.exit(1)
    made = ensure_catalog_dirs(target, 'reference' if '--reference' in sys.argv else 'copy')
    print('\n'.join(f'created {m}' for m in made) or 'nothing to create')
