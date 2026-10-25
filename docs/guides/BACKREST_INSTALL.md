# Backrest on the backup host

Airlock Photos backs catalogs up with **restic, which it runs itself**, in the backend
(`documentation/specs/backup-backrest-spec.md`, §3 and §16). Nothing about Backrest is
needed for a backup to run.

**Backrest** (restic's web UI and scheduler) is **its own software**, installed only on
the machine that holds the repository - backup-host. It is not one of Airlock's services and
does not ship with it (Backrest is GPL-3.0). It gives that machine:

- a view of the repository: snapshots, files, sizes - from any catalog that backs up there;
- **restore without Airlock** - if the catalog's machine is gone, this is how the files
  come back;
- the **maintenance** Airlock leaves to it: `prune` (reclaims the space of snapshots
  Airlock's retention forgot) and `check`.

Airlock runs `backup` and `forget` (after a plain `restic unlock`, which removes only stale
locks, such as one left by a backup cut off by a restart), and `check` when you press
**Verify**; Backrest runs `prune` and the scheduled `check`. restic's locks keep them
apart - a backup that finds the repository locked by a prune waits up to 30 minutes.

## 1. Run it

As backup-host runs it (Docker; podman takes the same arguments):

```bash
sudo mkdir -p /opt/backrest/{config,data,cache,tmp}
docker run -d --name backrest --restart=unless-stopped -p 9898:9898 \
  -e TZ=America/Los_Angeles \
  -e BACKREST_CONFIG=/config/config.json -e BACKREST_DATA=/data \
  -e XDG_CACHE_HOME=/cache -e TMPDIR=/tmp \
  -v /opt/backrest/config:/config -v /opt/backrest/data:/data \
  -v /opt/backrest/cache:/cache -v /opt/backrest/tmp:/tmp \
  -v /media/usb1:/media/usb1 \
  docker.io/garethgeorge/backrest:v1.14.1
```

- `/media/usb1` is the disk with the repository, mounted at the **same path** inside, so
  the repository is a plain local folder to Backrest - no SSH involved.
- Pin a version rather than `:latest`; backup-host runs 1.14.1 (restic 0.19.1, the same restic
  Airlock's backend carries).

Open `http://<host>:9898` once: it asks for an **instance name** and a **user and
password**. Keep authentication on - Backrest can delete repositories.

## 2. Add the repository

In Backrest: **Add repository**.

| Field | Value |
|---|---|
| Name | `usb1-backup` (any) |
| Repository URI | `/media/usb1/backup` - the local path, not `sftp:` |
| Password | the repository password - on homeserver in `/opt/backrest/secrets/credentials` (`repository_password`); the same one every Airlock card for this repository has |
| Prune policy | weekly, e.g. Sunday 10:00 - away from Airlock's 02:00 backups; max unused 10% |
| Check policy | monthly; read-data subset 0-5% |

Add **no plan**: Backrest backs nothing up here. Airlock's snapshots appear under the
repository, tagged `airlock`, `catalog:<name>`, `card:<id>`, with host
`airlock-<installation id>`.

## 3. Let an Airlock in

Each Airlock installation has its own SSH key. Its **Backup tab** shows the line to add,
when a card's repository is `sftp:`:

```
restrict,command="/usr/lib/openssh/sftp-server" ssh-ed25519 AAAA… airlock-backups
```

Append it to `~/.ssh/authorized_keys` of the user the repository URI names
(`sftp:izelpi@192.168.1.16:/media/usb1/backup` → `izelpi` on backup-host). `restrict` and
`sftp-server` limit the key to file transfer: no shell, no forwarding. Add a word to the
end (`airlock-backups@homeserver`) to tell installations apart; delete the line to cut one
off.

The first **Test connection** pins backup-host's host key in Airlock
(`database/backup-ssh/known_hosts`). If backup-host is reinstalled, its key changes and every
run fails with *"The backup host's SSH key is not the one Airlock first saw"* - remove the
host's line from that file and test again.

## As set up (2026-10-16)

- **backup-host** (192.168.1.16): Backrest in Docker, authentication on. Repository folder
  `/media/usb1/backup`, initialised (by homeserver's Backrest, before the redesign). Added to
  backup-host's Backrest as **`usb1-backup`** (local path): prune Sundays 10:00, max unused 10%;
  check on the 1st of each month 11:00, structure only - backup-host's local time (Los Angeles).
- backup-host's `authorized_keys`: `airlock-backups@mac` (the development Mac). homeserver's key goes
  in once homeserver runs v5.9.0.
- **homeserver**: its Backrest (v5.8) is **removed**. `/opt/backrest` is kept only for
  `secrets/credentials`, which holds the repository password - keep a copy elsewhere too.
