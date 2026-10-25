"""
Golden comparison for the picture import (documentation/features/parallelism.md
section 10.1).

Runs an import into a scratch catalogue and dumps what it wrote in a form that
two runs can be diffed on: ids, timestamps and anything else that legitimately
differs between runs are dropped, and every table is sorted by content.

    python import_golden.py run  --source DIR --target DIR [--reference]
                                 [--workflow PATH] [--pass-files]
    python import_golden.py dump --target DIR > dump.txt

--workflow runs a different copy of the workflow module (e.g. the pre-change
version saved elsewhere) against the same harness. --pass-files hands execute()
the walked file list, which the pre-change version requires.
"""

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))


def _load_workflow(path):
    spec = importlib.util.spec_from_file_location('import_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_run(args):
    from models import CatalogDatabase, ExecutionModel
    from catalog import Catalog

    source = os.path.abspath(args.source)
    target = os.path.abspath(args.target)
    os.makedirs(target, exist_ok=True)
    db = CatalogDatabase(target)
    execution_id = ExecutionModel(db).create(1, 'Import Pictures (golden)')

    catalog = Catalog(
        name='golden', target_directory=target,
        ingest_mode='reference' if args.reference else 'copy',
        source_root=source if args.reference else None,
        image_rungs=args.rungs.split(',') if args.rungs else None)

    workflow = _load_workflow(args.workflow or os.path.join(
        HERE, 'workflows', 'io_picture_import_workflow.py'))

    files = None
    if args.pass_files:
        files = [os.path.join(r, f) for r, _, fs in os.walk(source) for f in fs]

    started = time.monotonic()
    result = workflow.execute(
        source_dir=source, target_dir=target, files=files,
        progress_callback=None, catalog=catalog, execution_id=execution_id)
    print(json.dumps({'result': result,
                      'seconds': round(time.monotonic() - started, 2)}),
          file=sys.stderr)


def _rows(conn, sql):
    return sorted(json.dumps(list(r), default=str) for r in conn.execute(sql))


def cmd_dump(args):
    conn = sqlite3.connect(os.path.join(args.target, 'workflow_results.db'))
    out = {}
    out['file_contents'] = _rows(conn, '''
        SELECT file_path, file_name, file_size, file_type, file_permissions,
               sha256_hash, modification_time, master_datetime,
               master_datetime_source, duration_seconds,
               image_group_id IS NOT NULL
          FROM file_contents''')
    out['exif_data'] = _rows(conn, '''
        SELECT fc.file_name, e.creation_date, e.latitude, e.longitude, e.altitude,
               e.camera_make, e.camera_model, e.lens_model, e.focal_length,
               e.aperture, e.iso, e.shutter_speed, e.flash, e.orientation,
               e.width, e.height, e.geohash,
               length(COALESCE(er.raw_exif_json, e.raw_exif_json))
          FROM exif_data e JOIN file_contents fc ON fc.id = e.file_id
          LEFT JOIN exif_raw er ON er.exif_id = e.id
         WHERE fc.exif_data_id = e.id''')
    out['alternative_versions'] = _rows(conn, '''
        SELECT fc.file_name, a.alternative_file_path, a.alternative_file_name,
               a.characteristic, a.width, a.height, a.rung
          FROM alternative_versions a
          JOIN file_contents fc ON fc.id = a.original_file_id''')
    out['image_groups'] = _rows(conn, '''
        SELECT p.file_name, g.kind, g.match_rule, g.match_evidence,
               (SELECT group_concat(file_name) FROM
                  (SELECT file_name FROM file_contents
                    WHERE image_group_id = g.id ORDER BY file_name))
          FROM image_groups g JOIN file_contents p ON p.id = g.primary_file_id''')
    # file_id is compared as "points at a row with this name", not as an id.
    out['file_operations'] = _rows(conn, '''
        SELECT fo.operation_type, fo.source_path, fo.target_path, fo.file_size,
               fo.status, fo.details, fc.file_name
          FROM file_operations fo LEFT JOIN file_contents fc ON fc.id = fo.file_id''')
    # target_path is absolute; two runs into different targets must still diff.
    target = os.path.abspath(args.target)
    for table, rows in out.items():
        print(f'## {table} ({len(rows)})')
        for r in sorted(x.replace(target, '<TARGET>') for x in rows):
            print(r)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--source', required=True)
    r.add_argument('--target', required=True)
    r.add_argument('--reference', action='store_true')
    r.add_argument('--workflow')
    r.add_argument('--pass-files', action='store_true')
    r.add_argument('--rungs', help='comma-separated optional rungs, e.g. 2048')
    d = sub.add_parser('dump')
    d.add_argument('--target', required=True)
    a = p.parse_args()
    cmd_run(a) if a.cmd == 'run' else cmd_dump(a)


if __name__ == '__main__':
    main()
