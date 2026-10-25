#!/usr/bin/env python3
"""
migrate_add_default_workflows.py
================================
Add the default workflows to catalogs created before they were seeded
automatically.

Implements documentation/specs/workflows-default-spec.md section 6.

A catalog with no workflows has no automation at all: workflow_triggers resolves
a chain by matching script basenames against what is registered for that catalog
and silently skips the rest, so an import completes and nothing downstream runs,
with no error anywhere.

Idempotent, keyed on python_script: only missing workflows are added, so this is
safe to run repeatedly and against catalogs that already have some of them.

Ingestion workflows are created with an EMPTY source directory and cannot run
until one is set — that is deliberate (spec section 4.1); a wrong path is worse
than an empty one.

Usage
-----
    python backend/migrate_add_default_workflows.py --dry-run
    python backend/migrate_add_default_workflows.py
    python backend/migrate_add_default_workflows.py --catalog-id 55
    python backend/migrate_add_default_workflows.py --catalogs-root /mnt/catalogs
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

from catalog import Catalog  # noqa: E402
from migration_utils import resolve_catalog_dir  # noqa: E402
from models import CatalogDatabase, WorkflowModel  # noqa: E402
import workflow_defaults  # noqa: E402


def migrate_catalog(catalog, dry_run=False, catalogs_root=None):
    summary = {'created': 0, 'skipped': 0, 'unreachable': 0}

    if not catalog.target_directory:
        print(f"  [skip] Catalog '{catalog.name}' has no target_directory.")
        summary['unreachable'] = 1
        return summary

    # Do not re-implement the container-path fallback: target_directory holds
    # the path the backend container sees, which does not resolve on the host.
    target_dir = resolve_catalog_dir(catalog, catalogs_root)
    if not target_dir:
        print(f"  [skip] {catalog.target_directory} not reachable from here.")
        summary['unreachable'] = 1
        return summary

    catalog_db = CatalogDatabase(target_dir)
    existing = {os.path.basename(w.get('python_script') or '')
                for w in WorkflowModel(catalog_db).get_all()}
    entries = workflow_defaults.default_entries()

    if dry_run:
        for entry in entries:
            if entry['script'] in existing:
                print(f"    [have] {entry['name']}")
                summary['skipped'] += 1
            else:
                note = ' (needs a source directory before it can run)' \
                       if entry['ingestion'] else ''
                print(f"    [dry-run] would create {entry['name']}{note}")
                summary['created'] += 1
        return summary

    result = workflow_defaults.seed_default_workflows(catalog_db)
    for name in result['created']:
        print(f"    [created] {name}")
    for name in result['skipped']:
        print(f"    [have] {name}")
    summary['created'] = len(result['created'])
    summary['skipped'] = len(result['skipped'])
    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Seed default workflows into existing catalogs.')
    parser.add_argument('--catalog-id', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--catalogs-root',
                        help='Directory holding catalog folders on this machine, when the '
                             'stored target_directory is a container path.')
    args = parser.parse_args()

    if not workflow_defaults.load_manifest():
        print('No default workflows defined. Check '
              'systems/backend/workflows/defaults.json.', file=sys.stderr)
        return 1

    if args.catalog_id:
        catalog = Catalog.get_by_id(args.catalog_id)
        if not catalog:
            print(f'Error: catalog {args.catalog_id} not found.', file=sys.stderr)
            return 1
        catalogs = [catalog]
    else:
        catalogs = Catalog.get_all()

    total = {'created': 0, 'skipped': 0, 'unreachable': 0}
    for cat in catalogs:
        print(f"\nCatalog '{cat.name}' (id={cat.id}), target={cat.target_directory}")
        result = migrate_catalog(cat, dry_run=args.dry_run,
                                 catalogs_root=args.catalogs_root)
        for k in total:
            total[k] += result.get(k, 0)

    verb = 'Would create' if args.dry_run else 'Created'
    print(f"\n{verb}: {total['created']} workflow(s).")
    if total['skipped']:
        print(f"  Already present: {total['skipped']}")
    if total['unreachable']:
        print(f"  Catalogs not reachable from here: {total['unreachable']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
