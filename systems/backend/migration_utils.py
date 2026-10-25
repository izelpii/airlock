#!/usr/bin/env python3
"""
Shared helpers for the migration scripts.

The reason this exists: catalog_meta.target_directory holds the path the
*backend* uses for a catalog, and since the containerization that is a path
inside the container - "/catalogs/family". That directory does not exist on the
host. A migration run from a host shell therefore looked in the wrong place,
printed "Database not found" for every catalog and exited 1, which reads almost
exactly like a migration that ran and correctly found nothing to do.

That failure is silent in the way that matters: the operator believes the
schema changed, the schema did not change, and the breakage surfaces much later
somewhere unrelated.
"""

import os


def repo_root():
    """The airlock checkout this file lives in."""
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))


def catalog_dir_candidates(catalog, catalogs_root=None):
    """
    The directories a catalog's files might live in on this machine, in order.

    First the stored target_directory, then the same basename under the
    repository's catalogs/ directory (or *catalogs_root* when given). Both
    resolvers below walk this list so there is one rule, not two.
    """
    target = getattr(catalog, 'target_directory', None) or ''
    candidates = [target] if target else []

    basename = os.path.basename(target.rstrip('/'))
    if basename:
        root = catalogs_root or os.path.join(repo_root(), 'catalogs')
        fallback = os.path.join(root, basename)
        if fallback != target:
            candidates.append(fallback)
    return candidates


def _note_substitution(target, used, quiet):
    if used != target and not quiet:
        print(f'  (target_directory {target} is a container path; '
              f'using {used})')


def resolve_catalog_db(catalog, filename='workflow_results.db', quiet=False):
    """
    Find a catalog's database on THIS machine.

    Tries catalog.target_directory first, then the repository's own catalogs/
    directory under the same basename. Returns the original path when neither
    exists, so the caller's "not found" message names what it actually looked
    for first.
    """
    target = getattr(catalog, 'target_directory', None) or ''
    direct = os.path.join(target, filename)

    for directory in catalog_dir_candidates(catalog):
        path = os.path.join(directory, filename)
        if os.path.exists(path):
            _note_substitution(direct, path, quiet)
            return path

    return direct


def resolve_catalog_dir(catalog, catalogs_root=None, quiet=False):
    """
    Find the directory holding a catalog's files on THIS machine, or None.

    The directory-level counterpart of resolve_catalog_db, for migrations that
    write derived files next to the originals and therefore need the directory
    itself rather than one database path inside it.
    """
    target = getattr(catalog, 'target_directory', None) or ''

    for directory in catalog_dir_candidates(catalog, catalogs_root):
        if os.path.isdir(directory):
            _note_substitution(target, directory, quiet)
            return directory

    return None
