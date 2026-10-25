# BorgBackup on the backup host

BorgBackup is the second backup type on the Backup tab (v5.10;
`documentation/specs/backup-backrest-spec.md` §11), after restic. Like restic, Airlock
runs borg itself, in the backend (borg 1.4.5 is in the backend image). Unlike restic over SFTP, borg needs **`borg` on the
backup host too**: the client connects over SSH and runs `borg serve` there, and
`borg serve` reads and writes the repository on that machine's disk.

So the backup host needs a borg binary and nothing else - no service, no container, no
open port beyond SSH.

## Install (as done on backup-host, 2026-10-18)

The official standalone binary, version pinned and verified. No root needed when
`/opt` and the repository disk belong to the SSH user.

```bash
mkdir -p /opt/borg/bin /opt/borg/downloads && cd /opt/borg/downloads
B=https://github.com/borgbackup/borg/releases/download/1.4.5
curl -fsSLO $B/borg-linux-glibc231-x86_64
curl -fsSLO $B/borg-linux-glibc231-x86_64.asc
echo "c8457f70660064d0f45b38283ab4cc65b342970012013201d3aec713a75898fb  borg-linux-glibc231-x86_64" \
  | sha256sum -c -
gpg --keyserver hkps://keyserver.ubuntu.com --recv-keys 6D5BEF9ADD2075805747B70F9F88FB52FAF7B393
gpg --verify borg-linux-glibc231-x86_64.asc borg-linux-glibc231-x86_64   # Thomas Waldmann
install -m 755 borg-linux-glibc231-x86_64 /opt/borg/bin/borg-1.4.5
ln -sfn borg-1.4.5 /opt/borg/bin/borg
mkdir -p /media/usb1/borg && chmod 700 /media/usb1/borg
```

- **1.4.5**, the current stable release. borg 2.0 is still in beta and its repositories are
  not compatible with 1.x; the client and server must be the same major version.
- `glibc231` runs on glibc 2.31 and later (backup-host: Ubuntu 22.04, glibc 2.35).
- **Upgrade:** add `bin/borg-<version>` beside it and move the symlink.

## Letting an Airlock in

One `authorized_keys` line per client key, limited to `borg serve` and to the
repositories folder:

```
command="/opt/borg/bin/borg serve --restrict-to-path /media/usb1/borg",restrict ssh-ed25519 AAAA… airlock-borg@mac
```

A key line has one forced command, so the borg key is **not** the key restic uses (that
one is pinned to `sftp-server`): each Airlock has a second key, for borg
(`database/backup-ssh/id_ed25519_borg`), whose line the BorgBackup card's form shows.
`--append-only` can be added later to stop a client from deleting archives; pruning
would then be done on backup-host itself.

The client addresses a repository as

```
ssh://izelpi@192.168.1.16/media/usb1/borg/<name>
```

with `BORG_REMOTE_PATH=/opt/borg/bin/borg`, since borg is not on backup-host's `PATH`.

## As set up (2026-10-18)

- backup-host (192.168.1.16): `/opt/borg/bin/borg` → `borg-1.4.5`; notes in `/opt/borg/README.md`.
- Repositories go in `/media/usb1/borg/` (empty; mode 700).
- Smoke-tested on backup-host: `init` (repokey-blake2), `create`, `list`, `check`; the test
  repository was removed.
- Clients: `airlock-borg@mac` (the development Mac's backend), restricted to
  `borg serve --restrict-to-path /media/usb1/borg` (2026-10-18). Each other Airlock adds
  its own line - the Backup tab shows it.
- The Airlock client (v5.10) was checked end to end from the Mac against backup-host; its test
  repository was removed afterwards. User guide: `docs/features/backups.md`.
