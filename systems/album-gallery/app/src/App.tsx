import React, { useEffect, useMemo, useState } from 'react'
import { RowsPhotoAlbum } from 'react-photo-album'
import type { RenderPhotoProps, RenderPhotoContext } from 'react-photo-album'
import 'react-photo-album/rows.css'
import { createPortal } from 'react-dom'
import { Gallery, Item } from 'react-photoswipe-gallery'
import type { UIElementData } from 'photoswipe'
import 'photoswipe/dist/photoswipe.css'
import './App.css'
import { buildCategoryTree } from './categoryTree'
import type { CategoryNode, RawCategory } from './categoryTree'

type RawAlbum = {
  id: number
  name: string
  description: string | null
  created_at: string
  updated_at: string
  deployed_at: string
  sections: RawSection[]
  // Optional, and it stays optional. Old deployments are not re-published when
  // this app is upgraded, so a manifest without categories is the common case
  // for a long time - not an edge case. Absent means "everything at the root",
  // which renders exactly as this app rendered before categories existed
  // (album-gallery-enhancement-spec.md section 8).
  categories?: RawCategory[]
}

type RawSection = {
  id: number
  name: string
  description: string | null
  position: number
  // Format 1 (every manifest published before per-event pages) carries the
  // photos inline. Format 2 moves them into their own file, named here, so the
  // index stays small and an event's photos are fetched only when it is opened
  // - a 9,000-photo year was a 6.9 MB album.json before anything could show.
  photos?: RawPhoto[]
  photos_file?: string
  // Format 2 only: what the event card needs before its photos are loaded.
  photo_count?: number
  renderable_count?: number
  tags?: string[]
  // Null, absent, or naming a category this manifest does not contain all mean
  // the same thing: render at the root. A section is never dropped for having a
  // category the tree cannot place (spec section 8).
  category_id?: number | null
  cover?: RawCover | null
}

type RawMediaType = 'image' | 'video'

type RawCover = {
  file_id: number
  alt_path: string | null
  width: number | null
  height: number | null
  // Format 2: the cover's square crop, for the event card.
  thumb_path?: string
}

type RawPhoto = {
  file_id: number
  file_name: string
  position: number
  alt_path: string | null
  // Written into the manifest at publish time from alternative_versions, so
  // nothing has to ask a server for image sizes. That is what removed the
  // /api/dimensions round trip, and it is the one API a packed bundle sitting
  // on a static host could never have provided.
  width: number | null
  height: number | null
  datetime: string | null
  tags: string[]
  people: string[]
  location: string | null
  // All optional: a manifest published before videos were described carries
  // none of them, and an entry with no media_type is an image, which is what
  // this app assumed before.
  media_type?: RawMediaType
  // The browser-friendly proxy. Null when none was generated, and the item is
  // then a poster with a badge rather than something dropped.
  video_path?: string | null
  // Always equal to alt_path for a video; present so nothing has to infer that
  // one field means two things.
  poster_path?: string | null
  // Every display size published for this photo, narrowest first. Absent when
  // only the base exists, and absent entirely from manifests published before
  // multiple sizes existed - alt_path still carries the base either way.
  sources?: { rung: string; path: string; width: number; height: number }[]
  // Square crops. NOT interchangeable with `sources`: a rows layout needs the
  // photo's own aspect ratio, so these must never reach a srcset. Described
  // here for the gallery types that lay photos out in squares.
  thumbnails?: { rung: string; path: string; size: number }[]
}

// deployment.json. A LOCKED deployment answers with name and requires_password
// and nothing else — no album list, no photo count, no folder name.
type DeploymentMeta = {
  slug: string
  name: string
  description?: string | null
  requires_password: boolean
  locked: boolean
}

type AlbumManifestEntry = {
  id: string
  folder: string
  jsonPath: string
}

type AlbumMediaRecord = {
  id: string
  sourceId: number
  originalFileName: string
  position: number
  mediaType: 'image' | 'video' | 'unsupported'
  imageUrl: string | null
  // Resolved URLs for every display size, narrowest first. Always at least the
  // one `imageUrl` names, so callers never have to special-case an empty list.
  // `rung` is null only for that fallback entry, which no manifest named.
  sources: { src: string; width: number; height: number; rung: string | null }[]
  // The square crops, smallest first. Listed in the photo details, never used
  // in a srcset - see RawPhoto.thumbnails.
  thumbnails: { src: string; size: number; rung: string }[]
  capturedAt: string | null
  tags: string[]
  people: string[]
  location: string | null
  isRenderable: boolean
  width: number
  height: number
  videoUrl: string | null
  posterUrl: string | null
  // A small square for navigation icons: the 320px crop when one was packed,
  // otherwise the display image. Null only when there is nothing to show.
  thumbUrl: string | null
}

type GalleryPhoto = {
  key: string
  src: string
  width: number
  height: number
  alt: string
  // Handed to react-photo-album, which computes the srcset and sizes
  // attributes itself. Grid tiles are 200-400px tall, so this is what stops a
  // 2048px file being downloaded to fill one.
  srcSet?: { src: string; width: number; height: number }[]
  // The largest size there is, and ITS intrinsic dimensions. PhotoSwipe uses
  // width/height to compute zoom bounds, so describing the 1200px file while
  // serving the 2048px one makes zoom subtly wrong rather than visibly broken.
  originalSrc: string
  originalWidth: number
  originalHeight: number
  originalSrcSet?: string
  // Set only for a video. `src` stays the poster, so layout, alt text and every
  // fallback path behave exactly as they do for a photo.
  videoUrl?: string | null
  isVideo?: boolean
}

type AlbumSectionRecord = {
  id: string
  sourceId: number
  title: string
  description: string | null
  position: number
  itemCount: number
  renderableItemCount: number
  // null until loaded: a format-2 section's photos are in `photosFile`, fetched
  // when the event is opened. A format-1 section has them from the start.
  items: AlbumMediaRecord[] | null
  photosFile: string | null
  // Every tag on any photo here - for filtering event cards without loading
  // the events.
  tags: string[]
  categoryId: number | null
  coverImageUrl: string | null
  coverThumbUrl: string | null
}

// One node of the category tree. The root is a node too, with sourceId null and
// the album's name as its title, so rendering never needs a special case for it.
// The shape and the placement rules live in categoryTree.ts, which is pure and
// has its own tests.
type AlbumCategoryNode = CategoryNode<AlbumSectionRecord>

type AlbumRecord = {
  id: string
  sourceId: number
  title: string
  description: string | null
  createdAt: string
  updatedAt: string
  deployedAt: string
  coverImageUrl: string | null
  coverThumbUrl: string | null
  availableTags: string[]
  // Kept flat and in publish order. The tree is a view of these, not a
  // replacement: every section appears in exactly one node of categoryTree.
  sections: AlbumSectionRecord[]
  categoryTree: AlbumCategoryNode
  categoryCount: number
}

// Every fetch and every image src in this app is RELATIVE to the page.
//
// The same build is served from three different prefixes — /d/family/summer-2019/
// straight from the gallery, /albums/family/summer-2019/ behind nginx, and a
// bucket prefix in a packed bundle — and only relative paths are correct in all
// three. An absolute /albums.json would reach the main application instead of
// this one behind the gateway, which is the failure this replaced.
//
// document.baseURI is the browser's own answer to "what am I relative to", so
// it already accounts for the trailing slash the server redirects to add.
function asset(relative: string): string {
  return new URL(relative, document.baseURI).toString()
}

async function fetchDeployment(): Promise<DeploymentMeta | null> {
  try {
    const response = await fetch(asset('deployment.json'))
    if (!response.ok) return null
    return await response.json()
  } catch {
    return null
  }
}

async function fetchManifest(): Promise<AlbumManifestEntry[]> {
  const response = await fetch(asset('albums.json'))
  if (!response.ok) return []
  const folders: string[] = await response.json()
  return folders.map((folder) => ({
    id: folder,
    folder,
    jsonPath: `albums/${folder}/album.json`,
  }))
}

async function loadAlbums(): Promise<AlbumRecord[]> {
  const manifest = await fetchManifest()
  const results = await Promise.all(
    manifest.map(async (entry) => {
      try {
        const response = await fetch(asset(entry.jsonPath))
        if (!response.ok) return null
        const rawAlbum: RawAlbum = await response.json()
        return normalizeAlbum(entry, rawAlbum)
      } catch {
        return null
      }
    }),
  )
  return results.filter((album): album is AlbumRecord => album !== null)
}

/** A format-2 section's photos, fetched when the event is opened. */
async function loadSectionItems(
  albumFolder: string,
  section: AlbumSectionRecord,
): Promise<AlbumMediaRecord[]> {
  if (section.items) return section.items
  if (!section.photosFile) return []
  const response = await fetch(asset(`albums/${albumFolder}/${section.photosFile}`))
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  const data: { photos?: RawPhoto[] } = await response.json()
  const entry = { id: albumFolder, folder: albumFolder, jsonPath: '' }
  return [...(data.photos ?? [])]
    .sort((left, right) => left.position - right.position)
    .map((photo) => normalizePhoto(entry, section.sourceId, photo))
}

function normalizeAlbum(entry: AlbumManifestEntry, album: RawAlbum): AlbumRecord {
  const sections = [...album.sections]
    .sort((left, right) => left.position - right.position)
    .map((section) => normalizeSection(entry, section))

  const availableTags = Array.from(
    new Set(sections.flatMap((section) => section.tags)),
  ).sort((left, right) => left.localeCompare(right))

  // The first event with a picture. Sections are known without their photos,
  // so this no longer needs every photo of the album loaded.
  const coverSection = sections.find((section) => section.coverImageUrl)
  const coverImageUrl = coverSection?.coverImageUrl ?? null

  const rawCategories = album.categories ?? []

  return {
    id: entry.id,
    sourceId: album.id,
    title: album.name,
    description: album.description,
    createdAt: album.created_at,
    updatedAt: album.updated_at,
    deployedAt: album.deployed_at,
    coverImageUrl,
    coverThumbUrl: coverSection?.coverThumbUrl ?? coverImageUrl,
    availableTags,
    sections,
    categoryTree: buildCategoryTree(entry.id, album.name, rawCategories, sections),
    categoryCount: rawCategories.length,
  }
}

function normalizeSection(
  entry: AlbumManifestEntry,
  section: RawSection,
): AlbumSectionRecord {
  // Format 1 only. A format-2 section's photos arrive later, from photos_file.
  const items = section.photos
    ? [...section.photos]
      .sort((left, right) => left.position - right.position)
      .map((photo) => normalizePhoto(entry, section.id, photo))
    : null

  // The publisher's chosen cover. Falls back to the first renderable photo when
  // the photos are here, which is what this app used before covers existed.
  const coverFromManifest = section.cover?.alt_path
    ? toPublicAssetPath(section.cover.alt_path, entry.folder)
    : null
  const firstRenderable = items?.find((item) => item.isRenderable)
  const coverImageUrl = coverFromManifest ?? firstRenderable?.imageUrl ?? null
  // Format 2 names the cover's square crop; format 1 has it on the photo itself.
  const coverItem = section.cover
    ? items?.find((item) => item.sourceId === section.cover?.file_id)
    : firstRenderable
  const coverThumbUrl = section.cover?.thumb_path
    ? toPublicAssetPath(section.cover.thumb_path, entry.folder)
    : coverItem?.thumbUrl ?? coverImageUrl

  return {
    id: `${entry.id}-${section.id}`,
    sourceId: section.id,
    title: section.name,
    description: section.description,
    position: section.position,
    itemCount: items ? items.length : section.photo_count ?? 0,
    renderableItemCount: items
      ? items.filter((item) => item.isRenderable).length
      : section.renderable_count ?? section.photo_count ?? 0,
    items,
    photosFile: items ? null : section.photos_file ?? null,
    tags: items
      ? Array.from(new Set(items.flatMap((item) => item.tags)))
      : section.tags ?? [],
    // undefined (an older manifest) and null both mean the root.
    categoryId: section.category_id ?? null,
    coverImageUrl,
    coverThumbUrl,
  }
}

function normalizePhoto(
  entry: AlbumManifestEntry,
  sectionId: number,
  photo: RawPhoto,
): AlbumMediaRecord {
  const imageUrl = photo.alt_path ? toPublicAssetPath(photo.alt_path, entry.folder) : null

  // The manifest says what this is. It used to be inferred from whether an
  // image URL existed, which made every video an 'image' the moment its poster
  // frame was published - and then isRenderable dropped anything that was not.
  // Extension-sniffing stays as the fallback for manifests published before
  // media_type existed.
  const extension = photo.file_name.split('.').pop()?.toLowerCase() ?? ''
  const declared = photo.media_type
  const looksLikeVideo = ['mov', 'mp4', 'm4v', 'avi', '3gp'].includes(extension)
  const mediaType: AlbumMediaRecord['mediaType'] =
    declared === 'video' || (!declared && looksLikeVideo)
      ? 'video'
      : imageUrl
        ? 'image'
        : 'unsupported'

  const videoUrl = photo.video_path
    ? toPublicAssetPath(photo.video_path, entry.folder)
    : null
  // poster_path is the same file as alt_path for a video; either is fine, and
  // taking both means an older manifest still yields a poster.
  const posterUrl = photo.poster_path
    ? toPublicAssetPath(photo.poster_path, entry.folder)
    : imageUrl

  // Only entries that carry real dimensions: a srcset entry with a wrong or
  // missing width makes the browser pick badly, which is worse than not
  // offering the choice. Falls back to the single image the manifest already
  // described, so a manifest without `sources` behaves exactly as before.
  const sources = (photo.sources ?? [])
    .filter((entry_) => entry_.width > 0 && entry_.height > 0)
    .map((entry_) => ({
      src: toPublicAssetPath(entry_.path, entry.folder),
      width: entry_.width,
      height: entry_.height,
      rung: entry_.rung,
    }))
    .sort((a, b) => a.width - b.width)

  const thumbnails = (photo.thumbnails ?? [])
    .filter((thumb) => thumb.size > 0)
    .map((thumb) => ({
      src: toPublicAssetPath(thumb.path, entry.folder),
      size: thumb.size,
      rung: thumb.rung,
    }))
    .sort((a, b) => a.size - b.size)

  return {
    id: `${entry.id}-${sectionId}-${photo.file_id}`,
    sourceId: photo.file_id,
    originalFileName: photo.file_name,
    position: photo.position,
    mediaType,
    imageUrl,
    sources:
      sources.length > 0
        ? sources
        : imageUrl
          ? [{ src: imageUrl, width: photo.width ?? 4, height: photo.height ?? 3, rung: null }]
          : [],
    thumbnails,
    capturedAt: photo.datetime,
    tags: photo.tags,
    people: photo.people,
    location: photo.location,
    videoUrl,
    posterUrl,
    thumbUrl: thumbnails[0]?.src ?? posterUrl,
    // A video counts as renderable when there is anything to show: the proxy,
    // or failing that its poster. 16 of 178 videos in the reference catalog
    // have no proxy, and showing a still frame with a badge is better than
    // silently omitting the clip from the album.
    isRenderable:
      mediaType === 'video'
        ? videoUrl !== null || posterUrl !== null
        : mediaType === 'image' && imageUrl !== null,
    // Straight from the manifest. The 4:3 fallback is now only reached by a
    // photo the publisher could not measure at all, rather than by every photo
    // until a second request came back.
    width: photo.width ?? 4,
    height: photo.height ?? 3,
  }
}

function toPublicAssetPath(altPath: string, folder: string): string {
  // Strip the leading cache/Albums/<any-folder>/ prefix (the folder baked into
  // alt_path may differ from the actual directory if the album was renamed/moved)
  // then rebuild the URL using the real folder from the manifest entry.
  //
  // RELATIVE, with no leading slash: see asset() above for why that matters.
  const filename = altPath.replace(/^cache\/Albums\/[^/]+\//, '')
  return asset(`albums/${folder}/${filename}`)
}

// ─── Where the page is: the URL hash ─────────────────────────────────────────
// Every album, category and event has its own address, so an event can be
// bookmarked and shared, and Back steps between them:
//
//   #/00002          the album's root
//   #/00002/c/3      category 3 of album 00002 (a month, in a year album)
//   #/00002/e/12     event (section) 12
//
// In the HASH rather than the path: the server never sees it, so nothing about
// serving changes - the same build works behind the gallery server, behind the
// gateway, and as a packed bundle on a static host, none of which could route a
// deeper path back to index.html, and all of the app's relative URLs (asset()
// above) stay correct. The ids are the database's, stable across republishing.
type Route =
  | { kind: 'album'; albumId: string | null }
  | { kind: 'category'; albumId: string; categoryId: number }
  | { kind: 'event'; albumId: string; sectionId: number }

function parseRoute(hash: string): Route {
  const [albumId, kind, rawId] = hash.replace(/^#\/?/, '').split('/').filter(Boolean)
  const id = Number(rawId)
  if (albumId && kind === 'e' && Number.isInteger(id)) return { kind: 'event', albumId, sectionId: id }
  if (albumId && kind === 'c' && Number.isInteger(id)) return { kind: 'category', albumId, categoryId: id }
  return { kind: 'album', albumId: albumId ?? null }
}

/** The first event at or below a node, depth-first: the sidebar's order. */
function firstSectionUnder(node: AlbumCategoryNode): AlbumSectionRecord | null {
  if (node.sections.length > 0) return node.sections[0]
  for (const child of node.children) {
    const found = firstSectionUnder(child)
    if (found) return found
  }
  return null
}

function albumHref(albumId: string): string {
  return `#/${albumId}`
}

function categoryHref(albumId: string, node: AlbumCategoryNode): string {
  return node.sourceId === null ? albumHref(albumId) : `#/${albumId}/c/${node.sourceId}`
}

function eventHref(albumId: string, section: AlbumSectionRecord): string {
  return `#/${albumId}/e/${section.sourceId}`
}

function toGalleryPhoto(item: AlbumMediaRecord): GalleryPhoto {
  // For a video this is the poster, which is what the layout and the <video>
  // element both want.
  const src = (item.mediaType === 'video' ? item.posterUrl : item.imageUrl) ?? ''
  // A video has no size ladder - its poster is one file - so everything below
  // degrades to `src` for it, which is what it did before sizes existed.
  const ladder = item.mediaType === 'video' ? [] : item.sources
  const largest = ladder.length > 0 ? ladder[ladder.length - 1] : null
  return {
    key: item.id,
    src,
    width: item.width,
    height: item.height,
    alt: item.originalFileName,
    isVideo: item.mediaType === 'video',
    videoUrl: item.videoUrl,
    // Offered only when there is a real choice. One entry is not a srcset, it
    // is the same file described twice.
    srcSet: ladder.length > 1 ? ladder : undefined,
    originalSrc: largest?.src ?? src,
    originalWidth: largest?.width ?? item.width,
    originalHeight: largest?.height ?? item.height,
    originalSrcSet:
      ladder.length > 1 ? ladder.map((s) => `${s.src} ${s.width}w`).join(', ') : undefined,
  }
}

// When a photo was taken. A value the browser cannot parse (an EXIF-style
// "2019:07:14 10:22:31", say) is shown as written rather than as nothing.
function formatDateTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return value
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

/**
 * A video tile: the poster at rest, playing on hover, opening on click.
 *
 * The same behaviour the Events gallery uses in the main application, and for
 * the same reasons. muted+playsInline because a browser will refuse to
 * autoplay anything else; preload="none" because with a poster there is
 * nothing to gain from fetching metadata, and "metadata" would cost one request
 * per tile on every page turn.
 */
function VideoTile({
  photo,
  width,
  height,
  onOpen,
}: {
  photo: GalleryPhoto
  width: number
  height: number
  onOpen: () => void
}) {
  const playable = Boolean(photo.videoUrl)
  return (
    <div
      style={{ position: 'relative', width, height, cursor: playable ? 'pointer' : 'default' }}
      onClick={playable ? onOpen : undefined}
    >
      {playable ? (
        <video
          src={photo.videoUrl ?? undefined}
          poster={photo.src}
          preload="none"
          muted
          loop
          playsInline
          title={photo.alt}
          style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
          onMouseEnter={(e) => { void e.currentTarget.play().catch(() => {}) }}
          onMouseLeave={(e) => {
            e.currentTarget.pause()
            e.currentTarget.currentTime = 0
          }}
        />
      ) : (
        <img
          src={photo.src}
          alt={photo.alt}
          width={width}
          height={height}
          loading="lazy"
          decoding="async"
          style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
        />
      )}
      {/* Hover-to-play is invisible on a touch screen, so a video reads as a
          video at rest. A clip with no proxy says so rather than looking like
          a tile that ignores clicks. */}
      <span className="video-badge">
        <span aria-hidden="true">{playable ? '▶' : '⚠'}</span>
        {playable ? 'Video' : 'Video (no preview)'}
      </span>
    </div>
  )
}

/**
 * The tile renderer. A factory rather than a plain function because a video
 * tile has to reach the overlay, and PhotoSwipe only handles the photos.
 */
function makeRenderPhoto(onOpenVideo: (photo: GalleryPhoto) => void) {
  return function renderAlbumPhoto(
    _: RenderPhotoProps,
    { photo, width, height }: RenderPhotoContext<GalleryPhoto>,
  ) {
    if (photo.isVideo) {
      return (
        <VideoTile photo={photo} width={width} height={height}
          onOpen={() => onOpenVideo(photo)} />
      )
    }
    return (
      <Item
        // The LARGEST size available, not the one the grid uses. This is the
        // whole point of publishing more than one: a 1200px file shown
        // full-screen on a modern display is upscaled.
        original={photo.originalSrc}
        originalSrcset={photo.originalSrcSet}
        thumbnail={photo.src}
        // The intrinsic size of `original`, not of the tile - PhotoSwipe
        // computes its zoom bounds from these.
        width={photo.originalWidth}
        height={photo.originalHeight}
        alt={photo.alt}
        // Becomes the slide's pid, which is how the details panel finds the
        // photo the lightbox is showing.
        id={photo.key}
      >
        {({ ref, open }) => (
          <img
            ref={ref as React.Ref<HTMLImageElement>}
            src={photo.src}
            alt={photo.alt}
            width={width}
            height={height}
            loading="lazy"
            decoding="async"
            onClick={open}
            style={{ display: 'block', cursor: 'pointer' }}
          />
        )}
      </Item>
    )
  }
}

// ─── Photo details in the lightbox ───────────────────────────────────────────

/** Everything the details panel says about one photo, found by the slide's pid. */
type PhotoInfo = {
  item: AlbumMediaRecord
  section: AlbumSectionRecord
  album: AlbumRecord
  index: number
}

const PHOTO_INFO_KEY = 'album-gallery.photo-info-open'

function readPhotoInfoOpen(): boolean {
  try {
    return localStorage.getItem(PHOTO_INFO_KEY) === '1'
  } catch {
    return false
  }
}

// PhotoSwipe draws its own buttons from markup, not React, so the icon is a
// string. Same 32px box and fill convention as its built-in icons.
const INFO_ICON =
  '<svg aria-hidden="true" class="pswp__icn" viewBox="0 0 32 32" width="32" height="32">'
  + '<path d="M16 6.5a9.5 9.5 0 1 0 0 19 9.5 9.5 0 0 0 0-19Zm0 2a7.5 7.5 0 1 1 0 15 7.5 7.5 0 0 1 0-15Z"/>'
  + '<rect x="14.9" y="14" width="2.2" height="7" rx="1.1"/>'
  + '<circle cx="16" cy="11" r="1.4"/>'
  + '</svg>'

function sizeLabel(rung: string | null, width: number): string {
  if (rung === 'full') return 'Full resolution'
  if (rung === null) return 'Display image'
  return `${Number(rung) || width} px`
}

/**
 * The details panel. Rendered by a portal into an element PhotoSwipe owns, so
 * it sits inside the lightbox and goes when the lightbox goes. Kept mounted
 * while hidden - the slide in and out is a transition on this element, and an
 * element that is not there has nothing to transition.
 */
function PhotoInfoPanel({ info, open }: { info: PhotoInfo | null; open: boolean }) {
  return (
    <aside
      className={open ? 'photo-info photo-info--open' : 'photo-info'}
      aria-label="Photo details"
      aria-hidden={!open}
      inert={!open}
    >
      {info && <PhotoInfoBody info={info} />}
    </aside>
  )
}

function PhotoInfoBody({ info }: { info: PhotoInfo }) {
  const { item, section, album, index } = info
  const isVideo = item.mediaType === 'video'
  const largest = item.sources[item.sources.length - 1]

  return (
    <>
      <p className="photo-info__eyebrow">{isVideo ? 'Video' : 'Photo'} details</p>
      <h2 className="photo-info__title">{item.originalFileName}</h2>

      <dl className="photo-info__facts">
        {item.capturedAt && (
          <div>
            <dt>Taken</dt>
            <dd>{formatDateTime(item.capturedAt)}</dd>
          </div>
        )}
        {item.location && (
          <div>
            <dt>Location</dt>
            <dd>{item.location}</dd>
          </div>
        )}
        {item.people.length > 0 && (
          <div>
            <dt>People</dt>
            <dd>{item.people.join(', ')}</dd>
          </div>
        )}
        <div>
          <dt>Album</dt>
          <dd>{album.title}</dd>
        </div>
        <div>
          <dt>Section</dt>
          <dd>
            {section.title}
            <span className="photo-info__muted"> · {index + 1} of {section.itemCount}</span>
          </dd>
        </div>
        {largest && (
          <div>
            <dt>Largest size</dt>
            <dd>{largest.width} × {largest.height}</dd>
          </div>
        )}
      </dl>

      <h3 className="photo-info__heading">Tags</h3>
      {item.tags.length === 0 ? (
        <p className="photo-info__muted">No tags.</p>
      ) : (
        <ul className="photo-info__tags">
          {item.tags.map((tag) => <li key={tag}>{tag}</li>)}
        </ul>
      )}

      {/* Every file published for this photo, each opening on its own. What
          is listed is what this deployment packed - a size not packed has no
          file to link to. */}
      <h3 className="photo-info__heading">Available sizes</h3>
      <ul className="photo-info__sizes">
        {isVideo && item.videoUrl && (
          <li>
            <a href={item.videoUrl} target="_blank" rel="noopener noreferrer">
              <span>Video</span>
              <span className="photo-info__muted">playable copy</span>
            </a>
          </li>
        )}
        {item.sources.map((source) => (
          <li key={source.src}>
            <a href={source.src} target="_blank" rel="noopener noreferrer">
              <span>{isVideo ? 'Poster frame' : sizeLabel(source.rung, source.width)}</span>
              <span className="photo-info__muted">{source.width} × {source.height}</span>
            </a>
          </li>
        ))}
        {item.thumbnails.map((thumb) => (
          <li key={thumb.src}>
            <a href={thumb.src} target="_blank" rel="noopener noreferrer">
              <span>Square thumbnail</span>
              <span className="photo-info__muted">{thumb.size} × {thumb.size}</span>
            </a>
          </li>
        ))}
      </ul>
    </>
  )
}

/** Click-through playback: the proxy, with controls, at a usable size. */
function VideoOverlay({ photo, onClose }: { photo: GalleryPhoto; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="video-overlay" onClick={onClose} role="dialog" aria-label={photo.alt}>
      <button type="button" className="video-overlay__close" onClick={onClose}
        aria-label="Close">×</button>
      {/* Stop the click on the video itself from closing the overlay, or the
          controls would dismiss it instead of working. */}
      <video
        src={photo.videoUrl ?? undefined}
        poster={photo.src}
        controls
        autoPlay
        playsInline
        onClick={(e) => e.stopPropagation()}
        className="video-overlay__player"
      />
    </div>
  )
}

/**
 * A navigation icon: the cover's square crop, or the name's initials when there
 * is no picture to show. Decorative - the control around it carries the name.
 */
function Thumb({ src, name, className }: { src: string | null; name: string; className: string }) {
  if (src) {
    return (
      <span className={className} aria-hidden="true">
        <img src={src} alt="" loading="lazy" decoding="async" />
      </span>
    )
  }
  const initials = name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0].toUpperCase())
    .join('')
  return (
    <span className={`${className} thumb--empty`} aria-hidden="true">
      {initials || '?'}
    </span>
  )
}

/**
 * What the closed panel still offers: one icon per album, then one per section
 * of the album on screen, so browsing does not need the panel open. Sections
 * are in tree order - depth-first, a category's own sections before its
 * sub-categories - which is the order the open panel lists them in.
 */
function SidebarRail({
  albums,
  selectedAlbum,
  selectedNodeId,
  currentSectionId,
  inert,
  onSelectAlbum,
  onOpenSection,
}: {
  albums: AlbumRecord[]
  selectedAlbum: AlbumRecord
  selectedNodeId: string | null
  currentSectionId: string | null
  inert: boolean
  onSelectAlbum: (album: AlbumRecord) => void
  onOpenSection: (section: AlbumSectionRecord) => void
}) {
  const sections = useMemo(() => {
    const out: { node: AlbumCategoryNode; section: AlbumSectionRecord }[] = []
    const walk = (node: AlbumCategoryNode) => {
      for (const section of node.sections) out.push({ node, section })
      node.children.forEach(walk)
    }
    walk(selectedAlbum.categoryTree)
    return out
  }, [selectedAlbum])

  return (
    <nav className="sidebar__rail" aria-label="Albums and sections" inert={inert}>
      {/* A single album has nowhere else to go, so its icon would only take
          room from the sections. */}
      {albums.length > 1 && (
        <>
          <ul className="rail__list">
            {albums.map((album) => {
              const active = album.id === selectedAlbum.id
              return (
                <li key={album.id}>
                  <button
                    type="button"
                    className={active ? 'rail__button rail__button--active' : 'rail__button'}
                    title={album.title}
                    aria-label={album.title}
                    aria-current={active ? 'true' : undefined}
                    onClick={() => onSelectAlbum(album)}
                  >
                    <Thumb src={album.coverThumbUrl} name={album.title} className="thumb" />
                  </button>
                </li>
              )
            })}
          </ul>
          <hr className="rail__divider" />
        </>
      )}
      <ul className="rail__list">
        {sections.map(({ node, section }) => (
          <li key={section.id}>
            <button
              type="button"
              // "Here" marks the event on screen, or on a category page every
              // event that page lists.
              className={(currentSectionId
                ? section.id === currentSectionId
                : node.id === selectedNodeId)
                ? 'rail__button rail__button--section rail__button--here'
                : 'rail__button rail__button--section'}
              title={section.title}
              aria-label={section.title}
              aria-current={section.id === currentSectionId ? 'page' : undefined}
              onClick={() => onOpenSection(section)}
            >
              <Thumb src={section.coverThumbUrl} name={section.title} className="thumb" />
            </button>
          </li>
        ))}
      </ul>
    </nav>
  )
}

/**
 * The sidebar tree.
 *
 * Hand-rolled rather than pulled from a component library. MUI X Tree View would
 * bring @mui/material and @emotion with it - several hundred kilobytes into a
 * bundle that is currently 276KB in total - and would then need theming to stop
 * looking like a different application. The tree DATA already exists
 * (categoryTree.ts); all that was missing was disclosure and ARIA, which is the
 * small half.
 *
 * A category with nothing under it anywhere is still listed. It was published
 * deliberately, and a navigation that hides the empty ones disagrees with the
 * album it describes.
 */
function CategoryTree({
  albumId,
  root,
  selectedId,
  currentSectionId,
  onSelect,
}: {
  albumId: string
  root: AlbumCategoryNode
  selectedId: string
  currentSectionId: string | null
  onSelect: (node: AlbumCategoryNode) => void
}) {
  // Collapsed by id. Everything starts expanded: an album's tree is a handful
  // of nodes, and opening it to find anything would be work the user did not
  // ask for.
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set())

  const toggle = (id: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  return (
    <ul className="tree" role="tree" aria-label="Album contents">
      <CategoryTreeNode
        albumId={albumId}
        node={root}
        collapsed={collapsed}
        onToggle={toggle}
        selectedId={selectedId}
        currentSectionId={currentSectionId}
        onSelect={onSelect}
        isRoot
      />
    </ul>
  )
}

function CategoryTreeNode({
  albumId,
  node,
  collapsed,
  onToggle,
  selectedId,
  currentSectionId,
  onSelect,
  isRoot = false,
}: {
  albumId: string
  node: AlbumCategoryNode
  collapsed: Set<string>
  onToggle: (id: string) => void
  selectedId: string
  currentSectionId: string | null
  onSelect: (node: AlbumCategoryNode) => void
  isRoot?: boolean
}) {
  const hasChildren = node.children.length > 0 || node.sections.length > 0
  const isOpen = !collapsed.has(node.id)
  // On an event page the event is what is selected, not its category.
  const isSelected = node.id === selectedId && currentSectionId === null
  // aria-level is 1-based; the root sits at depth 0.
  const level = node.depth + 1

  const rowClass = [
    'tree__row',
    isRoot ? 'tree__row--root' : '',
    isSelected ? 'tree__row--selected' : '',
  ].filter(Boolean).join(' ')

  return (
    <li
      className="tree__item"
      role="treeitem"
      aria-expanded={hasChildren ? isOpen : undefined}
      aria-selected={isSelected}
      aria-level={level}
    >
      <div className={rowClass}>
        {hasChildren ? (
          <button
            type="button"
            className="tree__twisty"
            // Expanding is not selecting. Collapsing a category you are looking
            // at should not throw you somewhere else.
            onClick={(e) => { e.stopPropagation(); onToggle(node.id) }}
            aria-label={`${isOpen ? 'Collapse' : 'Expand'} ${node.title}`}
          >
            <span aria-hidden="true">{isOpen ? '\u25be' : '\u25b8'}</span>
          </button>
        ) : (
          <span className="tree__twisty tree__twisty--leaf" aria-hidden="true" />
        )}
        <button
          type="button"
          className="tree__label tree__label--button"
          title={node.title}
          onClick={() => onSelect(node)}
        >
          {node.title}
        </button>
        {node.descendantSectionCount > 0 && (
          <span className="tree__count">{node.descendantSectionCount}</span>
        )}
      </div>

      {isOpen && hasChildren && (
        <ul className="tree__children" role="group">
          {node.sections.map((section) => (
            <li key={section.id} className="tree__item" role="treeitem" aria-level={level + 1}
              aria-selected={section.id === currentSectionId}>
              {/* A real link to the event's own page: it opens in a new tab,
                  and copying it copies the event's address. */}
              <a
                href={eventHref(albumId, section)}
                className={section.id === currentSectionId
                  ? 'tree__row tree__row--link tree__row--selected'
                  : 'tree__row tree__row--link'}
                aria-current={section.id === currentSectionId ? 'page' : undefined}
              >
                <span className="tree__twisty tree__twisty--leaf" aria-hidden="true" />
                <Thumb src={section.coverThumbUrl} name={section.title} className="thumb tree__thumb" />
                <span className="tree__label" title={section.title}>{section.title}</span>
                <span
                  className="tree__count"
                  title={`${section.renderableItemCount} of ${section.itemCount} viewable`}
                >
                  {section.renderableItemCount}
                </span>
              </a>
            </li>
          ))}
          {node.children.map((child) => (
            <CategoryTreeNode
              key={child.id}
              albumId={albumId}
              node={child}
              collapsed={collapsed}
              onToggle={onToggle}
              selectedId={selectedId}
              currentSectionId={currentSectionId}
              onSelect={onSelect}
            />
          ))}
        </ul>
      )}
    </li>
  )
}

/**
 * ONE category's page: a card per event directly in it, and links to its
 * sub-categories.
 *
 * Cards, not photos. An event is its own page (EventView), so a month holding
 * 5,000 photos across three events is three cards here, and only the event
 * that is opened is ever downloaded or drawn.
 *
 * The tag filter narrows the cards to the events that hold the tag - known from
 * the index, without opening any of them.
 */
function CategoryView({
  album,
  node,
  selectedTag,
  onSelect,
}: {
  album: AlbumRecord
  node: AlbumCategoryNode
  selectedTag: string | null
  onSelect: (node: AlbumCategoryNode) => void
}) {
  const sections = selectedTag
    ? node.sections.filter((section) => section.tags.includes(selectedTag))
    : node.sections

  return (
    <div className="category-block">
      {node.children.length > 0 && (
        <nav className="subcategories" aria-label="Sub-categories">
          {node.children.map((child) => (
            <button
              key={child.id}
              type="button"
              className="subcategory-pill"
              onClick={() => onSelect(child)}
            >
              <span>{child.title}</span>
              <small>
                {child.descendantSectionCount} event
                {child.descendantSectionCount === 1 ? '' : 's'}
              </small>
            </button>
          ))}
        </nav>
      )}

      {sections.length === 0 ? (
        <div className="empty-state">
          {node.sections.length === 0
            ? node.children.length > 0
              ? 'Nothing directly in this category — open one of the sub-categories above.'
              : 'This category is empty.'
            : 'No events here have photos with this tag.'}
        </div>
      ) : (
        <ul className="event-cards">
          {sections.map((section) => (
            <li key={section.id}>
              <a className="event-card" href={eventHref(album.id, section)}>
                <span className="event-card__cover">
                  {section.coverThumbUrl ?? section.coverImageUrl ? (
                    <img
                      src={section.coverThumbUrl ?? section.coverImageUrl ?? undefined}
                      alt=""
                      loading="lazy"
                      decoding="async"
                    />
                  ) : (
                    <span className="event-card__empty" aria-hidden="true">No photos</span>
                  )}
                </span>
                <span className="event-card__body">
                  <span className="event-card__title">{section.title}</span>
                  <span className="event-card__count">
                    {section.renderableItemCount} photo
                    {section.renderableItemCount === 1 ? '' : 's'}
                  </span>
                </span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// How many tiles an event page draws at a time. The images are lazy either
// way; this bounds the DOM and the layout pass, which is what makes a
// 3,000-photo event slow to open - the rest follow as the page is scrolled.
const EVENT_PAGE_SIZE = 240

/**
 * ONE event's page: its photos, drawn progressively.
 *
 * The lightbox steps through the tiles drawn so far; scrolling the page draws
 * the next batch, which the lightbox then includes the next time it is opened.
 */
function EventView({
  section,
  items,
  failed,
  selectedTag,
  renderPhoto,
  onRetry,
}: {
  section: AlbumSectionRecord
  items: AlbumMediaRecord[] | null
  failed: boolean
  selectedTag: string | null
  renderPhoto: ReturnType<typeof makeRenderPhoto>
  onRetry: () => void
}) {
  const [shown, setShown] = useState(EVENT_PAGE_SIZE)
  const sentinel = React.useRef<HTMLDivElement>(null)

  const visible = useMemo(
    () => (items ?? []).filter(
      (item) => item.isRenderable && (!selectedTag || item.tags.includes(selectedTag)),
    ),
    [items, selectedTag],
  )
  const hasMore = shown < visible.length

  // Re-observed after every batch, so a screen tall enough to show the
  // sentinel again straight away keeps filling until it is not.
  useEffect(() => {
    const el = sentinel.current
    if (!hasMore || !el) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setShown((count) => count + EVENT_PAGE_SIZE)
        }
      },
      { rootMargin: '1500px 0px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [hasMore, shown])

  const photos = useMemo(() => visible.slice(0, shown).map(toGalleryPhoto), [visible, shown])

  return (
    <section className="album-section">
      <div className="album-section__header">
        <div>
          <h3>{section.title}</h3>
          {section.description && <p>{section.description}</p>}
        </div>
        <span className="section-count">
          {selectedTag && items
            ? `${visible.length} of ${section.renderableItemCount} with “${selectedTag}”`
            : `${section.renderableItemCount} photo${section.renderableItemCount === 1 ? '' : 's'}`}
        </span>
      </div>

      {failed ? (
        <div className="empty-state">
          <span>
            Could not load this event's photos.{' '}
            <button type="button" className="link-button" onClick={onRetry}>Try again</button>
          </span>
        </div>
      ) : items === null ? (
        <div className="empty-state">Loading photos…</div>
      ) : visible.length === 0 ? (
        <div className="empty-state">
          {/* An event with no photos at all is publishable, so the two reasons
              a grid is empty are different facts and must not share one
              sentence. */}
          {section.itemCount === 0
            ? 'This event has no photos yet.'
            : 'No photos match the current filter.'}
        </div>
      ) : (
        <>
          <RowsPhotoAlbum
            photos={photos}
            render={{ photo: renderPhoto }}
            spacing={2}
            padding={0}
            targetRowHeight={200}
          />
          {hasMore && (
            <div ref={sentinel} className="event-more">
              <button
                type="button"
                className="link-button"
                onClick={() => setShown((count) => count + EVENT_PAGE_SIZE)}
              >
                Show more ({visible.length - shown} left)
              </button>
            </div>
          )}
        </>
      )}
    </section>
  )
}

/**
 * The password form.
 *
 * A form rather than nginx basic auth, because the browser's own dialog cannot
 * be styled or explained, has no sign-out, and does not grow into the
 * per-deployment users this is meant to become.
 *
 * All it knows is the deployment's NAME. Not the album list, not a photo count:
 * the server refuses those until the cookie is set, so there is nothing here to
 * leak even if this component were wrong.
 */
function LockScreen({ name, onUnlocked }: { name: string; onUnlocked: () => void }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const response = await fetch(asset('login'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      })
      if (response.ok) {
        onUnlocked()
        return
      }
      const payload = await response.json().catch(() => ({}))
      setError(payload.error ?? 'That password is not right.')
    } catch {
      setError('Could not reach the gallery. Try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="lock-shell">
      <form className="lock-card" onSubmit={submit}>
        <p className="eyebrow">Private gallery</p>
        <h1>{name}</h1>
        <p className="lock-hint">This gallery is protected. Enter the password to view it.</p>
        <input
          type="password"
          value={password}
          autoFocus
          autoComplete="current-password"
          placeholder="Password"
          onChange={(event) => setPassword(event.target.value)}
        />
        {error && <p className="lock-error">{error}</p>}
        <button type="submit" disabled={busy || password.length === 0}>
          {busy ? 'Checking…' : 'View gallery'}
        </button>
      </form>
    </div>
  )
}

// Whether the left panel is showing. Remembered per browser, because it is a
// viewing preference and not part of the gallery. With nothing remembered it
// starts open on a wide screen and closed on a narrow one, where the panel
// stacks above the photos and would push them off the first screen.
const SIDEBAR_KEY = 'album-gallery.sidebar-open'

function readSidebarOpen(): boolean {
  try {
    const saved = localStorage.getItem(SIDEBAR_KEY)
    if (saved !== null) return saved === '1'
  } catch {
    // Private windows and blocked site data land here; fall through.
  }
  return !window.matchMedia('(max-width: 960px)').matches
}

function App() {
  const [albums, setAlbums] = useState<AlbumRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [deployment, setDeployment] = useState<DeploymentMeta | null>(null)
  const [selectedTag, setSelectedTag] = useState<string | null>(null)
  const [openVideo, setOpenVideo] = useState<GalleryPhoto | null>(null)
  // What the page shows is the URL hash (see parseRoute), so an event has an
  // address and Back works. Navigation only ever sets the hash; this follows it.
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash))
  // Format-2 events' photos, fetched when an event is first opened and kept
  // for the rest of the visit. Keyed by section record id.
  const [loadedItems, setLoadedItems] = useState<Record<string, AlbumMediaRecord[]>>({})
  const [failedSections, setFailedSections] = useState<Set<string>>(() => new Set())
  const [sidebarOpen, setSidebarOpen] = useState(readSidebarOpen)
  const sidebarRef = React.useRef<HTMLElement>(null)

  // The lightbox's details panel. Open or closed is remembered, like the
  // sidebar: someone who wants the details wants them on every photo.
  const [photoInfoOpen, setPhotoInfoOpen] = useState(readPhotoInfoOpen)
  const [photoInfoHost, setPhotoInfoHost] = useState<HTMLElement | null>(null)
  const [slideId, setSlideId] = useState<string | null>(null)
  const photoInfoButton = React.useRef<HTMLElement | null>(null)
  const photoInfoOpenRef = React.useRef(photoInfoOpen)

  // The closed strip is laid over the top of the panel, so a panel left
  // scrolled down would leave the strip - and the menu button - scrolled away.
  useEffect(() => {
    if (!sidebarOpen && sidebarRef.current) sidebarRef.current.scrollTop = 0
  }, [sidebarOpen])

  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_KEY, sidebarOpen ? '1' : '0')
    } catch {
      // Blocked storage only means the choice is not remembered next visit.
    }
  }, [sidebarOpen])

  // One boot sequence for both shapes this app is served in. deployment.json is
  // absent in legacy single-gallery mode, and its absence simply means "no lock
  // and no deployment name" rather than an error.
  const boot = React.useCallback(() => {
    setLoading(true)
    fetchDeployment().then((meta) => {
      setDeployment(meta)
      if (meta?.locked) {
        setAlbums([])
        setLoading(false)
        return
      }
      loadAlbums().then((loaded) => {
        setAlbums(loaded)
        setLoading(false)
      })
    })
  }, [])

  useEffect(() => { boot() }, [boot])

  useEffect(() => {
    const onHashChange = () => setRoute(parseRoute(window.location.hash))
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  // A new page starts at its top. Not on the first render, which may be a
  // reload the browser is restoring a scroll position for.
  const firstRoute = React.useRef(true)
  useEffect(() => {
    if (firstRoute.current) { firstRoute.current = false; return }
    window.scrollTo({ top: 0 })
  }, [route])

  const navigate = React.useCallback((href: string) => {
    if (window.location.hash !== href) window.location.hash = href
  }, [])

  // An album id the deployment does not have - a stale bookmark - lands on the
  // first album rather than on nothing.
  const selectedAlbum = useMemo(
    () => albums.find((album) => album.id === route.albumId) ?? albums[0] ?? null,
    [albums, route.albumId],
  )
  const routeIsForAlbum = selectedAlbum !== null && route.albumId === selectedAlbum.id

  // id -> node, for resolving the selection without
  // walking the tree on every render.
  const nodesById = useMemo(() => {
    const map = new Map<string, AlbumCategoryNode>()
    const walk = (node: AlbumCategoryNode) => {
      map.set(node.id, node)
      node.children.forEach(walk)
    }
    if (selectedAlbum) walk(selectedAlbum.categoryTree)
    return map
  }, [selectedAlbum])

  // The node that holds each section, so an event page selects its category.
  const nodeOfSection = useMemo(() => {
    const map = new Map<string, AlbumCategoryNode>()
    nodesById.forEach((node) => node.sections.forEach((section) => map.set(section.id, node)))
    return map
  }, [nodesById])

  // The event on screen, if any.
  //
  // A page never comes up empty. The album or category the address names is
  // shown as cards when it has events of its own; when it has none - a year
  // album holds its events in months, so its root has none - it opens the
  // first event beneath it, in the order the sidebar lists them. One holding
  // exactly one event and nothing else opens that event, since a page with a
  // single card on it would only be a click in the way. An event that is no
  // longer published falls back the same way, from the album root.
  const currentSection = useMemo(() => {
    if (!selectedAlbum) return null
    if (routeIsForAlbum && route.kind === 'event') {
      const named = selectedAlbum.sections.find((section) => section.sourceId === route.sectionId)
      if (named) return named
    }
    const node = (routeIsForAlbum && route.kind === 'category'
      ? nodesById.get(`${selectedAlbum.id}-cat-${route.categoryId}`)
      : undefined) ?? selectedAlbum.categoryTree
    if (node.sections.length === 1 && node.children.length === 0) return node.sections[0]
    if (node.sections.length === 0) return firstSectionUnder(node)
    return null
  }, [selectedAlbum, route, routeIsForAlbum, nodesById])

  // Falls back to the root, which also covers a category or event that is no
  // longer published.
  const selectedNode = useMemo(() => {
    if (!selectedAlbum) return null
    if (currentSection) return nodeOfSection.get(currentSection.id) ?? selectedAlbum.categoryTree
    if (routeIsForAlbum && route.kind === 'category') {
      return nodesById.get(`${selectedAlbum.id}-cat-${route.categoryId}`) ?? selectedAlbum.categoryTree
    }
    return selectedAlbum.categoryTree
  }, [selectedAlbum, currentSection, nodeOfSection, nodesById, route, routeIsForAlbum])

  const itemsOf = React.useCallback(
    (section: AlbumSectionRecord) => section.items ?? loadedItems[section.id] ?? null,
    [loadedItems],
  )
  const currentItems = currentSection ? itemsOf(currentSection) : null
  const currentFailed = currentSection ? failedSections.has(currentSection.id) : false

  // Fetch the open event's photos the first time it is opened.
  useEffect(() => {
    if (!selectedAlbum || !currentSection || currentItems || currentFailed) return
    let cancelled = false
    const section = currentSection
    loadSectionItems(selectedAlbum.id, section)
      .then((items) => {
        if (!cancelled) setLoadedItems((prev) => ({ ...prev, [section.id]: items }))
      })
      .catch(() => {
        if (!cancelled) setFailedSections((prev) => new Set(prev).add(section.id))
      })
    return () => { cancelled = true }
  }, [selectedAlbum, currentSection, currentItems, currentFailed])

  const retryCurrent = React.useCallback(() => {
    if (!currentSection) return
    const id = currentSection.id
    setFailedSections((prev) => {
      const next = new Set(prev)
      next.delete(id)
      return next
    })
  }, [currentSection])

  const renderPhoto = useMemo(() => makeRenderPhoto(setOpenVideo), [])

  // Every LOADED photo by its id, which is also the lightbox slide's pid.
  // Across albums rather than the selected one, so the panel can never be left
  // holding an id it cannot resolve.
  const photoInfoById = useMemo(() => {
    const byId = new Map<string, PhotoInfo>()
    for (const album of albums) {
      for (const section of album.sections) {
        const items = section.items ?? loadedItems[section.id]
        items?.forEach((item, index) => {
          byId.set(item.id, { item, section, album, index })
        })
      }
    }
    return byId
  }, [albums, loadedItems])

  useEffect(() => {
    photoInfoOpenRef.current = photoInfoOpen
    try {
      localStorage.setItem(PHOTO_INFO_KEY, photoInfoOpen ? '1' : '0')
    } catch {
      // Not remembered next visit; nothing else depends on it.
    }
    photoInfoButton.current?.setAttribute('aria-pressed', String(photoInfoOpen))
    // On the lightbox root, so its own controls can make room: the next arrow
    // would otherwise sit under the panel.
    photoInfoHost?.closest('.pswp')?.classList.toggle('pswp--info-open', photoInfoOpen)
  }, [photoInfoOpen, photoInfoHost])

  // Registered with PhotoSwipe each time it opens. Stable, because the
  // lightbox is rebuilt whenever this list changes identity.
  const lightboxUi = useMemo<UIElementData[]>(() => [
    {
      name: 'info-button',
      order: 15, // between zoom (10) and close (20)
      isButton: true,
      html: INFO_ICON,
      title: 'Photo details (i)',
      ariaLabel: 'Photo details',
      onInit: (el) => {
        photoInfoButton.current = el
        el.setAttribute('aria-pressed', String(photoInfoOpenRef.current))
      },
      onClick: () => setPhotoInfoOpen((open) => !open),
    },
    {
      name: 'info-panel',
      appendTo: 'root',
      onInit: (el, pswp) => {
        el.closest('.pswp')?.classList.toggle('pswp--info-open', photoInfoOpenRef.current)
        setPhotoInfoHost(el)
        const sync = () => {
          const pid = pswp.currSlide?.data.pid ?? pswp.getItemData(pswp.currIndex).pid
          setSlideId(pid != null ? String(pid) : null)
        }
        sync()
        pswp.on('change', sync)
        pswp.on('keydown', (e) => {
          const key = e.originalEvent
          if (key.key === 'i' && !key.metaKey && !key.ctrlKey && !key.altKey) {
            e.preventDefault()
            setPhotoInfoOpen((open) => !open)
          }
        })
        pswp.on('destroy', () => {
          photoInfoButton.current = null
          setPhotoInfoHost(null)
          setSlideId(null)
        })
      },
    },
  ], [])

  const selectAlbum = React.useCallback((album: AlbumRecord) => {
    setSelectedTag(null)
    navigate(albumHref(album.id))
  }, [navigate])

  const selectCategory = React.useCallback((node: AlbumCategoryNode) => {
    if (selectedAlbum) navigate(categoryHref(selectedAlbum.id, node))
  }, [navigate, selectedAlbum])

  const openSection = React.useCallback((section: AlbumSectionRecord) => {
    if (selectedAlbum) navigate(eventHref(selectedAlbum.id, section))
  }, [navigate, selectedAlbum])

  if (loading) {
    return <div className="empty-shell">Loading albums…</div>
  }

  if (deployment?.locked) {
    return <LockScreen name={deployment.name} onUnlocked={boot} />
  }

  if (!selectedAlbum) {
    return <div className="empty-shell">No albums available.</div>
  }

  return (
    <div className={sidebarOpen ? 'app-shell' : 'app-shell app-shell--collapsed'}>
      <aside className="sidebar" ref={sidebarRef}>
        {/* The button stays when the panel closes: the panel narrows to a
            strip holding just this, so the way back is where it went. */}
        <div className="sidebar__toolbar">
          <button
            type="button"
            className="menu-toggle"
            aria-controls="gallery-sidebar"
            aria-expanded={sidebarOpen}
            aria-label={sidebarOpen ? 'Hide menu' : 'Show menu'}
            title={sidebarOpen ? 'Hide menu' : 'Show menu'}
            onClick={() => setSidebarOpen((open) => !open)}
          >
            <span className="menu-toggle__bars" aria-hidden="true" />
          </button>
          {/* The deployment is what was SHARED, so it is what the page is
              called. The selected album keeps its own heading in the content
              area. Falls back to the album when there is no deployment.json,
              which is legacy single-gallery mode. */}
          <h1 className="sidebar__title">{deployment?.name ?? selectedAlbum.title}</h1>
        </div>

        <SidebarRail
          albums={albums}
          selectedAlbum={selectedAlbum}
          selectedNodeId={selectedNode?.id ?? null}
          currentSectionId={currentSection?.id ?? null}
          inert={sidebarOpen}
          onSelectAlbum={selectAlbum}
          onOpenSection={openSection}
        />

        {/* inert rather than unmounted, so the fade-out has something to
            fade - and while closed nothing in it can take focus. */}
        <div className="sidebar__body" id="gallery-sidebar" inert={!sidebarOpen}>
          <div className="sidebar__header">
            <p>
              {deployment?.description
                ?? selectedAlbum.description
                ?? 'A dedicated album viewer for curated photo collections.'}
            </p>
          </div>

          <section className="sidebar__section">
            <h2>Albums</h2>
            <div className="album-list">
              {albums.map((album) => (
                <button
                  key={album.id}
                  type="button"
                  className={album.id === selectedAlbum.id ? 'album-pill album-pill--active' : 'album-pill'}
                  onClick={() => selectAlbum(album)}
                >
                  <Thumb src={album.coverThumbUrl} name={album.title} className="thumb album-pill__thumb" />
                  <span className="album-pill__title">{album.title}</span>
                  <small>
                    {album.categoryCount > 0 && `${album.categoryCount} categories · `}
                    {album.sections.length} event{album.sections.length === 1 ? '' : 's'}
                  </small>
                </button>
              ))}
            </div>
          </section>

          <section className="sidebar__section">
            {/* With no categories the tree is one root holding every section, so
                this renders the same flat list of links it always did. */}
            <h2>{selectedAlbum.categoryCount > 0 ? 'Contents' : 'Sections'}</h2>
            <nav aria-label="Album contents">
              <CategoryTree
                // A different album is a different tree, and its collapsed
                // state should not carry over.
                key={selectedAlbum.id}
                albumId={selectedAlbum.id}
                root={selectedAlbum.categoryTree}
                selectedId={selectedNode?.id ?? selectedAlbum.categoryTree.id}
                currentSectionId={currentSection?.id ?? null}
                onSelect={selectCategory}
              />
            </nav>
          </section>

          <section className="sidebar__section">
            <h2>Tags</h2>
            {selectedAlbum.availableTags.length === 0 ? (
              <p className="empty-note">No tags available in the current sample albums.</p>
            ) : (
              <div className="tag-list">
                <button
                  type="button"
                  className={selectedTag === null ? 'tag-chip tag-chip--active' : 'tag-chip'}
                  onClick={() => setSelectedTag(null)}
                >
                  All photos
                </button>
                {selectedAlbum.availableTags.map((tag) => (
                  <button
                    key={tag}
                    type="button"
                    className={selectedTag === tag ? 'tag-chip tag-chip--active' : 'tag-chip'}
                    onClick={() => setSelectedTag(tag)}
                  >
                    {tag}
                  </button>
                ))}
              </div>
            )}
          </section>
        </div>
      </aside>

      <main className="content">
        {currentSection ? (
          // One Gallery per event, so the lightbox steps through this event
          // and stops at its edges.
          <Gallery uiElements={lightboxUi}>
            <EventView
              // A new event starts drawing from its first batch again.
              key={currentSection.id}
              section={currentSection}
              items={currentItems}
              failed={currentFailed}
              selectedTag={selectedTag}
              renderPhoto={renderPhoto}
              onRetry={retryCurrent}
            />
          </Gallery>
        ) : selectedNode && (
          <CategoryView
            album={selectedAlbum}
            node={selectedNode}
            selectedTag={selectedTag}
            onSelect={selectCategory}
          />
        )}

        {photoInfoHost && createPortal(
          <PhotoInfoPanel
            info={slideId ? photoInfoById.get(slideId) ?? null : null}
            open={photoInfoOpen}
          />,
          photoInfoHost,
        )}

        {openVideo && (
          <VideoOverlay photo={openVideo} onClose={() => setOpenVideo(null)} />
        )}
      </main>
    </div>
  )
}

export default App
