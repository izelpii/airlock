# Backing up a catalog

Each catalog can be backed up to another machine from **Catalogs → (open the catalog) →
Backup**. A backup is a card: where the copy goes, what it contains, when it runs, how long
old copies are kept. From the card you can **back up now**, **verify** the backup,
**restore** from it and **stop** a run in progress, and see at a glance whether the
catalog is backed up. The **Dashboard** shows the same, in its *Backups* card, and the
**Workflows** page draws each backup as a card in a *Backups* row under the workflow
diagram, connected to nothing (backups run on their own schedule, not through the
workflow queue): the pencil opens this tab, the play button is **Back up now**.

Three kinds of backup are available, and you can use several at once:

| | **restic** | **BorgBackup** | **rsync** |
|---|---|---|---|
| What it is | Encrypted, deduplicated snapshots | Encrypted, deduplicated, compressed archives | A plain copy of the catalog's files |
| History | every snapshot you keep | every archive you keep | one copy, kept up to date |
| Encrypted | yes - needs its password | yes - needs its passphrase | **no** - anyone who can open the folder sees the photos |
| The backup machine needs | only SSH (SFTP) | SSH **and borg** ([BORG_INSTALL.md](../guides/BORG_INSTALL.md)) | SSH and rsync (Ubuntu has both) |
| Destination looks like | `sftp:izelpi@192.168.1.16:/media/usb1/backup` | `ssh://izelpi@192.168.1.16/media/usb1/borg/family` | `izelpi@192.168.1.16:/media/usb1/rsync/family` |
| Old copies are cleared | by the backup machine (backup-host's Backrest prunes) | by Airlock, after each backup (prune + compact) | nothing to clear - replaced files are set aside (below) |
| Read it without Airlock | backup-host's Backrest web page | `borg list` / `borg mount` on backup-host | open the folder |

**Which to choose:** restic or borg when you want history - yesterday's or last month's
catalog - and encryption. rsync when you want a copy you can open with any file manager,
on any machine, with no tool and no password: the simplest to understand and to recover
from, at the cost of keeping no history.

Only the catalog's **owner** (or an administrator) can create, change, run or restore a
backup. Everyone who can see the catalog sees the cards and their status.

---

## What a backup contains

Each card backs up one of two scopes:

- **Only originals + the catalog database** - your photos, videos and other files as
  imported, plus a copy of the catalog's database: every name, face, event, album,
  location and date correction. Without the database a restore gives back the files but
  none of that work. This is the one most people want.
- **Everything in the catalog** - the above, plus the previews, video copies and face crops
  in `cache/`. These can all be rebuilt from the originals, but that takes days; backing
  them up makes a full recovery quicker, at the cost of more space.

Before every backup Airlock makes a **consistent copy of the database** (the live one is
being written to all the time), and the backup carries that copy. It needs free space
about the size of the database on the catalog's disk; the backup stops with a clear
message if there isn't enough.

A backup that starts while an **import** is running waits for it to finish.

---

## Setting up a backup

### restic

1. On the Backup tab, click the dashed **restic** card.
2. **Name** it (e.g. *backup-host nightly*) and pick the **scope**.
3. **Repository:**
   - *Use an existing repository* - enter its location and its password.
   - *Create a new one* - enter a location for it and click **Generate** for a password.
     **Copy the password somewhere safe now** (a password manager): without it nobody,
     not even you, can read these backups.
4. If the repository is `sftp:`, the form shows **Airlock's SSH key line**. Add it, once,
   to `~/.ssh/authorized_keys` on the backup machine. It only allows file transfer.
5. Click **Test connection** (or **Create repository**). It must succeed before the card
   can be saved; the first time, it also remembers the backup machine's identity (shown as
   a fingerprint) and will refuse that machine if it ever changes.
6. Choose the **schedule** and how many copies to **keep**, then **Create backup**.

### BorgBackup

The backup machine needs borg installed first - on backup-host it is, at `/opt/borg/bin/borg`
([BORG_INSTALL.md](../guides/BORG_INSTALL.md)).

1. Click the dashed **BorgBackup** card.
2. **Name** it and pick the **scope**.
3. **Repository:** `ssh://user@host/path`, e.g.
   `ssh://izelpi@192.168.1.16/media/usb1/borg/family`. Use one repository per catalog, or
   share one - each card only ever touches its own archives.
4. **Passphrase:** as for restic - existing, or *Generate* for a new repository, and
   **keep a copy somewhere safe**.
5. **borg on the server:** where borg is on the backup machine. On backup-host:
   `/opt/borg/bin/borg` (already filled in).
6. The form shows **Airlock's borg SSH key line**. Add it, once, to
   `~/.ssh/authorized_keys` on the backup machine. It is a **different key from restic's**:
   this one may only run `borg serve`, and only inside the repository's folder
   (`/media/usb1/borg` for the example above), so one line covers every borg repository
   you keep there.
7. **Test connection** / **Create repository**, then schedule and retention, then
   **Create backup**.

### rsync

The backup machine needs SSH and rsync - Ubuntu has both. On backup-host, copies go under
`/media/usb1/rsync/`.

1. Click the dashed **rsync** card.
2. **Name** it and pick the **scope**.
3. **Folder:** `user@host:/path`, e.g. `izelpi@192.168.1.16:/media/usb1/rsync/family` - one
   folder per catalog. *Create a new one* makes the folder.
4. **Folder the key is locked to:** leave it empty - it becomes the folder's parent
   (`/media/usb1/rsync`), so one key line covers every catalog copied there.
5. The form shows **Airlock's rsync SSH key line**. Add it, once, to
   `~/.ssh/authorized_keys` on the backup machine. It is a **third key**, different from
   restic's and borg's: `rrsync` (part of rsync) lets it copy files inside that folder and
   nothing else - no shell, no other folder.
6. **Test connection** / **Create folder**, then the schedule, then **Create backup**.
   There is no password, and no retention.

**What the copy looks like:** the catalog's own layout - `Images/`, `Videos/`, `Others/`,
`cache/backup/` (the database copy), and with *Everything* the rest of `cache/`. Each
backup brings it up to date and only sends what changed.

**Nothing is silently lost.** When a file is deleted or replaced in the catalog, the next
backup does not delete it from the copy: it moves it into
`.airlock-replaced/<date and time>/` inside the copy, in the same layout. Clear that folder
by hand once you are sure. (The previous database copy is the one exception: it is
superseded by the new one and not kept.)

### Schedule and retention

- **Schedule:** *Manual only*, *Daily* at a time, or *Weekly* on a day and time - in your
  browser's time zone. The first scheduled backup is the next one after the card is
  created.
- **Keep** (restic and borg): how many daily, weekly, monthly and yearly copies to keep
  (default 7 / 4 / 12 / 3). After each backup Airlock removes this card's older copies
  beyond these - never another card's, never another Airlock's. rsync keeps one copy.

---

## Reading a card

| Status | Meaning |
|---|---|
| **Up to date** (green) | The last backup succeeded, is recent enough for the schedule, and the catalog has not changed since. |
| **Backup needed** (amber) | No backup yet, the last one is older than the schedule allows (plus a day's grace), or files, faces or names have changed since. The reason is shown. |
| **Running** (blue) | A backup, verify or restore is in progress, with its progress. restic and rsync show a percentage; borg's backup does not know the total in advance, so it shows how much it has read so far. |
| **Failed** (red) | The last backup failed or was cut off by a restart. The reason is shown - a wrong password, the backup machine not answering, the SSH key not added. **Back up now** tries again. |

The card also shows the last backup (when, and how much new data it added), the next
scheduled run, when the backup was last **verified**, and the last restore.

The **first backup** of a large catalog takes hours (about 7-8 hours for 2.7 TB over a
1 Gb network). Later ones only send what changed, usually minutes. On disks whose file
identities are not stable (exFAT, FAT, NTFS, network shares, FUSE) Airlock tells restic
(`--ignore-inode`) and borg (`--files-cache mtime,size`) to treat a file with the same
size and modification time as unchanged, so they do not re-read everything.

---

## The buttons

- **Back up now** - copy the database, back up, then clear old copies by the retention.
- **Verify** - check the backup is whole. restic checks its structure; borg checks the
  repository and every archive in it; rsync compares the copy with the catalog file by
  file, by content, and names the first file that differs (after the catalog changed, a
  backup brings the copy up to date).
- **Restore…** - pick a snapshot (restic) or archive (borg), optionally one folder in it
  (e.g. `/catalogs/family/Images/2019`), and a folder name. The files are restored into
  **`~/airlock-restores/<folder>`** on the machine Airlock runs on - **never over the live
  catalog**. Inside it you find the catalog's full path, e.g.
  `~/airlock-restores/family-20261018/catalogs/family/Images/…` (restic, borg) or
  `~/airlock-restores/family-20261018/Images/…` (rsync, which keeps the catalog's layout).
- **Stop** - ends a run cleanly. The next backup picks up from what was already sent.
- **✎ / 🗑** - edit the card, or remove it. **Removing a card never deletes the backups
  already stored** in the repository.

---

## Bringing a catalog back (disaster recovery)

This is a procedure, not a button:

1. Restore the latest snapshot or archive into a folder (above).
2. Stop Airlock.
3. Put the restored folders back in the catalog's place, and the restored
   `cache/backup/workflow_results.snapshot.db` in place as `workflow_results.db`.
4. Start Airlock. If the application database was lost as well, the restored
   `cache/backup/catalogs.snapshot.db` holds users, sharing and storage routes.

Without Airlock at all, the files can still be read on any machine with the repository's
password or passphrase:

- **restic:** backup-host's Backrest web page (browse and restore), or
  `restic -r sftp:izelpi@192.168.1.16:/media/usb1/backup restore latest --target /somewhere`.
- **borg:** on backup-host, `/opt/borg/bin/borg list /media/usb1/borg/family`, then
  `borg extract` or `borg mount` to browse it as a folder.
- **rsync:** it is already a folder - `/media/usb1/rsync/family` on backup-host. Copy it back
  (`rsync -a backup-host:/media/usb1/rsync/family/ /somewhere/`), or open it.

---

## Keep in mind

- **The password or passphrase is the only key.** Lose it and the backups cannot be read.
  Keep it outside Airlock and outside the backup machine.
- **Test a restore now and then.** A backup you have never restored from is a hope.
- **Two copies are better than one.** restic, borg and rsync cards can back up the same
  catalog to different disks or machines - e.g. borg for history, rsync for a copy anyone
  can open.
- **An rsync copy is not encrypted.** Put it only where you would put the photos
  themselves.
- **Where things are stored in Airlock:** passwords encrypted in the application database;
  SSH keys (one each for restic, borg and rsync) in `database/backup-ssh/`, borg's cache in
  `database/borg/`; restic's cache
  in `database/restic-cache/`.

Design and decisions: backup-backrest-spec.md.
Backup machine setup: [BACKREST_INSTALL.md](../guides/BACKREST_INSTALL.md) (restic, backup-host's
Backrest), [BORG_INSTALL.md](../guides/BORG_INSTALL.md) (borg). rsync needs nothing beyond
the Ubuntu packages (`rsync`, which ships `rrsync`).
