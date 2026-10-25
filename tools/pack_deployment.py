#!/usr/bin/env python3
"""
Pack a published deployment into a tarball that runs anywhere.

    tools/pack_deployment.py --catalog family --deployment album2026 \
        --domain photos.example.com --out ./dist

Produces  dist/<slug>-<YYYYMMDD>.tar.gz  containing the catalog subtree the
gallery already expects, plus a compose file, a Caddyfile and a README whose
commands are the ones to actually run. See
documentation/specs/albums-enhancement-spec.md section 4.5.

With --stage DIR it writes the bundle as the folder DIR/<deployment> instead,
media hard-linked from the catalog, and refreshes it on later runs - only what
changed is touched, so rsync sends only that
(documentation/guides/PUBLISH_TO_EC2.md).

The work is systems/backend/deployment_pack.py, shared with the Publish to
server button (documentation/specs/deployment-publish-to-server-spec.md). This
file is its command line: arguments, what is printed, and the tarball. It runs
with the host's own python3; deployment_pack uses the standard library only.
"""

import argparse
import contextlib
import os
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, 'systems', 'backend'))

import deployment_pack as dp                       # noqa: E402
from deployment_pack import (                      # noqa: E402,F401  (re-exported)
    PackError, copy_dereferenced, manifest_shipping_list, prune_manifests,
    section_photos, human, DEFAULT_IMAGE,
)

DEFAULT_CATALOGS_ROOT = os.environ.get(
    'CATALOGS_HOST_DIR', os.path.join(REPO, 'catalogs'))
DEFAULT_SPA_DIST = os.path.join(REPO, 'systems', 'album-gallery', 'app', 'dist')


def build(args):
    info = dp.inspect(args.catalogs_root, args.catalog, args.deployment)
    print(f'  deployment : {info["name"]}  ({args.catalog}/{args.deployment})')
    print(f'  albums     : {len(info["folders"])}  {", ".join(info["folders"])}')
    print(f'  sizes      : {", ".join(sorted(info["allowed_rungs"]))}')
    print(f'  password   : {"yes" if info["locked"] else "no"}')
    print(f'  published  : {info["published"]}')
    dp.check_age(info, args.max_age)

    if info['locked'] and args.target == 'static':
        if not args.allow_unlocked_static:
            raise PackError(
                'this deployment is password-protected and the static target '
                'cannot carry a password.\n'
                '  A file host has nothing to verify one with, and the hash '
                'must never be shipped to it.\n'
                '  Use --target node, or pass --allow-unlocked-static to pack '
                'it WITHOUT protection.')
        print('\n  !! PACKING UNLOCKED: the static target cannot check a password.')
        print('     Everyone with the URL will see every photo in this album.\n')

    staging = bool(args.stage)
    if staging and args.target != 'node':
        raise PackError('--stage is for the node target, the one synced to a server.')
    os.makedirs(args.out, exist_ok=True)
    today = time.strftime('%Y%m%d')
    tar_path = os.path.join(os.path.abspath(args.out),
                            f'{args.deployment}-{today}.tar.gz')

    # --stage builds the bundle tree in a directory that stays, with media
    # hard-linked from the catalog, and writes no tarball: the tree is what
    # rsync sends, file by file, so only what changed crosses the network and
    # nothing is compressed, stored twice or unpacked. Refreshed, not rebuilt:
    # an unchanged file keeps its time, so rsync does not even look at it.
    if staging:
        stage = os.path.join(os.path.abspath(args.stage), args.deployment)
        counts = dp.refresh_stage(info, stage, args.image, args.domain)
        _report(info, counts)
        print(f'  staged      : {stage}  (media '
              f'{"hard-linked" if counts["linked"] else "copied"}: no tarball)')
        print(f'                {counts["placed"]} new or changed, {counts["kept"]} '
              f'unchanged, {counts["removed"]} removed')
        print('\nNext (dry run first, then without -n):')
        print('  rsync -a -n --itemize-changes --delete --exclude=/.env \\')
        print(f'        {stage}/ <user>@<server>:galleries/{args.deployment}/')
        print('  The first sync after a tarball deploy needs --checksum once: '
              'the tarball\'s files carry its packing time, not their own.')
        return 0

    with tempfile.TemporaryDirectory(prefix='airlock-pack-') as tmp:
        stage = os.path.join(tmp, args.deployment)
        os.makedirs(stage)
        if args.target == 'node':
            counts = dp.build_node(info, stage, args.image, args.domain)
        else:
            counts = dp.build_static(info, stage, args.domain, args.spa_dist)
        _report(info, counts)

        # Sorted and with a fixed mtime so two packs of the same album produce
        # comparable archives.
        def norm(ti):
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ''
            if os.path.basename(ti.name) == '.auth.json':
                ti.mode = 0o600
            return ti

        with tarfile.open(tar_path, 'w:gz') as tar:
            tar.add(stage, arcname=args.deployment, filter=norm, recursive=True)

    packed = os.path.getsize(tar_path)
    base = os.path.basename(tar_path)
    print(f'  tarball     : {tar_path}  ({human(packed)})')

    # The next steps differ entirely by target: one runs a stack, the other
    # copies files to a bucket. Printing the compose instructions for a static
    # bundle would be advice that cannot work.
    print('\nNext:')
    if args.target == 'node':
        print(f'  scp {base} <vm>:~/')
        print('  ssh <vm>')
        print(f'  tar xzf {base} && cd {args.deployment}')
        print('  echo "ALBUM_SESSION_SECRET=$(openssl rand -hex 32)" > .env')
        print('  docker compose up -d')
        if args.image == DEFAULT_IMAGE:
            print(f'\n  NOTE: compose.yml points at {DEFAULT_IMAGE}.')
            print('        Pass --image with a registry you can actually pull from.')
        if args.domain == dp.PLACEHOLDER_DOMAIN:
            print('\n  NOTE: no --domain was given; edit the Caddyfile before starting.')
    else:
        print(f'  tar xzf {base} && cd {args.deployment}')
        print('  aws s3 sync . s3://YOUR-BUCKET/ --delete')
        print('\n  NOTE: a static bundle has no password and no server.')
    return 0


def _report(info, counts):
    for w in counts['warnings']:
        print(f'  ! {w}')
    if counts['skipped']:
        # Silence here would make a filtered bundle indistinguishable from a bug.
        print(f'  not packed  : {counts["skipped"]} files, {human(counts["skipped_bytes"])} '
              f'(sizes this deployment does not ship)')
    print(f'\n  dereferenced: {counts["files"]} files, {human(counts["bytes"])}'
          f'   (the link farm on disk is only '
          f'{human(dp.dir_size(info["dep_dir"], follow=False))})')


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Pack a published album deployment into a portable tarball.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='See documentation/specs/albums-enhancement-spec.md section 4.5.')
    ap.add_argument('--catalog', required=True, help='catalog slug, e.g. family')
    ap.add_argument('--deployment', required=True, help='deployment slug')
    ap.add_argument('--target', choices=('node', 'static'), default='node',
                    help='node ships compose + Caddy and keeps the password; '
                         'static ships plain files and cannot (default: node)')
    ap.add_argument('--domain', default=dp.PLACEHOLDER_DOMAIN,
                    help='the DNS name this will be served from')
    ap.add_argument('--image', default=DEFAULT_IMAGE,
                    help=f'gallery image to pull (default: {DEFAULT_IMAGE})')
    ap.add_argument('--out', default='./dist', help='where to write the tarball')
    ap.add_argument('--catalogs-root', default=DEFAULT_CATALOGS_ROOT,
                    help=f'default: {DEFAULT_CATALOGS_ROOT}')
    ap.add_argument('--spa-dist', default=DEFAULT_SPA_DIST,
                    help='gallery build, for --target static')
    ap.add_argument('--stage', metavar='DIR',
                    help='build the bundle as a directory DIR/<deployment> (media '
                         'hard-linked from the catalog) and write no tarball - to '
                         'rsync it to a server file by file; a later run refreshes it')
    ap.add_argument('--max-age', type=int, metavar='MINUTES',
                    help='fail if the deployment was published longer ago than '
                         'this - the packer cannot publish, so this is how a '
                         'stale bundle becomes an error instead of a surprise')
    ap.add_argument('--allow-unlocked-static', action='store_true',
                    help='pack a password-protected deployment for a static '
                         'host WITHOUT its password')
    args = ap.parse_args(argv)

    print(f'\nPacking "{args.deployment}" for the {args.target} target\n')
    try:
        return build(args)
    except PackError as exc:
        print(f'\nerror: {exc}\n', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
