# Introducing Airlock Photos

This page is for people who are new to Airlock. It explains what Airlock is
for and where it came from, and then walks through the ideas the rest of the
application is built on: catalogs, import modes, storage routes, events and
albums. Once these are clear, every screen in Airlock is easier to read.

To install it, see the [Installation guide](INSTALLATION.md).

---

## What Airlock is for

Most families end up with photographs and videos scattered across many
devices: phones, digital cameras, old flip cameras, memory cards, backup
drives, and folders copied from one computer to the next. Each source names
and dates its files in its own way. Over the years, the same photo is often
copied several times, and nobody remembers which copy is the original.

Airlock brings all of that media into one organised library, which runs on
your own computer. It keeps four jobs separate, and each can be understood on
its own:

1. **Storing.** Where the files live, and whether Airlock keeps its own copy of them.
2. **Organising.** Putting the media in order, by date and into events, without losing anything.
3. **Managing metadata.** Learning what is in each photo and video: when and where it was taken, who is in it (faces), which pets appear, and a written description.
4. **Publishing.** Choosing photos, collecting them into albums, and showing those albums to other people.

Because the four are separate, you can change how one of them works without
disturbing the others. For example, you can make a new album for a gallery
without touching the files underneath it.

Everything runs on your own machine, and that includes the recognition of
faces and pets and the written descriptions. Your photos leave the machine
only when you decide to publish or send an album.

---

## How it started

Airlock began around **2005** as a Perl script that organised a personal photo
collection. Its job was to copy files, remove duplicates, and sort the photos
by date.

- **Around 2007** the script ran as a mix of bash and Perl. In those years a separate Perl script removed duplicates, as one step of the organising workflow.
- **In 2010** it was rewritten in Python.
- **Also around 2010**, a costly bug touched every file in the gallery and lost the original filesystem creation dates of most of the collection. Since then, Airlock has taken care to keep track of when each file was originally created and last modified. That lesson still shapes how it handles dates today (see *Dates* below).
- **Around 2011** a first web interface was built, in HTML and PHP. It was rarely used: the scripts were still doing most of the work.
- **In 2017** the interface was rewritten on AdminLTE. It was still barely connected to the scripts, which by then organised the photos by year.
- **In 2026** the interface was rewritten completely, in React. The work the scripts did became *workflows* that the interface starts, follows and chains together. For the first time, the interface and the scripts are one application.

The details are in the CHANGELOG, under "Before this
repository".

## How it is available now

Airlock is **self-hosted**. You install it on a computer you control: a Linux
PC or server, a Mac, or a Raspberry Pi. It runs there as a set of small
services in podman containers, and you use it from a web browser, on that
computer or on any other device on your home network. Accounts and passwords
protect it, and the connection is encrypted with HTTPS.

The source code is published on GitHub, and Airlock is free to use for
**personal and non-commercial purposes**; see [license.txt](../../license.txt).
Commercial use needs a separate licence from the author.

---

## The key concepts

### Catalogs

A **catalog** is the top-level container in Airlock. It holds everything that
belongs to one collection:
- the photos and videos;
- the events they are grouped into;
- the faces found in them, and the people those faces belong to;
- pets, places and descriptions;
- albums, and the galleries made from them;
- the catalog's backups.

How you divide your media into catalogs is up to you. Many households need
only one, a family catalog. A photographer, professional or amateur, might
keep a **work** catalog separate from a **personal** one, so that clients'
photos never appear among the family's, and so that each can be shared with
different people.

Some things to know about catalogs:
- **You work in one catalog at a time.** Whatever page you are on, it shows the current catalog: its photos, its people, its events. You switch catalogs from the top of the page.
- **Each catalog is self-contained.** It is one folder on disk, with its own database, so it can be backed up, moved or restored on its own.
- **Catalogs can be shared.** The person who creates a catalog is its owner. An owner can give other accounts access at one of three roles:
  - **viewer:** can look;
  - **editor:** can also change things and run workflows;
  - **owner:** can also share, rename or delete the catalog, set album passwords, publish, and manage backups.

### Import Mode

When you create a catalog, the first decision is its **Import Mode**: how the
catalog treats your original files. It is one of Airlock's key features,
because it lets Airlock fit the way you already keep your photos, instead of
forcing one way on you. You choose one of three modes, and it is fixed once
anything has been imported.

**1. Index files where they are (reference).** Airlock only *reads* your
media, which can be in a local folder or on a network drive. It makes its own
browser-friendly versions (display sizes, thumbnails and video previews), and
keeps those in the catalog. It never copies, moves, renames or changes the
originals. Choose this mode when you already organise your files your own
way, or with another program, and want Airlock to respect that organisation
and leave it alone. It needs the least extra disk space.

**2. Copy files into the catalog (copy).** Airlock *copies* each file into
the catalog, renamed by its date and filed in dated folders, and makes its
browser-friendly versions beside it. Your originals stay exactly where they
were. A catalog in copy mode can take media from many places, through several
storage routes (next section), and gather it all in one organised library.
Keep in mind that this stores a second copy of everything you import, so the
catalog needs roughly as much disk space as the photos themselves, plus the
smaller versions.

**3. Move files into the catalog (move).** Airlock *moves* each file from the
source into the catalog, so the catalog holds the only copy, its master
copy. A file leaves the source only after it is safely recorded in the
catalog, and only after its copy has been checked, byte for byte, against
the original. **Duplicates are not imported.** A file whose content is
already in the catalog is moved to a `DUPS` folder in the source, so you can
see which files were examined and why they were left out. Files Airlock does
not recognise as photos or videos stay where they are, and folders that end
up empty are removed. Choose this mode to empty a memory card, a phone dump or
an old drive into one place.

Different catalogs can use different modes. For example, a family catalog can
copy from the phones, while a work catalog indexes a photo archive that
another program manages.

### Storage routes

A **storage route** tells a catalog where to find media. It is a folder on
the computer, or on a disk or network share mounted on it. Airlock reads from
it according to the catalog's Import Mode.

When you add a storage route, Airlock automatically creates an **Import
workflow** for it in the current catalog, named after the route. Running that
workflow imports whatever is new in the folder. You can run it again whenever
more files arrive. A catalog can have as many storage routes as you have
sources: one for each phone's backup folder, one for the camera's card, one
for an old archive drive.

For safety, every storage route must lie inside one folder that the computer's
owner chooses when installing Airlock (`STORAGE_ROOT_HOST`). Airlock can read
that folder, but it can never write to it.

### Workflows

A **workflow** is one step of work Airlock does on a catalog: importing,
creating events, finding faces, matching them, finding places, and so on.
Each workflow appears as a card on the Workflows page. The steps are chained:
when an import finishes, the steps that depend on it are queued after it.
- **Create Events** groups the new photos.
- **Extract Locations** and **Resolve Locations** turn GPS coordinates into places.
- **Find Faces** and **Measure Faces** find each face, crop it, and measure it.
- **Match Faces by Appearance** and **Re-rank Matches by Context** suggest who each face is, comparing it with the faces you have already named. They only suggest: a person is named only when you confirm it.
- **Measure and Match Pets** and **Group Unnamed Pets** do the same for animals.

You can watch each step on the Workflows page, and an import's progress phase
by phase. Separately from the chain, a local AI model (Ollama), if one is
installed, writes a description of a photo in plain words.

### Dates

The date of a photo is the most important fact Airlock knows about it. It
places the photo on the calendar, in an event, and in a year's album. Airlock
looks for the date in this order:
1. **The photo's own metadata** (EXIF): the moment the camera recorded it.
2. **The folder it came from**, when the folder's name is a date, such as `2019/07/14/`.
3. **Its file name**, when that contains a date, such as `IMG_20190714_103000.jpg`.
4. **The filesystem**, as a last resort: when the file was created on the disk.

Messaging apps often strip EXIF dates, and copying files can change their
filesystem dates. So Airlock records where each date came from, keeps track
of each file's original creation and modification times, and lets you correct
a date when it is wrong.

### Events

Once photos are imported, **Create Events** organises them into **events**
automatically. An event is a group of photos and videos taken close together:
whenever more than about two hours pass between one photo and the next, a new
event begins.

A new event gets a generic name built from its date, such as
`Event 2019-07-14 10:30 (#12)`. The idea is that you then give events real
names, in words: "Beach day", "Grandma's 80th", "First day of school". Those
names are worth the effort, because they are used later: when Airlock builds
albums from events, the event names become the albums' section titles.

Everything you see under **Content**, the photos, the calendar and the map,
is the same imported media, viewed in different ways.

### Albums

An **album** is a chosen group of photos and videos, arranged in categories
and sections. It is a *soft* association: an album points at photos in the
catalog, and doesn't hold copies of them. Deleting an album removes only that
grouping, never the photos.

Albums are kept separate from the library on purpose. You manage your media
in the catalog, and you present it through albums, in as many different ways
as you like, without changing the media itself. One photo can appear in
several albums.

The quickest way to make albums is the **year template**. It takes every
event of a year and builds an album from them, one category for each month
that has events and one section for each event, titled with the event's
name. This is where named events pay off.

### Sharing albums: deployments, galleries and transfers

An album can leave the library in three ways:
- **A deployment** turns an album, or several, into a **gallery**: a web page that people on your network open in a browser, at the catalog's own address. A deployment can have a password, and only a catalog's owner can set one. Without a password, anyone who can reach the gallery can see it.
- **Publishing** puts a deployment on a server of your own on the internet, such as a small Amazon EC2 machine, so that people outside your home can see it. It exposes only that gallery, never the library. A deployment can also be **packed** to run anywhere on its own.
- **A transfer** sends an album's photos directly to another person, with a one-time code. The recipient may have their own Airlock, where the album is imported automatically. Without one, a free command-line tool receives it. **Mass transfer** sends one album to several people at once.

### Backups

A catalog is the one folder you can't afford to lose. Each catalog has a
**Backup** tab, where an owner sets up backups to another disk or another
machine, with restic, borg or rsync. The backups appear among the catalog's
workflows, so you can see when each one last ran.

---

## Where to go next

- [Installation guide](INSTALLATION.md): install Airlock on Linux, a Mac or a Raspberry Pi.
- [Quick start](QUICKSTART.md): the first catalog, storage route and import, step by step.
- [Architecture](../architecture/ARCHITECTURE.md): how the services fit together.
- [Backups](../features/backups.md) and [Publish to EC2](PUBLISH_TO_EC2.md): protecting a catalog, and sharing a gallery outside your home.
