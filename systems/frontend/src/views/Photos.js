import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CContainer,
  CButton,
  CRow,
  CCol,
  CModal,
  CModalHeader,
  CModalTitle,
  CModalBody,
  CModalFooter,
  CForm,
  CFormInput,
  CFormLabel,
  CFormTextarea,
  CSpinner,
  CAlert,
  CBadge,
  CListGroup,
  CListGroupItem,
  CNav,
  CNavItem,
  CNavLink,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilPlus, cilFace, cilCalendar, cilLocationPin, cilCamera, cilPeople, cilPencil, cilCheck, cilLayers, cilPaw } from '@coreui/icons';
import api, { faceDetectionAPI, photoAPI, petAPI, peopleCategoryAPI, photoEditAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import { MONTH_NAMES } from '../utils/dateGrouping';
import { categoryOf, categoryTabs, countByCategory, openingTab } from '../utils/peopleCategories';
import { usePhotoScope } from '../hooks/usePhotoScope';
import { getTimezoneOptions, getBrowserTimezone } from '../utils/timezones';
import { applyTool, cssTransform, delta as orientationDelta, swaps, summary as editSummary } from '../utils/orientation';
import { bumpEditVersion, editVersionOf } from '../utils/editVersions';
import BasicEditorTools from '../components/BasicEditorTools';
import { API_HOST, VISION_HOST, PEOPLE_PHOTOS_BASE_URL, galleryThumbUrl } from '../config';
import { Filmstrip } from '../components/Filmstrip';
import CatalogFileCard from '../components/CatalogFileCard';

// ─── Date helpers (same as PhotoDetail) ──────────────────────────────────────

function formatDate(dt) {
  if (!dt) return null;
  try {
    const d = new Date(dt);
    if (isNaN(d.getTime())) return dt;
    return d.toLocaleString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
  } catch { return dt; }
}

function formatDateTime(dt) {
  if (!dt) return '';
  try {
    const d = new Date(dt);
    if (isNaN(d.getTime())) return dt;
    const day = String(d.getDate()).padStart(2, '0');
    const mon = d.toLocaleString('en-US', { month: 'short' });
    const year = d.getFullYear();
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    return `${day} ${mon} ${year} ${hh}:${mm}`;
  } catch { return dt; }
}

function formatFileSize(bytes) {
  if (!bytes) return null;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  return `${(mb / 1024).toFixed(2)} GB`;
}


// ?date=YYYY-MM-DD -> {y, m (1-based), d}, or null.
export const dateParam = (search) => {
  const raw = new URLSearchParams(search || '').get('date') || '';
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
  return m ? { y: Number(m[1]), m: Number(m[2]), d: Number(m[3]) } : null;
};

const Photos = () => {
  const location = useLocation();
  const { catalogId: urlCatalogId, fileId: urlFileId } = useParams();
  const { activeCatalog } = useCatalog();
  const routerNavigate = useNavigate();
  // Navigation that only happens while this page is on screen. Opening a day
  // is several requests and a second or more on a large catalog, and each
  // load ends by putting its photo in the URL. Leave Photos before that and
  // the unmounted page used to finish anyway and navigate - pulling the user
  // back from wherever they had gone, every time, since coming back started
  // another load. Every navigate here goes through this.
  const onScreen = useRef(true);
  useEffect(() => {
    onScreen.current = true;
    return () => { onScreen.current = false; };
  }, []);
  const navigate = useCallback((...args) => {
    if (onScreen.current) routerNavigate(...args);
  }, [routerNavigate]);
  const [people, setPeople] = useState([]);
  const [error, setError] = useState(null);
  // When navigated with a specific file to show (via URL params or location state)
  const pendingFileIdRef = useRef(
    urlFileId ? Number(urlFileId) : (location.state?.fileId ?? null)
  );
  
  // Photo tagging state
  const [taggingCatalog, setTaggingCatalog] = useState(null);
  // The day, month, year or catalog being browsed, fetched a page at a time
  // (hooks/usePhotoScope). `catalogImages` is that scope: as long as it, with
  // holes where nothing has been fetched yet, indexed by position in it.
  const scope = usePhotoScope();
  const catalogImages = scope.items;
  const [currentImageIndex, setCurrentImageIndex] = useState(0);
  const [imagesLoading, setImagesLoading] = useState(false);
  const [availableYearsMonths, setAvailableYearsMonths] = useState({});
  const [availableYearsMonthsDays, setAvailableYearsMonthsDays] = useState({});
  const [selectedYear, setSelectedYear] = useState(null);
  const [selectedMonth, setSelectedMonth] = useState(null);
  const [selectedDay, setSelectedDay] = useState(null);
  const [yearsMonthsLoading, setYearsMonthsLoading] = useState(false);
  // Slider active photo
  const [activePhotoId, setActivePhotoId] = useState(null);
  
  // Face detection state
  const [detectedFaces, setDetectedFaces] = useState([]);
  // Animals in this photograph. Drawn on the SAME canvas as the faces, so the
  // draw function reads them from a ref rather than taking another argument -
  // it is called from fifteen places and every one of them is about faces.
  const [detectedPets, setDetectedPets] = useState([]);
  const detectedPetsRef = useRef([]);
  const [detectingFaces, setDetectingFaces] = useState(false);
  const [selectedFace, setSelectedFace] = useState(null);
  const [showTagModal, setShowTagModal] = useState(false);
  const [showAddPersonModal, setShowAddPersonModal] = useState(false);
  const [newPersonName, setNewPersonName] = useState('');
  const [peopleCategories, setPeopleCategories] = useState([]);
  const [tagCategory, setTagCategory] = useState('Family');   // the picker's open tab
  const [showSidePanel, setShowSidePanel] = useState(false);
  const [hoveredFaceId, setHoveredFaceId] = useState(null);
  const [hoveredRemoveTagId, setHoveredRemoveTagId] = useState(null);
  
  // AI Tagging state - for displaying person's face images
  const [selectedPersonForAI, setSelectedPersonForAI] = useState(null);
  const [aiTaggingYear, setAITaggingYear] = useState(null);
  const [aiTaggingMonth, setAITaggingMonth] = useState(null);
  const [personFaceImages, setPersonFaceImages] = useState([]);
  const [loadingFaceImages, setLoadingFaceImages] = useState(false);
  const [availableAIYears, setAvailableAIYears] = useState([]);
  
  // Photo detail (EXIF + events + locations) for the current image
  const [photoDetail, setPhotoDetail] = useState(null);

  // Description editing state
  const [imageDescription, setImageDescription] = useState('');
  const [isEditingDescription, setIsEditingDescription] = useState(false);
  const [savingDescription, setSavingDescription] = useState(false);
  const [generatingDescription, setGeneratingDescription] = useState(false);

  // Date editing state
  const [editingDate, setEditingDate]   = useState(false);
  const [dateInput,   setDateInput]     = useState('');
  const [timeInput,   setTimeInput]     = useState('');
  const [tzInput,     setTzInput]       = useState(getBrowserTimezone);
  const [dateSaving,  setDateSaving]    = useState(false);
  const [dateError,   setDateError]     = useState(null);
  const [dateSuccess, setDateSuccess]   = useState(null);
  const [removeNotice, setRemoveNotice] = useState(null);

  // Basic Editor (documentation/specs/basic-photo-editing.md section 3).
  // editingPhoto: the page is in edit mode. pendingOrientation: what the
  // preview shows, saved only on Save. thumbTick re-renders the image and the
  // filmstrip with their new versions once an edit's rebuild is done.
  const [editingPhoto, setEditingPhoto] = useState(false);
  const [pendingOrientation, setPendingOrientation] = useState(1);
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState(null);
  const [editNotice, setEditNotice] = useState(null);
  const [, setThumbTick] = useState(0);

  // Manual drawing state
  const [isDrawing, setIsDrawing] = useState(false);
  const [drawStart, setDrawStart] = useState(null);
  const [drawPreview, setDrawPreview] = useState(null);
  const imageRef = useRef(null);
  const canvasRef = useRef(null);
  const containerRef = useRef(null);
  const removeTagAreasRef = useRef([]);
  const isDrawingRef = useRef(false);
  const drawStartRef = useRef(null);


  // Initialise with activeCatalog when it becomes available
  useEffect(() => {
    if (!activeCatalog) {
      setTaggingCatalog(null);
      scope.reset();
      return;
    }
    const targetCatalogId = activeCatalog.id;
    // A file id means nothing outside the catalog it came from: each catalog
    // numbers its files from 1. The URL effect below re-stores the linked id
    // while the first load is in flight, so without this check switching
    // catalogs later opened whichever photo in the NEW catalog happened to
    // share the old one's id. No catalog in the URL (location.state) is
    // trusted as before.
    const targetFileId = !urlCatalogId || String(urlCatalogId) === String(targetCatalogId)
      ? pendingFileIdRef.current : null;
    pendingFileIdRef.current = null;
    // /photos?date=YYYY-MM-DD - a day from Calendar view or the Events day view
    // (calendar-view-spec.md 6). Only when no photo was asked for.
    const targetDate = targetFileId ? null : dateParam(location.search);
    setTaggingCatalog(targetCatalogId);
    setDetectedFaces([]);
    clearPets();
    setShowSidePanel(true);
    setSelectedYear(null);
    setSelectedMonth(null);
    setSelectedDay(null);
    setActivePhotoId(null);
    setAvailableYearsMonths({});
    setAvailableYearsMonthsDays({});
    loadAllPhotosAndGroup(targetCatalogId, { targetFileId, targetDate });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeCatalog]);

  // When the URL fileId param changes while the component is already mounted.
  // (e.g. navigated here from another page without a remount, or user clicks a
  // photo in EventDetail while Photos is already open on a different day.)
  useEffect(() => {
    if (!urlFileId) return;
    const targetId = Number(urlFileId);

    // Photos data not yet loaded — store for the activeCatalog effect to pick up
    if (!scope.loaded) {
      pendingFileIdRef.current = targetId;
      return;
    }

    // Helper: reset photo-level state when switching images
    const resetPhotoState = () => {
      setDetectedFaces([]);
      clearPets();
      setSelectedFace(null);
      setHoveredFaceId(null);
      setPhotoDetail(null);
      setImageDescription('');
      setIsEditingDescription(false);
    };

    // Check if the target photo is in the scope on screen. By the position
    // map, not a search: under "All" the scope is the whole catalog.
    const idxInCurrent = scope.indexOf(targetId);
    if (idxInCurrent !== -1) {
      if (idxInCurrent !== currentImageIndex) {
        setCurrentImageIndex(idxInCurrent);
        resetPhotoState();
      }
      setActivePhotoId(targetId);
      return;
    }

    // Not in the part of the scope we have loaded - which is the ORDINARY
    // case, because only one scope is in hand and only the pages of it that
    // have been looked at. Ask the server which day it is on and go there.
    //
    // Before the gallery loaded lazily, falling through here meant the photo
    // did not exist and doing nothing was right. Now it usually means we
    // simply have not fetched its day, and doing nothing would be a link from
    // Events or Identify People that silently does not move.
    (async () => {
      if (!activeCatalog) return;
      try {
        const loc = await photoAPI.dayOf(activeCatalog.id, targetId);
        const year = loc.data.year;
        const month = calMonthToUi(loc.data.month);
        const day = loc.data.day;
        const opened = await openScope(activeCatalog.id, year, month, day,
                                       { around: targetId });
        if (!opened) return;                 // superseded by a later choice
        setSelectedYear(year);
        setSelectedMonth(month);
        setSelectedDay(day);
        setCurrentImageIndex(opened.position ?? 0);
        setActivePhotoId(targetId);
        resetPhotoState();
      } catch {
        // No such photo, or it has no date. Leaving the page where it is
        // beats clearing it over a bad link.
      }
    })();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlFileId]);

  const loadPeople = async () => {
    if (!taggingCatalog) return;
    
    try {
      const [response, categories] = await Promise.all([
        api.get(`/catalogs/${taggingCatalog}/people`),
        // The Tag Face picker's tabs. A failure leaves them to be derived
        // from the people alone rather than losing the picker.
        peopleCategoryAPI.getAll(taggingCatalog).catch(() => ({ data: [] })),
      ]);
      setPeople(response.data);
      setPeopleCategories(categories.data || []);
    } catch (err) {
      console.error('Failed to load people:', err);
      setError('Failed to load people');
    }
  };

  // ─── The Tag Face picker's tabs: one per people category ─────────────────
  // Family first and where it opens - see utils/peopleCategories.
  const tagTabs = categoryTabs(peopleCategories, people);
  const tagTabCounts = countByCategory(people);
  const peopleInTagTab = people.filter((p) => categoryOf(p) === tagCategory);

  // Every time the picker opens, it opens on Family.
  useEffect(() => {
    if (showTagModal) setTagCategory(openingTab(tagTabs));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showTagModal]);

  useEffect(() => {
    if (taggingCatalog) {
      loadPeople();
    }
  }, [taggingCatalog]);

  // ─── Load all photos from the catalog, group by year/month/day, and auto-select ─

  // ─── One day at a time ────────────────────────────────────────────────
  //
  // This used to fetch the whole catalog. On `family` that is 301,195 rows and
  // 190 MB, about 21 s to build before the browser had parsed a byte, which is
  // why the page "sometimes never finishes loading".
  //
  // It fetched everything to answer two unrelated questions: which days exist,
  // to draw the navigation, and what is in the one day being looked at. The
  // first is now /photos/calendar - 6,134 rows and 154 KB on the same catalog.
  // The second is /photos?year=&month=&day=, tens to a few thousand rows.
  //
  // And the day, month or year itself is now fetched a page at a time
  // (hooks/usePhotoScope): "All" of 2022 is 29,680 photographs, and the strip
  // under the viewer draws a dozen.

  // THE CALENDAR'S MONTHS ARE 1-BASED; THE UI'S ARE 0-BASED, because
  // groupByYearMonthDay() uses Date.getMonth(). Getting this wrong does not
  // throw - every day simply opens empty, which looks like missing data rather
  // than an off-by-one.
  const calMonthToUi = (m) => m - 1;

  // A new scope, opened on its first page - or, with `around`, on the page
  // holding that photograph, whose position comes back with it. Null when a
  // later choice superseded this one before it arrived.
  const openScope = async (catalogId, year, month, day, { around = null } = {}) => {
    const params = {};
    if (year != null && year !== 'Unknown') params.year = year;
    if (month != null && month >= 0) params.month = month + 1;   // back to 1-based
    if (day != null && day >= 0) params.day = day;
    return scope.open(catalogId, params, { around });
  };

  const loadAllPhotosAndGroup = async (catalogId, { targetFileId = null, targetDate = null } = {}) => {
    if (!catalogId) return;
    setYearsMonthsLoading(true);
    setImagesLoading(true);
    setError(null);
    try {
      const cal = await photoAPI.calendar(catalogId);
      const days = cal.data?.days ?? [];

      // The navigation, from counts alone - no photographs needed.
      const ymd = {};
      const ym = {};
      for (const [y, m, d] of days) {
        const yr = String(y);
        const mo = calMonthToUi(m);
        ymd[yr] = ymd[yr] ?? {};
        ymd[yr][mo] = ymd[yr][mo] ?? [];
        if (!ymd[yr][mo].includes(d)) ymd[yr][mo].push(d);
        ym[yr] = ym[yr] ?? [];
        if (!ym[yr].includes(mo)) ym[yr].push(mo);
      }
      for (const yr of Object.keys(ymd)) {
        ym[yr].sort((x, z) => x - z);
        for (const mo of Object.keys(ymd[yr])) ymd[yr][mo].sort((x, z) => x - z);
      }
      setAvailableYearsMonthsDays(ymd);
      setAvailableYearsMonths(ym);

      if (days.length === 0) {
        scope.reset();
        return;
      }

      // Which day to open. A deep link asks the server which day its photo is
      // on - one row - instead of loading the catalog to find out.
      let targetYear = null;
      let targetMonth = null;
      let targetDay = null;
      if (targetFileId) {
        try {
          const loc = await photoAPI.dayOf(catalogId, targetFileId);
          targetYear = loc.data.year;
          targetMonth = calMonthToUi(loc.data.month);
          targetDay = loc.data.day;
        } catch {
          targetYear = null;      // removed, or undated: fall through
        }
      }
      if (targetYear === null && targetDate) {
        // That day, or the nearest earlier day with photographs (a link made
        // before some were removed). The calendar is newest first.
        const want = targetDate.y * 10000 + targetDate.m * 100 + targetDate.d;
        const hit = days.find(([y, m, d]) => y * 10000 + m * 100 + d <= want);
        if (hit) {
          targetYear = hit[0];
          targetMonth = calMonthToUi(hit[1]);
          targetDay = hit[2];
        }
      }
      if (targetYear === null) {
        const [y, m, d] = days[0];       // the calendar is newest first
        targetYear = y;
        targetMonth = calMonthToUi(m);
        targetDay = d;
      }

      const opened = await openScope(catalogId, targetYear, targetMonth, targetDay,
                                     { around: targetFileId });
      if (!opened) return;                   // superseded by a later choice
      const targetIdx = opened.position ?? 0;
      const dayPhotos = opened.items;

      setSelectedYear(targetYear);
      setSelectedMonth(targetMonth);
      setSelectedDay(targetDay);

      setCurrentImageIndex(targetIdx);
      if (dayPhotos[targetIdx]) {
        setActivePhotoId(dayPhotos[targetIdx].id);
        navigate(`/photos/${catalogId}/${dayPhotos[targetIdx].id}`, { replace: true });
      }
    } catch (err) {
      console.error('Failed to load photos', err);
      setError('Failed to load photos from catalog');
      scope.reset();
    } finally {
      setImagesLoading(false);
      setYearsMonthsLoading(false);
    }
  };

  // Helper function to get the display path for an image
  // Prioritizes alternative_file_path (1200px SCALE version or JPG) over original file_path
  const getImageDisplayPath = (image) => {
    if (!image) return null;
    // Use alternative_file_path if available (this will be the 1200px SCALE version or JPG conversion)
    return image.alternative_file_path || image.file_path;
  };

  const handleTaggingCatalogChange = (catalogId) => {
    setTaggingCatalog(catalogId);
    setDetectedFaces([]);
    clearPets();
    setShowSidePanel(true);
    setSelectedYear(null);
    setSelectedMonth(null);
    setSelectedDay(null);
    setActivePhotoId(null);
    setAvailableYearsMonths({});
    setAvailableYearsMonthsDays({});
    loadAllPhotosAndGroup(catalogId);
  };

  // ─── Year/Month/Day handlers (spec §Navigation Row Changes) ──────────────

  // Show a scope, fetching it first if it is not in hand.
  //
  // Every navigation handler goes through this, so there is one answer to
  // "what happens when the user asks for a day we have not loaded" instead of
  // four. `keepCurrent` is for the All buttons, which widen around the photo
  // being looked at rather than jumping somewhere else.
  const showScope = async (year, month, day, { keepCurrent = false } = {}) => {
    if (!activeCatalog) return [];
    // Widening asks for the page holding the photo on screen, and is told
    // where in the wider scope it sits - nothing before it is fetched.
    const current = keepCurrent ? catalogImages[currentImageIndex] : null;
    let opened;
    setImagesLoading(true);
    try {
      opened = await openScope(activeCatalog.id, year, month, day,
                               { around: current?.id ?? null });
    } catch (err) {
      console.error('Failed to load that day', err);
      setError('Could not load those photos');
      return catalogImages;
    } finally {
      setImagesLoading(false);
    }
    if (!opened) return catalogImages;       // superseded by a later choice
    const list = opened.items;
    setSelectedYear(year);
    setSelectedMonth(month);
    setSelectedDay(day);

    if (keepCurrent) {
      const idx = opened.position ?? -1;
      setCurrentImageIndex(idx >= 0 ? idx : 0);
      if (idx < 0) {
        setDetectedFaces([]);
        clearPets();
        setActivePhotoId(list[0]?.id ?? null);
      }
      return list;
    }

    setDetectedFaces([]);
    clearPets();
    setActivePhotoId(null);
    setCurrentImageIndex(0);
    if (list[0]) {
      navigate(`/photos/${activeCatalog.id}/${list[0].id}`, { replace: true });
    }
    return list;
  };

  // Most recent day of a month, from the calendar rather than from loaded data.
  const latestDayOf = (year, month) =>
    (availableYearsMonthsDays[String(year)]?.[month] ?? [])
      .filter((d) => d !== -1).sort((a, b) => b - a)[0] ?? -1;

  const handleYearChange = async (year) => {
    const months = (availableYearsMonths[String(year)] ?? [])
      .filter((m) => m !== -1).sort((x, z) => z - x);
    const month = months[0] ?? -1;
    await showScope(year, month, latestDayOf(year, month));
  };

  const handleMonthChange = async (month) => {
    await showScope(selectedYear, month, latestDayOf(selectedYear, month));
  };

  // ─── The "All" buttons ──────────────────────────────────────────────────
  // Each clears its own level and everything under it, which widens the strip
  // to that scope: All -> the catalog, All <year> -> the year, All <month> ->
  // the month. Modelled on the Events filter card, with the extra day row this
  // page has.
  //
  // They do not pick a photo. The effect that resolves activePhotoId against
  // the list does that, exactly as it does when a day is chosen, so there is
  // one rule for "the selection must be inside what is on screen".
  //
  // WIDENING KEEPS THE PHOTOGRAPH ON SCREEN. A wider scope always contains the
  // narrower one, so the photo being looked at is still in the list - only its
  // index moves. Jumping to the oldest photo of the new scope would throw away
  // the user's place for no reason, and would clear the faces already detected
  // on it. Picking a year or a day is different: that is a request to go
  // somewhere else, and those handlers still move.
  // WIDENING IS A FETCH OF ONE PAGE: the page of the wider scope that holds
  // the photo on screen, plus its position there. "All" across the catalog
  // was 301,195 rows and 190 MB on `family`; it is now 200 rows, and the rest
  // arrive a page at a time as the strip is scrolled to them.
  const widenTo = (year, month, day) => showScope(year, month, day,
                                                  { keepCurrent: true });

  const showAllYears = () => widenTo(null, null, null);
  const showAllOfYear = () => widenTo(selectedYear, null, null);
  const showAllOfMonth = () => widenTo(selectedYear, selectedMonth, null);

  const handleDayChange = async (day) => {
    await showScope(selectedYear, selectedMonth, day);
  };

  // ─── Slider handlers ──────────────────────────────────────────────────────

  const handleSliderClick = (photo) => {
    setActivePhotoId(photo.id);
    if (activeCatalog) {
      navigate(`/photos/${activeCatalog.id}/${photo.id}`);
    }
  };

  const handleDetectFaces = async () => {
    if (!catalogImages[currentImageIndex]) return;
    
    setDetectingFaces(true);
    setError(null);
    
    try {
      const currentImage = catalogImages[currentImageIndex];
      const response = await faceDetectionAPI.detectFaces(taggingCatalog, currentImage.id);
      
      if (response.data.success) {
        // Scale face coordinates if needed (same logic as loadExistingFaces)
        const scaledFaces = (response.data.faces || []).map(face => {
          // Only scale if we have stored detection dimensions and they differ from display
          if (face.image_width && face.image_height && imageRef.current) {
            const displayWidth = imageRef.current.naturalWidth;
            const displayHeight = imageRef.current.naturalHeight;
            
            console.log('Auto-detect face coordinate scaling check:', {
              faceId: face.id,
              source: face.source,
              storedDimensions: { width: face.image_width, height: face.image_height },
              displayDimensions: { width: displayWidth, height: displayHeight },
              originalCoords: { x: face.x, y: face.y, w: face.w, h: face.h }
            });
            
            // Check if dimensions differ (accounting for potential orientation differences)
            if (face.image_width !== displayWidth || face.image_height !== displayHeight) {
              const scaleX = displayWidth / face.image_width;
              const scaleY = displayHeight / face.image_height;
              
              const scaledFace = {
                ...face,
                x: Math.round(face.x * scaleX),
                y: Math.round(face.y * scaleY),
                w: Math.round(face.w * scaleX),
                h: Math.round(face.h * scaleY)
              };
              
              console.log('Scaled auto-detect face coordinates:', {
                faceId: face.id,
                scale: { x: scaleX, y: scaleY },
                scaledCoords: { x: scaledFace.x, y: scaledFace.y, w: scaledFace.w, h: scaledFace.h }
              });
              
              return scaledFace;
            }
          }
          return face;
        });
        
        setDetectedFaces(scaledFaces);
        setShowSidePanel(true);
        drawFacesOnCanvas(scaledFaces);
      } else {
        setError('Failed to detect faces');
      }
    } catch (err) {
      if (err.response?.status === 503) {
        setError('Face detection service is not available. Please make sure the deepface backend is running.');
      } else {
        setError(`Failed to detect faces: ${err.response?.data?.error || err.message}`);
      }
    } finally {
      setDetectingFaces(false);
    }
  };

  const drawFacesOnCanvas = (faces, preview = null) => {
    if (!imageRef.current || !canvasRef.current) return;
    
    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    const img = imageRef.current;
    
    // Get the actual displayed dimensions of the image
    const displayWidth = img.width;
    const displayHeight = img.height;
    const naturalWidth = img.naturalWidth;
    const naturalHeight = img.naturalHeight;
    
    // Set canvas internal dimensions to match natural image size
    canvas.width = naturalWidth;
    canvas.height = naturalHeight;
    
    // Set canvas CSS dimensions to match displayed image size
    canvas.style.width = `${displayWidth}px`;
    canvas.style.height = `${displayHeight}px`;
    
    // Calculate the offset to center the canvas on the image
    const container = containerRef.current;
    if (container) {
      const containerRect = container.getBoundingClientRect();
      const imgRect = img.getBoundingClientRect();
      const offsetLeft = imgRect.left - containerRect.left;
      const offsetTop = imgRect.top - containerRect.top;
      canvas.style.left = `${offsetLeft}px`;
      canvas.style.top = `${offsetTop}px`;
    }
    
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    removeTagAreasRef.current = [];
    
    faces.forEach((face, index) => {
      const isHovered = hoveredFaceId === face.id;
      const isSelected = selectedFace?.id === face.id;
      const isRemoveTagHovered = hoveredRemoveTagId === face.id;
      
      if (isSelected) {
        ctx.strokeStyle = '#ffc107';
        ctx.lineWidth = 5;
      } else if (isHovered) {
        ctx.strokeStyle = face.person_id ? '#28a745' : '#007bff';
        ctx.lineWidth = 5;
      } else {
        ctx.strokeStyle = face.person_id ? '#28a745' : '#007bff';
        ctx.lineWidth = 3;
      }
      
      ctx.strokeRect(face.x, face.y, face.w, face.h);
      
      const label = face.person_name || `Face ${index + 1}`;
      ctx.font = '16px Arial';
      const textWidth = ctx.measureText(label).width;
      
      if (isSelected) {
        ctx.fillStyle = '#ffc107';
      } else {
        ctx.fillStyle = face.person_id ? '#28a745' : '#007bff';
      }
      
      ctx.fillRect(face.x, face.y - 25, textWidth + 10, 25);
      ctx.fillStyle = '#ffffff';
      ctx.fillText(label, face.x + 5, face.y - 7);
      
      if (face.person_id) {
        const removeLabel = '✕';
        ctx.font = 'bold 16px Arial';
        const removeButtonSize = 20;
        const removeButtonX = face.x + face.w - removeButtonSize;
        const removeButtonY = face.y;
        
        removeTagAreasRef.current.push({
          faceId: face.id,
          x: removeButtonX,
          y: removeButtonY,
          w: removeButtonSize,
          h: removeButtonSize,
        });
        
        if (isRemoveTagHovered) {
          ctx.fillStyle = '#c82333';
        } else {
          ctx.fillStyle = '#dc3545';
        }
        ctx.fillRect(removeButtonX, removeButtonY, removeButtonSize, removeButtonSize);
        
        ctx.fillStyle = '#ffffff';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(removeLabel, removeButtonX + removeButtonSize / 2, removeButtonY + removeButtonSize / 2);
        
        ctx.textAlign = 'left';
        ctx.textBaseline = 'alphabetic';
      }
    });

    // ── Animals, on the same canvas ──────────────────────────────────────
    //
    // After the faces, so a box drawn over a face reads as the later thing
    // rather than being hidden under it. Read from a ref because this
    // function is called from fifteen places that all pass faces and know
    // nothing about pets.
    //
    // A DIFFERENT COLOUR and a dashed line, deliberately: a pet box must not
    // be mistakable for an untagged face, because the two are clicked for
    // completely different reasons and only faces are clickable here.
    detectedPetsRef.current.forEach((pet) => {
      const named = !!pet.person_id;
      ctx.strokeStyle = named ? '#8e44ad' : '#e67e22';
      ctx.lineWidth = 3;
      ctx.setLineDash([10, 5]);
      ctx.strokeRect(pet.x, pet.y, pet.w, pet.h);
      ctx.setLineDash([]);

      const label = pet.person_name
        || `${pet.species || 'animal'} ${Math.round((pet.confidence || 0) * 100)}%`;
      ctx.font = '16px Arial';
      const tw = ctx.measureText(label).width;
      ctx.fillStyle = named ? '#8e44ad' : '#e67e22';
      // Below the box, where a face label is above it, so an animal inside a
      // group shot does not write over the face label of whoever is holding
      // it.
      ctx.fillRect(pet.x, pet.y + pet.h, tw + 10, 24);
      ctx.fillStyle = '#ffffff';
      ctx.fillText(label, pet.x + 5, pet.y + pet.h + 17);
    });

    if (preview && preview.size > 0) {
      ctx.strokeStyle = '#ff6600';
      ctx.lineWidth = 2;
      ctx.setLineDash([8, 4]);
      ctx.strokeRect(preview.x, preview.y, preview.size, preview.size);
      ctx.setLineDash([]);

      ctx.font = '14px Arial';
      const previewLabel = 'New area';
      const previewTextWidth = ctx.measureText(previewLabel).width;
      ctx.fillStyle = '#ff6600';
      ctx.fillRect(preview.x, preview.y - 22, previewTextWidth + 10, 22);
      ctx.fillStyle = '#ffffff';
      ctx.fillText(previewLabel, preview.x + 5, preview.y - 5);
    }
  };

  const getCanvasCoords = (event) => {
    const canvas = canvasRef.current;
    const img = imageRef.current;
    const rect = canvas.getBoundingClientRect();
    const scaleX = img.naturalWidth / rect.width;
    const scaleY = img.naturalHeight / rect.height;
    return {
      x: (event.clientX - rect.left) * scaleX,
      y: (event.clientY - rect.top) * scaleY,
    };
  };

  const pointInAnyFace = (x, y, faces) => {
    for (const face of faces) {
      if (x >= face.x && x <= face.x + face.w && y >= face.y && y <= face.y + face.h) {
        return face;
      }
    }
    return null;
  };

  const handleCanvasMouseDown = (event) => {
    if (!imageRef.current || !canvasRef.current) return;

    const { x, y } = getCanvasCoords(event);

    for (const area of removeTagAreasRef.current) {
      if (x >= area.x && x <= area.x + area.w && y >= area.y && y <= area.y + area.h) {
        const face = detectedFaces.find(f => f.id === area.faceId);
        if (face) {
          handleRemoveTag(face);
          return;
        }
      }
    }

    // Clicking a box is asking to say who it is, so the person picker opens at
    // once - it used to take a second click, on Tag Face in the side panel.
    // The face stays selected, so Remove Tag and Delete Area are still there
    // behind the picker for the other things a click on a box can mean.
    const clickedFace = pointInAnyFace(x, y, detectedFaces);
    if (clickedFace) {
      setSelectedFace(clickedFace);
      drawFacesOnCanvas(detectedFaces);
      setShowTagModal(true);
      return;
    }

    isDrawingRef.current = true;
    drawStartRef.current = { x, y };
    setIsDrawing(true);
    setDrawStart({ x, y });
    setDrawPreview(null);
    setSelectedFace(null);
    event.preventDefault();
  };

  const handleCanvasMouseMove = (event) => {
    if (!imageRef.current || !canvasRef.current) return;

    const { x, y } = getCanvasCoords(event);
    const canvas = canvasRef.current;

    if (isDrawingRef.current && drawStartRef.current) {
      const dx = x - drawStartRef.current.x;
      const dy = y - drawStartRef.current.y;
      const size = Math.max(Math.abs(dx), Math.abs(dy));
      const previewX = dx >= 0 ? drawStartRef.current.x : drawStartRef.current.x - size;
      const previewY = dy >= 0 ? drawStartRef.current.y : drawStartRef.current.y - size;
      const preview = { x: previewX, y: previewY, size };
      setDrawPreview(preview);
      drawFacesOnCanvas(detectedFaces, preview);
      canvas.style.cursor = 'crosshair';
      return;
    }

    if (detectedFaces.length === 0) {
      canvas.style.cursor = 'crosshair';
      return;
    }

    let foundRemoveTag = null;
    for (const area of removeTagAreasRef.current) {
      if (x >= area.x && x <= area.x + area.w && y >= area.y && y <= area.y + area.h) {
        foundRemoveTag = area.faceId;
        break;
      }
    }

    if (foundRemoveTag) {
      if (hoveredRemoveTagId !== foundRemoveTag) {
        setHoveredRemoveTagId(foundRemoveTag);
        setHoveredFaceId(null);
        canvas.style.cursor = 'pointer';
        drawFacesOnCanvas(detectedFaces);
      }
      return;
    } else if (hoveredRemoveTagId !== null) {
      setHoveredRemoveTagId(null);
      drawFacesOnCanvas(detectedFaces);
    }

    const foundFace = pointInAnyFace(x, y, detectedFaces);

    if (foundFace) {
      if (hoveredFaceId !== foundFace.id) {
        setHoveredFaceId(foundFace.id);
        canvas.style.cursor = 'pointer';
        drawFacesOnCanvas(detectedFaces);
      }
    } else {
      if (hoveredFaceId !== null) {
        setHoveredFaceId(null);
        drawFacesOnCanvas(detectedFaces);
      }
      canvas.style.cursor = 'crosshair';
    }
  };

  const handleCanvasMouseUp = async (event) => {
    if (!isDrawingRef.current || !drawStartRef.current) return;

    const { x, y } = getCanvasCoords(event);
    const startX = drawStartRef.current.x;
    const startY = drawStartRef.current.y;
    const dx = x - startX;
    const dy = y - startY;
    const size = Math.max(Math.abs(dx), Math.abs(dy));

    isDrawingRef.current = false;
    drawStartRef.current = null;
    setIsDrawing(false);
    setDrawStart(null);
    setDrawPreview(null);

    if (size < 10) {
      drawFacesOnCanvas(detectedFaces);
      return;
    }

    const squareX = Math.round(dx >= 0 ? startX : startX - size);
    const squareY = Math.round(dy >= 0 ? startY : startY - size);
    const squareSize = Math.round(size);

    if (!catalogImages[currentImageIndex] || !taggingCatalog) return;

    try {
      const imageWidth = imageRef.current?.naturalWidth;
      const imageHeight = imageRef.current?.naturalHeight;

      const response = await faceDetectionAPI.createManualFace(
        taggingCatalog,
        catalogImages[currentImageIndex].id,
        {
          x: squareX,
          y: squareY,
          w: squareSize,
          h: squareSize,
          image_width: imageWidth,
          image_height: imageHeight
        }
      );

      const newFace = {
        id: response.data.face_id,
        x: squareX,
        y: squareY,
        w: squareSize,
        h: squareSize,
        confidence: null,
        person_id: null,
        person_name: null,
        source: 'Manual',
        image_width: imageWidth,
        image_height: imageHeight
      };

      const updatedFaces = [...detectedFaces, newFace];
      setDetectedFaces(updatedFaces);
      drawFacesOnCanvas(updatedFaces);
      setSelectedFace(newFace);
    } catch (err) {
      setError('Failed to create tagging area');
      drawFacesOnCanvas(detectedFaces);
    }
  };

  const handleCanvasMouseLeave = () => {
    if (isDrawingRef.current) {
      isDrawingRef.current = false;
      drawStartRef.current = null;
      setIsDrawing(false);
      setDrawStart(null);
      setDrawPreview(null);
      drawFacesOnCanvas(detectedFaces);
    }
  };

  const handleTagFace = async (personId) => {
    if (!selectedFace) return;
    
    try {
      await faceDetectionAPI.tagFace(taggingCatalog, selectedFace.id, personId);
      
      const updatedFaces = detectedFaces.map(f => {
        if (f.id === selectedFace.id) {
          const person = people.find(p => p.id === personId);
          return { ...f, person_id: personId, person_name: person?.name || null };
        }
        return f;
      });
      
      setDetectedFaces(updatedFaces);
      drawFacesOnCanvas(updatedFaces);
      setShowTagModal(false);
      setSelectedFace(null);
    } catch (err) {
      setError('Failed to tag face');
    }
  };

  const handleRemoveTag = async (face) => {
    if (!face || !face.person_id) return;
    
    try {
      await faceDetectionAPI.untagFace(taggingCatalog, face.id);
      
      const updatedFaces = detectedFaces.map(f => {
        if (f.id === face.id) {
          return { ...f, person_id: null, person_name: null };
        }
        return f;
      });
      
      setDetectedFaces(updatedFaces);
      drawFacesOnCanvas(updatedFaces);
      setSelectedFace(null);
    } catch (err) {
      setError('Failed to remove tag');
    }
  };

  const handleDeleteArea = async (face) => {
    if (!face) return;
    
    try {
      await faceDetectionAPI.deleteFace(taggingCatalog, face.id);
      
      const updatedFaces = detectedFaces.filter(f => f.id !== face.id);
      
      setDetectedFaces(updatedFaces);
      drawFacesOnCanvas(updatedFaces);
      setSelectedFace(null);
    } catch (err) {
      setError('Failed to remove area');
    }
  };

  const handleAddNewPerson = async () => {
    if (!newPersonName.trim()) return;
    
    try {
      // Into the tab the picker is showing, so the new person is found where
      // they were added rather than under Other.
      const response = await api.post(`/catalogs/${taggingCatalog}/people`, {
        name: newPersonName.trim(),
        category: tagCategory,
      });
      
      await loadPeople();
      
      if (selectedFace && response.data.id) {
        const updatedFaces = detectedFaces.map(f => {
          if (f.id === selectedFace.id) {
            return { ...f, person_id: response.data.id, person_name: newPersonName.trim() };
          }
          return f;
        });
        
        await faceDetectionAPI.tagFace(taggingCatalog, selectedFace.id, response.data.id);
        
        setDetectedFaces(updatedFaces);
        drawFacesOnCanvas(updatedFaces);
        setSelectedFace(null);
      }
      
      setShowAddPersonModal(false);
      setNewPersonName('');
      setShowTagModal(false);
    } catch (err) {
      setError('Failed to add person');
    }
  };

  const handleImageLoad = async () => {
    setShowSidePanel(true);
    
    const currentImage = catalogImages[currentImageIndex];
    if (!currentImage) return;

    // Load existing faces for this image
    const faces = await loadExistingFaces(currentImage.id);
    await loadPetsForImage(currentImage.id, faces);

    // Load detailed photo info (EXIF, events, locations)
    try {
      const r = await photoAPI.getById(taggingCatalog, currentImage.id);
      setPhotoDetail(r.data);
      setImageDescription(r.data?.description || '');
    } catch {
      setPhotoDetail(null);
      setImageDescription('');
    }
    setIsEditingDescription(false);
  };

  // ─── Basic Editor ──────────────────────────────────────────────────────────
  const savedOrientation = photoDetail?.edit?.orientation || 1;
  const editDirty = editingPhoto && pendingOrientation !== savedOrientation;

  const openEditor = () => {
    setPendingOrientation(savedOrientation);
    setEditError(null);
    setEditNotice(null);
    setEditingPhoto(true);
  };

  const cancelEditor = () => {
    if (editSaving) return;
    setEditingPhoto(false);
    setPendingOrientation(savedOrientation);
    setEditError(null);
  };

  const saveEditor = async () => {
    const image = catalogImages[currentImageIndex];
    if (!image || editSaving || pendingOrientation === savedOrientation) return;
    setEditSaving(true);
    setEditError(null);
    try {
      const base = photoDetail?.edit?.version ?? 0;
      const r = pendingOrientation === 1
        ? await photoEditAPI.reset(taggingCatalog, image.id, base)
        : await photoEditAPI.save(taggingCatalog, image.id, pendingOrientation, base);
      const owner = r.data.file_id || image.id;
      let edit = r.data;
      // 202: the derivatives are being rebuilt. Asked every second until done.
      const deadline = Date.now() + 5 * 60 * 1000;
      while (edit.render_status === 'rendering' || edit.render_status === 'pending') {
        if (Date.now() > deadline) throw new Error('The rebuild is taking too long - reload the page later.');
        // eslint-disable-next-line no-await-in-loop
        await new Promise((resolve) => { setTimeout(resolve, 1000); });
        // eslint-disable-next-line no-await-in-loop
        edit = (await photoEditAPI.get(taggingCatalog, owner)).data;
      }
      if (edit.render_status === 'failed') {
        throw new Error(edit.render_error || 'The photo could not be rebuilt.');
      }
      bumpEditVersion(taggingCatalog, owner, edit.version);
      if (owner !== image.id) bumpEditVersion(taggingCatalog, image.id, edit.version);
      const albums = r.data.published_albums || [];
      setEditNotice(`Saved. The original file is unchanged.${albums.length
        ? ` It is in a published album: re-publish ${albums.map((a) => `“${a.name}”`).join(', ')} to update it.`
        : ''}`);
      setPhotoDetail((prev) => (prev ? { ...prev, edit } : prev));
      setEditingPhoto(false);
      // A new image URL: its onLoad reloads the faces, pets and details.
      setThumbTick((n) => n + 1);
    } catch (err) {
      setEditError(err.response?.data?.error || err.message || 'Could not save the edit.');
    } finally {
      setEditSaving(false);
    }
  };

  // Shortcuts while editing: [ and ] turn, Esc cancels, Ctrl/Cmd+S saves.
  useEffect(() => {
    if (!editingPhoto) return undefined;
    const onKey = (e) => {
      if (e.target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;
      if (e.key === '[') { e.preventDefault(); setPendingOrientation((v) => applyTool(v, 'rotate_left')); }
      else if (e.key === ']') { e.preventDefault(); setPendingOrientation((v) => applyTool(v, 'rotate_right')); }
      else if (e.key === 'Escape') { e.preventDefault(); cancelEditor(); }
      else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); saveEditor(); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }); // eslint-disable-line react-hooks/exhaustive-deps

  // Another photo is another edit.
  const editingFileId = catalogImages[currentImageIndex]?.id;
  useEffect(() => {
    setEditingPhoto(false);
    setEditError(null);
  }, [editingFileId]);

  // Leaving the page with an unsaved edit asks first.
  useEffect(() => {
    if (!editDirty) return undefined;
    const onBeforeUnload = (e) => { e.preventDefault(); e.returnValue = ''; };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [editDirty]);

  // The preview: the image on screen already carries the saved edit, so it is
  // turned by the difference, shrunk to fit when a quarter turn swaps its sides.
  const previewStyle = () => {
    if (!editingPhoto) return {};
    const d = orientationDelta(savedOrientation, pendingOrientation);
    let fit = 1;
    const img = imageRef.current;
    const box = containerRef.current;
    if (swaps(d) && img && box && img.offsetWidth && img.offsetHeight) {
      fit = Math.min(1, box.clientWidth / img.offsetHeight,
                     (window.innerHeight * 0.7) / img.offsetWidth);
    }
    return { transform: cssTransform(d, fit), transition: 'transform 0.2s ease' };
  };

  const handleEditDate = () => {
    const src = photoDetail?.master_datetime;
    const d   = src ? new Date(src) : new Date();
    const pad = (n) => String(n).padStart(2, '0');
    setDateInput(`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`);
    setTimeInput(`${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`);
    setDateError(null);
    setEditingDate(true);
  };

  // A photo removed from the catalog leaves this page at once: out of the day
  // being shown and out of the date tree, then the next photo of the day opens.
  //
  // The scope is re-opened on the photo that takes its place rather than
  // edited here: removing one shifts the position of everything after it, and
  // the pages not yet fetched would come back at the old positions.
  const handlePhotoRemoved = async (result) => {
    const gone = new Set(result.removed_file_ids);
    const keep = (p) => p && !gone.has(p.id);
    const nextPhoto = [catalogImages[currentImageIndex + 1],
                       catalogImages[currentImageIndex - 1]].find(keep) ?? null;
    let opened = null;
    try {
      opened = await openScope(activeCatalog.id, selectedYear, selectedMonth,
                               selectedDay, { around: nextPhoto?.id ?? null });
    } catch (err) {
      console.error('Failed to reload after removing', err);
    }
    if (opened && opened.total > 0) {
      handleImageChange(Math.min(opened.position ?? 0, opened.total - 1), opened.items);
    } else {
      setPhotoDetail(null);
    }
    setRemoveNotice(
      `Removed ${result.file_names.join(' and ')} from the catalog. `
      + 'The original is still on disk - it is listed on Removed files until you delete it.');
  };

  const handleSavePhotoDate = async () => {
    const currentImage = catalogImages[currentImageIndex];
    if (!currentImage) return;
    setDateSaving(true);
    setDateError(null);
    try {
      const isoLocal = `${dateInput}T${timeInput}`;
      await photoAPI.updateDate(taggingCatalog, currentImage.id, isoLocal, tzInput);
      // Reload the photo detail so the date card reflects the new value
      const r = await photoAPI.getById(taggingCatalog, currentImage.id);
      setPhotoDetail(r.data);
      setEditingDate(false);
      setDateSuccess('Date updated. File has been renamed and moved.');
    } catch (err) {
      setDateError(err.response?.data?.error || 'Failed to update date');
    } finally {
      setDateSaving(false);
    }
  };

  const handleSaveDescription = async () => {
    const currentImage = catalogImages[currentImageIndex];
    if (!currentImage) return;

    setSavingDescription(true);
    try {
      await api.put(
        `/catalogs/${taggingCatalog}/files/${currentImage.id}/description`,
        { description: imageDescription }
      );
      setPhotoDetail(prev => prev ? { ...prev, description: imageDescription } : prev);
      setIsEditingDescription(false);
    } catch (err) {
      console.error('Failed to save description:', err);
      alert('Failed to save description');
    } finally {
      setSavingDescription(false);
    }
  };

  const handleGenerateDescription = async () => {
    const currentImage = catalogImages[currentImageIndex];
    if (!currentImage) return;

    setGeneratingDescription(true);
    try {
      const peopleNames = detectedFaces
        .filter(f => f.person_name)
        .map(f => f.person_name);

      const imagePath = currentImage.alternative_file_path || currentImage.file_path;
      if (!imagePath) throw new Error('Image path not available');

      const locationName = photoDetail?.locations?.length > 0 ? photoDetail.locations[0].name : null;

      const response = await fetch(`${VISION_HOST}/describe`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          image_path: imagePath,
          people_names: peopleNames.length > 0 ? peopleNames : undefined,
          location_name: locationName
        })
      });

      const data = await response.json();

      if (data.success && data.description) {
        setImageDescription(data.description);
        setIsEditingDescription(true);
      } else {
        throw new Error(data.error || 'Failed to generate description');
      }
    } catch (err) {
      console.error('Failed to generate description:', err);
      const errorMsg = err.message || 'Unknown error';
      alert(`Failed to generate description: ${errorMsg}\n\nMake sure the vision backend is running and the image file is accessible.\n\nImage: ${catalogImages[currentImageIndex]?.file_name || 'unknown'}`);
    } finally {
      setGeneratingDescription(false);
    }
  };

  // Animals in this photograph, scaled into the displayed image's pixels.
  //
  // The same scaling as a face and for the same reason: x/y/w/h are in the
  // PREPARED detection image's space - 1200 px on the long edge, usually -
  // while the canvas is sized to the natural image. image_width/image_height
  // travel with every box precisely so this conversion is possible, which is
  // what documentation/fixes/ has nine files about.
  // Clearing the faces without clearing these left the previous photograph's
  // animals drawn over the next one. One call, so the pair cannot drift.
  const clearPets = () => {
    detectedPetsRef.current = [];
    setDetectedPets([]);
  };

  const loadPetsForImage = async (fileId, faces = []) => {
    try {
      const r = await petAPI.imageDetections(taggingCatalog, fileId);
      const pets = (r.data?.pets || []).map((pet) => {
        const img = imageRef.current;
        if (!pet.image_width || !pet.image_height || !img) return pet;
        const nw = img.naturalWidth;
        const nh = img.naturalHeight;
        if (pet.image_width === nw && pet.image_height === nh) return pet;
        const sx = nw / pet.image_width;
        const sy = nh / pet.image_height;
        return {
          ...pet,
          x: Math.round(pet.x * sx),
          y: Math.round(pet.y * sy),
          w: Math.round(pet.w * sx),
          h: Math.round(pet.h * sy),
        };
      });
      detectedPetsRef.current = pets;
      setDetectedPets(pets);
      // Redraw, because the faces were drawn before these arrived.
      drawFacesOnCanvas(faces);
    } catch {
      // A catalog with pets switched off has no endpoint answer worth
      // reporting: the viewer simply shows no animals.
      detectedPetsRef.current = [];
      setDetectedPets([]);
    }
  };

  const loadExistingFaces = async (fileId) => {
    try {
      const response = await faceDetectionAPI.getFaces(taggingCatalog, fileId);
      if (response.data.faces && response.data.faces.length > 0) {
        const scaledFaces = response.data.faces.map(face => {
          // Only scale if we have stored detection dimensions and they differ from display
          if (face.image_width && face.image_height && imageRef.current) {
            const displayWidth = imageRef.current.naturalWidth;
            const displayHeight = imageRef.current.naturalHeight;
            
            console.log('Face coordinate scaling check:', {
              faceId: face.id,
              storedDimensions: { width: face.image_width, height: face.image_height },
              displayDimensions: { width: displayWidth, height: displayHeight },
              originalCoords: { x: face.x, y: face.y, w: face.w, h: face.h }
            });
            
            // Check if dimensions differ (accounting for potential orientation differences)
            // The detection image and display image should both be orientation-corrected,
            // but may have different scales
            if (face.image_width !== displayWidth || face.image_height !== displayHeight) {
              const scaleX = displayWidth / face.image_width;
              const scaleY = displayHeight / face.image_height;
              
              const scaledFace = {
                ...face,
                x: Math.round(face.x * scaleX),
                y: Math.round(face.y * scaleY),
                w: Math.round(face.w * scaleX),
                h: Math.round(face.h * scaleY)
              };
              
              console.log('Scaled face coordinates:', {
                faceId: face.id,
                scale: { x: scaleX, y: scaleY },
                scaledCoords: { x: scaledFace.x, y: scaledFace.y, w: scaledFace.w, h: scaledFace.h }
              });
              
              return scaledFace;
            }
          }
          return face;
        });
        
        setDetectedFaces(scaledFaces);
        drawFacesOnCanvas(scaledFaces);
        // Returned as well as stored: the pet loader runs immediately after
        // this and needs to redraw with them, and setState has not landed yet
        // in the same tick.
        return scaledFaces;
      } else {
        setDetectedFaces([]);
        clearPets();
        if (canvasRef.current) {
          const ctx = canvasRef.current.getContext('2d');
          ctx.clearRect(0, 0, canvasRef.current.width, canvasRef.current.height);
        }
      }
    } catch (err) {
      console.log('No existing faces found or error loading faces:', err);
      setDetectedFaces([]);
      clearPets();
    }
    return [];
  };

  const handleImageChange = (newIndex, images) => {
    const imageList = images || catalogImages;
    setCurrentImageIndex(newIndex);
    setDetectedFaces([]);
    clearPets();
    setSelectedFace(null);
    setHoveredFaceId(null);
    setPhotoDetail(null);
    setImageDescription('');
    setIsEditingDescription(false);
    setEditingDate(false);
    setDateError(null);
    isDrawingRef.current = false;
    drawStartRef.current = null;
    setIsDrawing(false);
    setDrawStart(null);
    setDrawPreview(null);
    if (canvasRef.current) {
      const ctx = canvasRef.current.getContext('2d');
      ctx.clearRect(0, 0, canvasRef.current.width, canvasRef.current.height);
    }
    // Keep URL in sync with the current photo
    const img = imageList[newIndex];
    if (img && activeCatalog) {
      navigate(`/photos/${activeCatalog.id}/${img.id}`, { replace: true });
    }
  };

  // Load face images for a person with optional year/month filters
  const loadPersonFaceImages = async (personId, year = null, month = null) => {
    if (!taggingCatalog || !personId) return;
    
    setLoadingFaceImages(true);
    try {
      const response = await faceDetectionAPI.getPersonFaceContents(taggingCatalog, personId, year, month);
      setPersonFaceImages(response.data.faces || []);
      
      // Extract available years from the response for filtering
      const years = [...new Set(response.data.faces.map(f => f.year))].filter(y => y).sort((a, b) => b - a);
      setAvailableAIYears(years);
    } catch (err) {
      console.error('Failed to load person face images:', err);
      setPersonFaceImages([]);
      setAvailableAIYears([]);
    } finally {
      setLoadingFaceImages(false);
    }
  };

  // Handle person selection for AI tagging
  const handlePersonSelectForAI = (person) => {
    setSelectedPersonForAI(person);
    setAITaggingYear(null);
    setAITaggingMonth(null);
    setPersonFaceImages([]);
    // Load all face images for this person initially
    loadPersonFaceImages(person.id);
  };

  // Handle year change for AI tagging
  const handleAIYearChange = (year) => {
    setAITaggingYear(year);
    setAITaggingMonth(null);
    if (selectedPersonForAI) {
      loadPersonFaceImages(selectedPersonForAI.id, year, null);
    }
  };

  // Handle month change for AI tagging
  const handleAIMonthChange = (month) => {
    setAITaggingMonth(month);
    if (selectedPersonForAI) {
      loadPersonFaceImages(selectedPersonForAI.id, aiTaggingYear, month);
    }
  };

  useEffect(() => {
    if (detectedFaces.length > 0) {
      drawFacesOnCanvas(detectedFaces);
    }
  }, [hoveredFaceId, selectedFace]);

  // ─── Derived data for slider ──────────────────────────────────────────────
  // What the strip shows: the day, the month, the year or the whole catalog,
  // depending on how much of the date is chosen. Each "All" button clears one
  // level and widens it. It is the scope itself - as long as the scope, filled
  // in a page at a time as the strip and the arrow keys reach it.
  const activeDayPhotos = catalogImages;
  const scopeTotal = scope.total;

  const activePhotoIndex = scope.indexOf(activePhotoId);

  // Resolve activePhotoId when the scope changes: it must be a photo in it.
  useEffect(() => {
    if (scopeTotal === 0) { setActivePhotoId(null); return; }
    if (scope.indexOf(activePhotoId) < 0) {
      const fallback = activeDayPhotos[currentImageIndex] ?? activeDayPhotos.find(Boolean);
      if (fallback) setActivePhotoId(fallback.id);
    }
  }, [activeDayPhotos, scopeTotal]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the pages either side of the photo on screen in hand, so the arrow
  // keys step onto a loaded photo rather than a hole.
  useEffect(() => {
    if (activePhotoIndex < 0) return;
    scope.ensure(activePhotoIndex - 1, activePhotoIndex + 2);
  }, [activePhotoIndex]); // eslint-disable-line react-hooks/exhaustive-deps

  // What the strip is about to draw.
  const handleStripRange = useCallback((start, end) => {
    scope.ensure(start, end);
  }, [scope.ensure]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keyboard navigation for slider. Wraps at either end, which under "All"
  // can land on a page not fetched yet - fetched first, then selected.
  useEffect(() => {
    const step = (to) => {
      const target = activeDayPhotos[to];
      if (target) { handleSliderClick(target); return; }
      scope.ensure(to, to + 1).then((items) => {
        if (items[to]) handleSliderClick(items[to]);
      });
    };
    const handleKey = (e) => {
      if (!scopeTotal || editingPhoto) return;
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
        e.preventDefault();
        step((activePhotoIndex + 1) % scopeTotal);
      } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
        e.preventDefault();
        step((activePhotoIndex - 1 + scopeTotal) % scopeTotal);
      }
    };
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [activeDayPhotos, activePhotoIndex, scopeTotal, editingPhoto]); // eslint-disable-line react-hooks/exhaustive-deps

  const currentImage = catalogImages[currentImageIndex];

  if (!activeCatalog) {
    return (
      <CContainer fluid className="py-4 px-4">
        <div className="text-center py-5 text-muted">
          <strong>No catalog selected</strong>
          <div style={{ marginTop: 8, fontSize: '0.9rem' }}>
            Select a catalog from the dropdown in the top menu to get started.
          </div>
        </div>
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="py-4 px-4">
      {/* Header */}
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Photos</h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            Identify and tag people in your photos
          </div>
        </div>
      </div>

      <CCard className="mb-4"
             style={editingPhoto ? { pointerEvents: 'none', opacity: 0.5 } : undefined}
             title={editingPhoto ? 'Save or cancel the edit first' : undefined}>
        <CCardBody className="py-3">
          <div className="d-flex flex-column gap-2">
              {/* ── Year row ──
                  "All" first, active while no year is picked, as on the Events
                  filter card. */}
              {yearsMonthsLoading ? (
                <CSpinner size="sm" color="secondary" />
              ) : (
                <div className="d-flex gap-1 flex-wrap">
                  <button
                    className={`btn btn-sm ${selectedYear == null ? 'btn-primary' : 'btn-outline-secondary'}`}
                    onClick={showAllYears}
                  >
                    All
                  </button>
                  {Object.keys(availableYearsMonths)
                    .filter(y => y !== 'Unknown')
                    .sort((a, b) => b - a)
                    .map((year) => (
                      <button
                        key={year}
                        className={`btn btn-sm ${selectedYear === Number(year) ? 'btn-primary' : 'btn-outline-secondary'}`}
                        onClick={() => handleYearChange(Number(year))}
                      >
                        {year}
                      </button>
                    ))}
                </div>
              )}

              {/* ── Month row — visible once a year is picked ── */}
              {selectedYear != null && (
                <div className="d-flex gap-1 flex-wrap">
                  {/* The whole year. Shown so the row has a visible "no month"
                      state rather than an implied one (Events says the same). */}
                  <button
                    className={`btn btn-sm ${selectedMonth == null ? 'btn-primary' : 'btn-outline-secondary'}`}
                    onClick={showAllOfYear}
                  >
                    All {selectedYear}
                  </button>
                  {Object.keys(availableYearsMonthsDays[String(selectedYear)] ?? {})
                    .map(Number)
                    .filter(m => m !== -1)
                    .sort((a, b) => a - b)
                    .map(monthIdx => (
                      <button
                        key={monthIdx}
                        className={`btn btn-sm ${selectedMonth === monthIdx ? 'btn-primary' : 'btn-outline-secondary'}`}
                        onClick={() => handleMonthChange(monthIdx)}
                      >
                        {MONTH_NAMES[monthIdx]?.slice(0, 3) ?? monthIdx}
                      </button>
                    ))}
                </div>
              )}

              {/* ── Day row — visible once a month is picked ── */}
              {selectedYear != null && selectedMonth != null && (
                <div className="d-flex gap-1 flex-wrap">
                  <button
                    className={`btn btn-sm ${selectedDay == null ? 'btn-primary' : 'btn-outline-secondary'}`}
                    onClick={showAllOfMonth}
                  >
                    All {MONTH_NAMES[selectedMonth]?.slice(0, 3) ?? ''}
                  </button>
                  {(availableYearsMonthsDays[String(selectedYear)]?.[selectedMonth] ?? [])
                    .filter(d => d !== -1)
                    .map(day => (
                      <button
                        key={day}
                        className={`btn btn-sm ${selectedDay === day ? 'btn-primary' : 'btn-outline-secondary'}`}
                        onClick={() => handleDayChange(day)}
                      >
                        {day}
                      </button>
                    ))}
                </div>
             )}
         </div>

         {/* ── Photo Slider ── */}
         {activeDayPhotos.length > 0 && (
           <div style={{
             borderTop: '1px solid #e0e0e0',
             paddingTop: 8,
             marginTop: 8,
           }}>
             {/* Only the tiles near the visible window are rendered, and only
                 their images are fetched - see components/Filmstrip.js. Under
                 "All" this list is as long as the catalog, and only the pages
                 the strip reaches are fetched (onRange). */}
             <Filmstrip
               items={activeDayPhotos}
               activeId={activePhotoId}
               activeIndex={activePhotoIndex >= 0 ? activePhotoIndex : undefined}
               onRange={handleStripRange}
               srcFor={(photo) => galleryThumbUrl(activeCatalog.id, photo.id, 72)}
               onSelect={handleSliderClick}
             />
             {activePhotoIndex >= 0 && (
               <div style={{ fontSize: '0.75rem', color: '#888', textAlign: 'center', marginTop: 4 }}>
                 {activePhotoIndex + 1} / {activeDayPhotos.length}
                 {activeDayPhotos[activePhotoIndex]?.master_datetime && (
                    <span style={{ marginLeft: 8 }}>
                      · {formatDateTime(activeDayPhotos[activePhotoIndex].master_datetime)}
                    </span>
                  )}
               </div>
             )}
           </div>
         )}
       </CCardBody>
     </CCard>

      {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}
      {dateSuccess && (
        <CAlert color="success" dismissible onClose={() => setDateSuccess(null)} className="mb-2">
          {dateSuccess}
        </CAlert>
      )}
      {removeNotice && (
        <CAlert color="info" dismissible onClose={() => setRemoveNotice(null)} className="mb-2">
          {removeNotice}
        </CAlert>
      )}
      {editNotice && (
        <CAlert color="success" dismissible onClose={() => setEditNotice(null)} className="mb-2">
          {editNotice}
        </CAlert>
      )}

      {!imagesLoading && catalogImages.length === 0 && (
        <CAlert color="info">No images found in this catalog.</CAlert>
      )}

      {imagesLoading && (
        <div className="text-center py-5">
          <CSpinner color="primary" />
          <p className="mt-2">Loading images...</p>
        </div>
      )}

      {catalogImages.length > 0 && (
        <div>
          <CRow className="g-3">
                  {/* ── Left column: Basic Editor, Date, Event, People, Location, Description ── */}
                  <CCol md={2} className="d-flex flex-column gap-3">
                    {/* ── Basic Editor (basic-photo-editing.md 3.1) ── */}
                    {photoDetail && !photoDetail.removed_at && (
                      <CCard>
                        <CCardHeader className="d-flex align-items-center py-2">
                          <CIcon icon={cilPencil} className="me-2" />
                          <strong>Basic Editor</strong>
                        </CCardHeader>
                        <CCardBody className="py-2">
                          {editingPhoto ? (
                            <div style={{ fontSize: '0.78rem', color: '#888' }}>Editing…</div>
                          ) : (
                            <>
                              {savedOrientation !== 1 && (
                                <div style={{ fontSize: '0.78rem' }} className="mb-1">
                                  {editSummary(savedOrientation)}
                                  {photoDetail.edit?.updated_at && (
                                    <div style={{ color: '#888' }}>
                                      {new Date(photoDetail.edit.updated_at).toLocaleDateString()}
                                      {photoDetail.edit.updated_by_name ? ` · ${photoDetail.edit.updated_by_name}` : ''}
                                    </div>
                                  )}
                                </div>
                              )}
                              {photoDetail.edit?.render_status === 'failed' && (
                                <div style={{ fontSize: '0.72rem' }} className="text-danger mb-1">
                                  The last rebuild failed: {photoDetail.edit.render_error}
                                </div>
                              )}
                              <div className="d-flex justify-content-end">
                                <CButton size="sm" color="primary" variant="outline" onClick={openEditor}>Open</CButton>
                              </div>
                            </>
                          )}
                        </CCardBody>
                      </CCard>
                    )}

                    {/* ── Date & Image counter ── */}
                    <CCard>
                      <CCardHeader className="d-flex align-items-center justify-content-between py-2">
                        <div className="d-flex align-items-center">
                          <CIcon icon={cilCalendar} className="me-2" />
                          <strong>Date</strong>
                        </div>
                        {!editingDate && (
                          <CButton size="sm" color="primary" variant="outline"
                                   onClick={handleEditDate} title="Edit date">
                            <CIcon icon={cilPencil} />
                          </CButton>
                        )}
                      </CCardHeader>
                      <CCardBody className="py-2">
                        {editingDate ? (
                          <>
                            <div className="mb-2">
                              <label style={{ fontSize: '0.72rem', color: '#888' }}>Date</label>
                              <input type="date" className="form-control form-control-sm"
                                     value={dateInput} onChange={(e) => setDateInput(e.target.value)} />
                            </div>
                            <div className="mb-2">
                              <label style={{ fontSize: '0.72rem', color: '#888' }}>Time</label>
                              <input type="time" step="1" className="form-control form-control-sm"
                                     value={timeInput} onChange={(e) => setTimeInput(e.target.value)} />
                            </div>
                            <div className="mb-2">
                              <label style={{ fontSize: '0.72rem', color: '#888' }}>Time Zone</label>
                              <select className="form-select form-select-sm" value={tzInput}
                                      onChange={(e) => setTzInput(e.target.value)}>
                                {getTimezoneOptions().map((tz) => (
                                  <option key={tz} value={tz}>{tz}</option>
                                ))}
                              </select>
                            </div>
                            {dateError && (
                              <div style={{ fontSize: '0.72rem', color: '#c00' }} className="mb-2">{dateError}</div>
                            )}
                            <div className="d-flex gap-2 justify-content-end">
                              <CButton size="sm" color="primary" variant="outline" onClick={handleSavePhotoDate}
                                       disabled={dateSaving || !dateInput || !timeInput}>
                                {dateSaving ? <CSpinner size="sm" /> : 'Save'}
                              </CButton>
                              <CButton size="sm" color="primary" variant="outline"
                                       onClick={() => { setEditingDate(false); setDateError(null); }}
                                       disabled={dateSaving}>
                                Cancel
                              </CButton>
                            </div>
                          </>
                        ) : (
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%' }}>
                            {(() => {
                              const _src = photoDetail?.master_datetime;
                              const _d = _src ? new Date(_src) : null;
                              const _day = _d && !isNaN(_d.getTime()) ? _d.getDate() : null;
                              const _suffix = _d && !isNaN(_d.getTime())
                                ? _d.toLocaleString('en-US', { weekday: 'short' }).toUpperCase()
                                : null;
                              return (
                                <>
                                  {_day !== null && (
                                    <div style={{
                                      display: 'flex', flexDirection: 'column', alignItems: 'center',
                                      justifyContent: 'center', flexShrink: 0, lineHeight: 1,
                                    }}>
                                      <span style={{ fontSize: '1.6rem', fontWeight: 800, color: '#212121', lineHeight: 1 }}>
                                        {String(_day).padStart(2, '0')}
                                      </span>
                                      <span style={{
                                        fontSize: '0.6rem', fontWeight: 700, color: '#888',
                                        textTransform: 'uppercase', letterSpacing: '0.5px', marginTop: 1,
                                      }}>
                                        {_suffix}
                                      </span>
                                    </div>
                                  )}
                                  <div style={{ flex: 1, minWidth: 0 }}>
                                    {_d && !isNaN(_d.getTime()) ? (
                                      <div style={{ fontSize: '0.75rem', color: '#444', fontWeight: 500 }}>
                                        {formatDate(_src)}
                                      </div>
                                    ) : (
                                      <div style={{ fontSize: '0.75rem', color: '#888', fontStyle: 'italic' }}>
                                        No date available
                                      </div>
                                    )}
                                    <div style={{ fontSize: '0.72rem', color: '#666', marginTop: 4 }}>
                                      Image {currentImageIndex + 1} of {catalogImages.length}
                                    </div>
                                    {currentImage && (
                                      <div style={{
                                        fontSize: '0.68rem', color: '#aaa', marginTop: 2,
                                        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                                      }}>
                                        {currentImage.file_name}
                                      </div>
                                    )}
                                  </div>
                                  {_d && !isNaN(_d.getTime()) && (
                                    <div style={{
                                      marginLeft: 'auto', flexShrink: 0, textAlign: 'right',
                                      display: 'flex', flexDirection: 'column', alignItems: 'flex-end',
                                      justifyContent: 'center',
                                    }}>
                                      <span style={{ fontSize: '1rem', fontWeight: 700, color: '#212121', lineHeight: 1 }}>
                                        {_d.toLocaleString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true }).replace(' ', '\u00A0')}
                                      </span>
                                    </div>
                                  )}
                                </>
                              );
                            })()}
                          </div>
                        )}
                      </CCardBody>
                    </CCard>

                    {/* ── Event ── */}
                    <CCard>
                      <CCardHeader className="d-flex align-items-center py-2">
                        <CIcon icon={cilCalendar} className="me-2" />
                        <strong>Event</strong>
                        {photoDetail?.events && photoDetail.events.length > 0 && (
                          <CBadge color="primary" className="ms-2">{photoDetail.events.length}</CBadge>
                        )}
                      </CCardHeader>
                      <CCardBody className="py-2" style={{ maxHeight: '140px', overflowY: 'auto' }}>
                        {photoDetail?.events && photoDetail.events.length > 0 ? (
                          <CListGroup flush>
                            {photoDetail.events.map((event) => (
                              <CListGroupItem key={event.id} className="px-0 py-2 border-0">
                                <div
                                  style={{ fontSize: '0.8rem', fontWeight: 500, color: '#1a73e8', cursor: 'pointer', textDecoration: 'underline' }}
                                  onClick={() => navigate(`/events/${activeCatalog.id}/${event.id}`)}
                                >
                                  {event.name}
                                </div>
                                {event.event_type && (
                                  <CBadge color="secondary" className="me-2" style={{ fontSize: '0.62rem' }}>
                                    {event.event_type}
                                  </CBadge>
                                )}
                                {event.start_time && (
                                  <div style={{ fontSize: '0.72rem', color: '#666' }}>
                                    {formatDate(event.start_time)}
                                  </div>
                                )}
                              </CListGroupItem>
                            ))}
                          </CListGroup>
                        ) : (
                          <div style={{ fontSize: '0.8rem', color: '#888', fontStyle: 'italic' }}>
                            No events tagged
                          </div>
                        )}
                      </CCardBody>
                    </CCard>

                    {/* ── People ── */}
                    <CCard>
                      <CCardHeader className="d-flex align-items-center py-2">
                        <CIcon icon={cilPeople} className="me-2" />
                        <strong>People</strong>
                        {photoDetail?.people && photoDetail.people.length > 0 && (
                          <CBadge color="primary" className="ms-2">{photoDetail.people.length}</CBadge>
                        )}
                      </CCardHeader>
                      <CCardBody className="py-2">
                        {photoDetail?.people && photoDetail.people.length > 0 ? (
                          <div className="d-flex flex-wrap gap-2">
                            {photoDetail.people.map((person) => (
                              <CBadge
                                key={person.id}
                                color="info"
                                style={{ fontSize: '0.78rem', padding: '0.3rem 0.5rem' }}
                              >
                                {person.name}
                              </CBadge>
                            ))}
                          </div>
                        ) : (
                          <div style={{ fontSize: '0.8rem', color: '#888', fontStyle: 'italic' }}>
                            No people tagged
                          </div>
                        )}
                      </CCardBody>
                    </CCard>

                    {/* ── Location ── */}
                    <CCard>
                      <CCardHeader className="d-flex align-items-center py-2">
                        <CIcon icon={cilLocationPin} className="me-2" />
                        <strong>Location</strong>
                        {(() => {
                          const n = (photoDetail?.locations?.length || 0)
                                  + (photoDetail?.location?.location_id != null ? 1 : 0);
                          return n > 0
                            ? <CBadge color="primary" className="ms-2">{n}</CBadge>
                            : null;
                        })()}
                      </CCardHeader>
                      <CCardBody className="py-2">
                        {/* ── This photo's OWN location, and how it got one ──
                            The resolved answer from file_locations, which is
                            the primary one now: it is a fact about the FILE,
                            not about an event the file happens to belong to.
                            See location-spec.md 4.1 and 8.3.

                            The provenance line is not decoration. A location the
                            camera's coordinates earned, one a person typed and
                            one inherited from the rest of an event are three
                            different claims, and a card that showed them
                            identically would be hiding the only thing that
                            tells you how much to trust it. location_share is what
                            separates a person's tag on the event (null) from
                            its members' vote (a fraction). */}
                        {photoDetail?.location?.location_id != null && (
                          <div className="mb-2 pb-2" style={{ borderBottom: '1px solid #eee' }}>
                            <div style={{ fontSize: '0.8rem', fontWeight: 500, color: '#1a73e8' }}>
                              {photoDetail.location.location_name || 'Unnamed location'}
                            </div>
                            <div style={{ fontSize: '0.7rem', color: '#888', marginTop: 1 }}>
                              {photoDetail.location.location_source === 'manual' && 'tagged by hand'}
                              {photoDetail.location.location_source === 'cluster'
                                && 'from this photo’s own coordinates'}
                              {photoDetail.location.location_source === 'event' && (
                                photoDetail.location.location_share == null
                                  ? `tagged on event${photoDetail.location.location_event_name
                                      ? ` ${photoDetail.location.location_event_name}` : ''}`
                                  : `agreed by ${Math.round(photoDetail.location.location_share * 100)}%`
                                    + ` of the photos in event`
                                    + `${photoDetail.location.location_event_name
                                        ? ` ${photoDetail.location.location_event_name}` : ''}`
                              )}
                            </div>
                          </div>
                        )}

                        {/* The locations reached through this photo's EVENTS.
                            The fallback path, not the primary one: one
                            tagged on an Auto event does not survive the next
                            Create Events run, so it is shown as context rather
                            than as this photo's answer (location-spec.md 4.7).

                            Scrolls on its own so the coordinates below stay
                            visible however many there are - the whole body
                            used to be the scroll area, which would have hidden
                            them. */}
                        <div style={{ maxHeight: '140px', overflowY: 'auto' }}>
                          {photoDetail?.locations && photoDetail.locations.length > 0 ? (
                            <>
                              <div style={{ fontSize: '0.7rem', color: '#888',
                                            textTransform: 'uppercase', letterSpacing: '0.03em' }}>
                                Through its events
                              </div>
                              <CListGroup flush>
                                {photoDetail.locations.map((loc) => (
                                  <CListGroupItem key={loc.id} className="px-0 py-2 border-0">
                                    <div style={{ fontSize: '0.8rem', fontWeight: 500, color: '#1a73e8' }}>
                                      {loc.name}
                                    </div>
                                    {(loc.latitude && loc.longitude) && (
                                      <div style={{ fontSize: '0.72rem', color: '#888' }}>
                                        {loc.latitude.toFixed(4)}, {loc.longitude.toFixed(4)}
                                      </div>
                                    )}
                                  </CListGroupItem>
                                ))}
                              </CListGroup>
                            </>
                          ) : photoDetail?.location?.location_id == null ? (
                            <div style={{ fontSize: '0.8rem', color: '#888', fontStyle: 'italic' }}>
                              No location yet
                            </div>
                          ) : null}
                        </div>

                        {/* ── The photo's OWN coordinates ──────────────────
                            A different fact from the locations above: those are
                            what someone tagged this photo WITH, this is where
                            the camera says it was. They can disagree, and when
                            they do it is worth being able to see both.

                            Read from exif_data.latitude/longitude, which the
                            import extracts and stores in the same statement as
                            creation_date (utilities.extract_exif_data). Absent
                            on 56% of a real catalog - a phone with location off,
                            a scan, a camera with no GPS - so the section is
                            omitted entirely rather than shown empty.

                            Six decimals: ~0.1 m, and the precision you want
                            when pasting into a map. The tagged locations
                            above keep four, because a location is an area. */}
                        {(photoDetail?.latitude != null && photoDetail?.longitude != null) && (
                          <div className="mt-2 pt-2" style={{ borderTop: '1px solid #eee' }}>
                            <div style={{ fontSize: '0.7rem', color: '#888',
                                          textTransform: 'uppercase', letterSpacing: '0.03em' }}>
                              Coordinates
                            </div>
                            <div style={{ fontSize: '0.8rem', fontFamily: 'monospace',
                                          color: '#495057', marginTop: 2 }}>
                              {photoDetail.latitude.toFixed(6)}, {photoDetail.longitude.toFixed(6)}
                            </div>
                            <div className="d-flex align-items-center gap-2" style={{ marginTop: 3 }}>
                              {photoDetail.altitude != null && (
                                <span style={{ fontSize: '0.72rem', color: '#888' }}>
                                  {Math.round(photoDetail.altitude)} m
                                </span>
                              )}
                              {/* OpenStreetMap, which the event map already
                                  uses for its tiles - so this opens the same
                                  service the application is built on rather
                                  than introducing another one. Explicit click,
                                  and noopener so the opened page gets no handle
                                  on this one. */}
                              <a
                                href={`https://www.openstreetmap.org/?mlat=${photoDetail.latitude}&mlon=${photoDetail.longitude}#map=16/${photoDetail.latitude}/${photoDetail.longitude}`}
                                target="_blank"
                                rel="noopener noreferrer"
                                style={{ fontSize: '0.72rem' }}
                                title="Open these coordinates on OpenStreetMap"
                              >
                                map
                              </a>
                            </div>
                          </div>
                        )}
                      </CCardBody>
                    </CCard>

                    {/* ── Description ── */}
                    <CCard>
                      <CCardHeader className="d-flex justify-content-between align-items-center py-2">
                        <strong>Description</strong>
                        <CButton
                          color="primary"
                          variant="outline"
                          size="sm"
                          onClick={handleGenerateDescription}
                          disabled={generatingDescription}
                        >
                          {generatingDescription ? (
                            <>
                              <CSpinner size="sm" className="me-1" />
                              Generating...
                            </>
                          ) : (
                            <>✨ AI</>
                          )}
                        </CButton>
                      </CCardHeader>
                      <CCardBody className="py-2">
                        {isEditingDescription ? (
                          <>
                            <CFormTextarea
                              rows={4}
                              value={imageDescription}
                              onChange={(e) => setImageDescription(e.target.value)}
                              placeholder="Enter image description..."
                              className="mb-2"
                              style={{ fontSize: '0.8rem' }}
                            />
                            <div className="d-flex gap-2 justify-content-end">
                              <CButton
                                color="primary"
                                variant="outline"
                                size="sm"
                                onClick={handleSaveDescription}
                                disabled={savingDescription}
                              >
                                {savingDescription ? (
                                  <>
                                    <CSpinner size="sm" className="me-1" />
                                    Saving...
                                  </>
                                ) : (
                                  <>
                                    <CIcon icon={cilCheck} className="me-1" />
                                    Save
                                  </>
                                )}
                              </CButton>
                              <CButton
                                color="primary"
                                size="sm"
                                variant="outline"
                                onClick={() => {
                                  setIsEditingDescription(false);
                                  setImageDescription(photoDetail?.description || '');
                                }}
                                disabled={savingDescription}
                              >
                                Cancel
                              </CButton>
                            </div>
                          </>
                        ) : (
                          <>
                            <div
                              className="mb-2"
                              style={{
                                fontSize: '0.8rem',
                                lineHeight: '1.5',
                                color: imageDescription ? '#212529' : '#888',
                                fontStyle: imageDescription ? 'normal' : 'italic',
                              }}
                            >
                              {imageDescription || 'No description'}
                            </div>
                            <div className="d-flex justify-content-end">
                              <CButton
                                color="primary"
                                size="sm"
                                variant="outline"
                                onClick={() => setIsEditingDescription(true)}
                              >
                                <CIcon icon={cilPencil} className="me-1" />
                                Edit
                              </CButton>
                            </div>
                          </>
                        )}
                      </CCardBody>
                    </CCard>

                    {/* ── Versions card ──────────────────────────────────
                        Every file that IS this image, plus the one rendering
                        the system made from it. Thumbnails are deliberately
                        absent (file-unity-spec.md section 7).

                        Rendered even when there is no group and only one
                        alternate: a card that appears only sometimes is harder
                        to find than one that is always there. */}
                    {photoDetail?.versions?.length > 0 && (
                      <CCard>
                        <CCardHeader className="d-flex align-items-center justify-content-between py-2">
                          <div className="d-flex align-items-center">
                            <CIcon icon={cilLayers} className="me-2" />
                            <strong>Versions</strong>
                          </div>
                          {photoDetail?.image_group && (
                            <CBadge color="info" style={{ fontSize: '0.65rem' }}
                                    title={`Matched on ${photoDetail.image_group.match_rule}`}>
                              {photoDetail.image_group.kind === 'live_photo' ? 'LIVE PHOTO' : 'RAW + JPG'}
                            </CBadge>
                          )}
                        </CCardHeader>
                        <CCardBody className="py-2">
                          {photoDetail.versions.map((v) => (
                            <a
                              key={`${v.kind}-${v.file_id}-${v.label}`}
                              href={`${API_HOST}${v.url}`}
                              target={v.kind === 'original' ? undefined : '_blank'}
                              rel="noreferrer"
                              className="d-flex justify-content-between align-items-center py-1"
                              style={{ fontSize: '0.78rem', textDecoration: 'none', color: '#0d6efd' }}
                              title={v.is_raw
                                ? 'Downloads the RAW file. Browsers cannot display it.'
                                : v.kind === 'original'
                                  ? 'Downloads the original file'
                                  : 'Opens this version'}
                            >
                              <span className="text-truncate me-2">
                                {v.label}{v.kind !== 'original' && savedOrientation !== 1 ? ' (edited)' : ''}
                              </span>
                              <span style={{ color: '#888', whiteSpace: 'nowrap' }}>
                                {v.width ? `${v.width}px · ` : ''}{formatFileSize(v.bytes)}
                              </span>
                            </a>
                          ))}
                          {photoDetail.versions.some((v) => v.is_raw) && (
                            <div style={{ fontSize: '0.68rem', color: '#999', marginTop: 6 }}>
                              RAW files download rather than open — no browser can display one.
                            </div>
                          )}
                        </CCardBody>
                      </CCard>
                    )}
                  </CCol>

                  {/* ── Middle column: Image ── */}
                  <CCol md={8}>
                    <div
                      ref={containerRef}
                      style={{
                        position: 'relative',
                        width: '100%',
                        maxHeight: '70vh',
                        overflow: 'hidden',
                        border: '1px solid #ddd',
                        borderRadius: '4px',
                        backgroundColor: '#f8f9fa',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                      }}
                    >
                      {currentImage && (
                        <>
                          <img
                            ref={imageRef}
                            src={`${API_HOST}/api/catalogs/files?path=${encodeURIComponent(getImageDisplayPath(currentImage))}${
                              editVersionOf(activeCatalog.id, currentImage.id)
                                ? `&v=${editVersionOf(activeCatalog.id, currentImage.id)}` : ''}`}
                            alt={currentImage.file_name}
                            style={{
                              maxWidth: '100%',
                              maxHeight: '70vh',
                              width: 'auto',
                              height: 'auto',
                              display: 'block',
                              objectFit: 'contain',
                              ...previewStyle(),
                            }}
                            onLoad={handleImageLoad}
                          />
                          <canvas
                            ref={canvasRef}
                            style={{
                              position: 'absolute',
                              pointerEvents: 'auto',
                              cursor: 'crosshair',
                              // Its boxes are in the saved orientation; hidden
                              // while the preview turns the image.
                              display: editingPhoto ? 'none' : undefined,
                            }}
                            onMouseDown={handleCanvasMouseDown}
                            onMouseMove={handleCanvasMouseMove}
                            onMouseUp={handleCanvasMouseUp}
                            onMouseLeave={handleCanvasMouseLeave}
                          />
                        </>
                      )}
                    </div>

                  </CCol>

                  <CCol md={2} className="d-flex flex-column gap-3">
                    {/* While editing, the editor's tools are all this column
                        holds (basic-photo-editing.md 3.2). */}
                    {editingPhoto ? (
                      <BasicEditorTools
                        value={pendingOrientation}
                        saved={savedOrientation}
                        saving={editSaving}
                        error={editError}
                        onTool={(tool) => setPendingOrientation((v) => applyTool(v, tool))}
                        onReset={() => setPendingOrientation(1)}
                        onSave={saveEditor}
                        onCancel={cancelEditor}
                      />
                    ) : (<>
                    {/* ── Detected Faces card ── */}
                    <CCard>
                      <CCardHeader className="d-flex justify-content-between align-items-center">
                        <div>
                          <strong>Detected Faces</strong>
                          {showSidePanel && (
                            <CBadge color="primary" className="ms-2">{detectedFaces.length}</CBadge>
                          )}
                        </div>
                      </CCardHeader>
                      <CCardBody className="py-2">
                        <div className="d-flex justify-content-end mb-2">
                          <CButton
                            color="primary"
                            variant="outline"
                            size="sm"
                            onClick={handleDetectFaces}
                            disabled={detectingFaces}
                          >
                            {detectingFaces ? (
                              <>
                                <CSpinner size="sm" className="me-1" />
                                Detecting...
                              </>
                            ) : (
                              <>
                                <CIcon icon={cilFace} className="me-1" />
                                Auto-detect
                              </>
                            )}
                          </CButton>
                        </div>
                        {showSidePanel && (
                          <div>
                            {selectedFace && (
                              <>
                                <div className="mb-3 p-2 border rounded bg-light">
                                  <strong>Selected Face:</strong>
                                  <p className="mb-2 small">
                                    {selectedFace.person_name || 'Untagged'}
                                  </p>
                                  <div className="d-flex flex-column gap-2">
                                    <CButton
                                      color="primary"
                                      variant="outline"
                                      size="sm"
                                      onClick={() => setShowTagModal(true)}
                                    >
                                      {selectedFace.person_id ? 'Change Tag' : 'Tag Face'}
                                    </CButton>
                                    {selectedFace.person_id && (
                                      <CButton
                                        color="danger"
                                        variant="outline"
                                        size="sm"
                                        onClick={() => handleRemoveTag(selectedFace)}
                                      >
                                        Remove Tag
                                      </CButton>
                                    )}
                                    <CButton
                                      color="danger"
                                      variant="outline"
                                      size="sm"
                                      onClick={() => handleDeleteArea(selectedFace)}
                                    >
                                      Delete Area
                                    </CButton>
                                  </div>
                                </div>
                                <hr />
                              </>
                            )}

                            {detectedFaces.length > 0 && (
                              <>
                                <p className="small text-muted mb-2">
                                  Click on a face to select it:
                                </p>
                                <CListGroup>
                                  {detectedFaces.map((face, index) => (
                                    <CListGroupItem
                                      key={face.id}
                                      action
                                      onClick={() => setSelectedFace(face)}
                                      style={{ cursor: 'pointer' }}
                                    >
                                      <div className="d-flex justify-content-between align-items-center">
                                        <span>
                                          {face.person_name || `Face ${index + 1}`}
                                        </span>
                                        {face.person_name && (
                                          <CBadge color="success">Tagged</CBadge>
                                        )}
                                      </div>
                                    </CListGroupItem>
                                  ))}
                                </CListGroup>
                              </>
                            )}

                            <div className="border-top pt-3 mt-3">
                              <CButton
                                color="primary"
                                variant="outline"
                                size="sm"
                                className="w-100"
                                onClick={() => setShowAddPersonModal(true)}
                              >
                                <CIcon icon={cilPlus} className="me-1" />
                                Add New Person
                              </CButton>
                            </div>
                          </div>
                        )}
                      </CCardBody>
                    </CCard>

                    {/* ── Detected Pets card ──
                        Below Detected Faces and deliberately quieter: there is
                        no detect button and nothing to click. Animals are found
                        by the ride-along inside Find Faces, and named in
                        groups on Identify pets, so a per-photograph control here
                        would be a fourth place to do it. This says what is in
                        the picture and links to the animal. */}
                    {detectedPets.length > 0 && (
                      <CCard>
                        <CCardHeader className="d-flex align-items-center py-2">
                          <CIcon icon={cilPaw} className="me-2" />
                          <strong>Detected Pets</strong>
                          <CBadge color="warning" className="ms-2">{detectedPets.length}</CBadge>
                        </CCardHeader>
                        <CCardBody className="py-2">
                          <CListGroup flush>
                            {detectedPets.map((pet) => (
                              <CListGroupItem
                                key={pet.id}
                                className="px-0 py-2 border-0 d-flex align-items-center gap-2"
                                action={!!pet.person_id}
                                onClick={pet.person_id
                                  ? () => navigate(`/pets/${taggingCatalog}/${pet.person_id}`)
                                  : undefined}
                                style={{ cursor: pet.person_id ? 'pointer' : 'default' }}
                              >
                                <div style={{ width: 44, height: 44, flexShrink: 0,
                                              borderRadius: 4, overflow: 'hidden',
                                              background: '#f0f0f0', display: 'flex',
                                              alignItems: 'center', justifyContent: 'center' }}>
                                  {pet.crop_url ? (
                                    <img alt="" src={`${API_HOST}${pet.crop_url}`}
                                         style={{ width: '100%', height: '100%',
                                                  objectFit: 'cover' }} />
                                  ) : (
                                    <CIcon icon={cilPaw}
                                           style={{ width: 18, height: 18, color: '#c7c7c7' }} />
                                  )}
                                </div>
                                <div style={{ minWidth: 0 }}>
                                  <div className="text-truncate" style={{ fontSize: '0.85rem' }}>
                                    {pet.person_name || 'Not named yet'}
                                  </div>
                                  {/* Named, so the question is settled: show
                                      what the animal IS, from the people row
                                      somebody filled in, and drop the guess
                                      entirely. "looks like a cat, 56%" under a
                                      dog the user has already named reads as
                                      the page arguing with them - and on this
                                      very photograph that is exactly what it
                                      was doing.

                                      Unnamed, the guess is all there is, and
                                      it is hedged because it is wrong about a
                                      quarter of the time (spec 12.2). */}
                                  {pet.person_id ? (
                                    pet.person_species && (
                                      <CBadge color="secondary" shape="rounded-pill"
                                              style={{ fontSize: '0.62rem',
                                                       textTransform: 'capitalize' }}>
                                        {pet.person_species}
                                      </CBadge>
                                    )
                                  ) : (
                                    <div style={{ fontSize: '0.72rem', color: '#888' }}>
                                      looks like a {pet.species || 'animal'}
                                      {pet.source === 'Manual'
                                        ? ', by hand'
                                        : `, ${Math.round((pet.confidence || 0) * 100)}%`}
                                    </div>
                                  )}
                                </div>
                              </CListGroupItem>
                            ))}
                          </CListGroup>
                          {detectedPets.some((p) => !p.person_id) && (
                            <div className="text-muted mt-2" style={{ fontSize: '0.72rem' }}>
                              Unnamed animals are grouped on{' '}
                              <a href="/identify-pets">Identify pets</a>, where naming
                              a group names every photograph in it at once.
                            </div>
                          )}
                        </CCardBody>
                      </CCard>
                    )}

                    {/* ── Photo Information card ── */}
                    {photoDetail && (
                      <CCard>
                        <CCardHeader className="d-flex align-items-center py-2">
                          <CIcon icon={cilCamera} className="me-2" />
                          <strong>Photo Information</strong>
                        </CCardHeader>
                        <CCardBody className="py-2">
                          <CListGroup flush>
                            {photoDetail.master_datetime && (
                              <CListGroupItem className="px-0 py-1 border-0">
                                <div style={{ fontSize: '0.72rem', color: '#888' }}>Date</div>
                                <div style={{ fontSize: '0.82rem' }}>{formatDate(photoDetail.master_datetime)}</div>
                              </CListGroupItem>
                            )}
                            {photoDetail.camera_make && (
                              <CListGroupItem className="px-0 py-1 border-0">
                                <div style={{ fontSize: '0.72rem', color: '#888' }}>Camera Make</div>
                                <div style={{ fontSize: '0.82rem' }}>{photoDetail.camera_make}</div>
                              </CListGroupItem>
                            )}
                            {photoDetail.camera_model && (
                              <CListGroupItem className="px-0 py-1 border-0">
                                <div style={{ fontSize: '0.72rem', color: '#888' }}>Camera Model</div>
                                <div style={{ fontSize: '0.82rem' }}>{photoDetail.camera_model}</div>
                              </CListGroupItem>
                            )}
                            {photoDetail.lens_model && (
                              <CListGroupItem className="px-0 py-1 border-0">
                                <div style={{ fontSize: '0.72rem', color: '#888' }}>Lens</div>
                                <div style={{ fontSize: '0.82rem' }}>{photoDetail.lens_model}</div>
                              </CListGroupItem>
                            )}
                            {photoDetail.focal_length && (
                              <CListGroupItem className="px-0 py-1 border-0">
                                <div style={{ fontSize: '0.72rem', color: '#888' }}>Focal Length</div>
                                <div style={{ fontSize: '0.82rem' }}>{photoDetail.focal_length}mm</div>
                              </CListGroupItem>
                            )}
                            {photoDetail.aperture && (
                              <CListGroupItem className="px-0 py-1 border-0">
                                <div style={{ fontSize: '0.72rem', color: '#888' }}>Aperture</div>
                                <div style={{ fontSize: '0.82rem' }}>f/{photoDetail.aperture}</div>
                              </CListGroupItem>
                            )}
                            {photoDetail.iso && (
                              <CListGroupItem className="px-0 py-1 border-0">
                                <div style={{ fontSize: '0.72rem', color: '#888' }}>ISO</div>
                                <div style={{ fontSize: '0.82rem' }}>{photoDetail.iso}</div>
                              </CListGroupItem>
                            )}
                            {photoDetail.shutter_speed && (
                              <CListGroupItem className="px-0 py-1 border-0">
                                <div style={{ fontSize: '0.72rem', color: '#888' }}>Shutter Speed</div>
                                <div style={{ fontSize: '0.82rem' }}>{photoDetail.shutter_speed}s</div>
                              </CListGroupItem>
                            )}
                            {photoDetail.width && photoDetail.height && (
                              <CListGroupItem className="px-0 py-1 border-0">
                                <div style={{ fontSize: '0.72rem', color: '#888' }}>Dimensions</div>
                                <div style={{ fontSize: '0.82rem' }}>{photoDetail.width} × {photoDetail.height}</div>
                              </CListGroupItem>
                            )}
                            {photoDetail.file_size && (
                              <CListGroupItem className="px-0 py-1 border-0">
                                <div style={{ fontSize: '0.72rem', color: '#888' }}>File Size</div>
                                <div style={{ fontSize: '0.82rem' }}>{formatFileSize(photoDetail.file_size)}</div>
                              </CListGroupItem>
                            )}
                            {photoDetail.file_type && (
                              <CListGroupItem className="px-0 py-1 border-0">
                                <div style={{ fontSize: '0.72rem', color: '#888' }}>File Type</div>
                                <div style={{ fontSize: '0.82rem' }}>{photoDetail.file_type.toUpperCase()}</div>
                              </CListGroupItem>
                            )}
                            {!photoDetail.master_datetime && !photoDetail.creation_date && !photoDetail.camera_make && !photoDetail.camera_model
                              && !photoDetail.width && !photoDetail.file_size && (
                              <CListGroupItem className="px-0 py-1 border-0">
                                <div style={{ fontSize: '0.82rem', color: '#888', fontStyle: 'italic' }}>
                                  No EXIF data available
                                </div>
                              </CListGroupItem>
                            )}
                          </CListGroup>
                        </CCardBody>
                      </CCard>
                    )}

                    {/* ── File: the catalog's original, and removing it ── */}
                    <CatalogFileCard catalogId={taggingCatalog} detail={photoDetail}
                                     kind="photo" onRemoved={handlePhotoRemoved} />
                    </>)}
                  </CCol>
          </CRow>

        </div>
      )}

      {/* Tag Face Modal */}
      <CModal visible={showTagModal} onClose={() => setShowTagModal(false)} size="lg">
        <CModalHeader>
          <CModalTitle>Tag Face</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <p className="mb-3">Select a person to tag this face, or add a new person:</p>
          {/* One tab per people category, Family first. */}
          <CNav variant="tabs" className="mb-3" role="tablist">
            {tagTabs.map((cat) => (
              <CNavItem key={cat}>
                <CNavLink
                  href="#"
                  active={cat === tagCategory}
                  onClick={(e) => { e.preventDefault(); setTagCategory(cat); }}
                >
                  {cat}
                  <CBadge color={cat === tagCategory ? 'primary' : 'secondary'} className="ms-2">
                    {tagTabCounts[cat] || 0}
                  </CBadge>
                </CNavLink>
              </CNavItem>
            ))}
          </CNav>
          {peopleInTagTab.length === 0 && (
            <div className="text-muted text-center py-4" style={{ fontSize: '0.9rem' }}>
              Nobody in {tagCategory} yet. Add a new person to put them here.
            </div>
          )}
          <div className="d-flex flex-wrap gap-3 mb-3">
            {peopleInTagTab.map((person) => {
              // Check if person has a profile photo
              const hasProfilePhoto = person.profile_photo_display_path || person.profile_photo_path;
              
              // Determine image source for profile photo
              let imgSrc = null;
              if (hasProfilePhoto) {
                // If profile_photo_path is an absolute path, it's a pre-cropped face image
                const isCroppedFace = person.profile_photo_path && person.profile_photo_path.startsWith('/');
                if (isCroppedFace) {
                  imgSrc = `${API_HOST}/api/catalogs/files?path=${encodeURIComponent(person.profile_photo_path)}`;
                } else if (person.profile_photo_display_path) {
                  imgSrc = `${API_HOST}/api/catalogs/files?path=${encodeURIComponent(person.profile_photo_display_path)}`;
                } else if (person.profile_photo_path) {
                  imgSrc = `${PEOPLE_PHOTOS_BASE_URL}${person.profile_photo_path}`;
                }
              }
              
              console.log(`Person ${person.name}:`, {
                profile_photo_display_path: person.profile_photo_display_path,
                profile_photo_path: person.profile_photo_path,
                imgSrc: imgSrc,
                face_box: person.profile_photo_face_box
              });
              
              const fb = person.profile_photo_face_box;

              const isSelected = selectedPersonForAI?.id === person.id;
              
              return (
                <div
                  key={person.id}
                  style={{
                    border: `2px solid ${isSelected ? '#321fdb' : '#d8dbe0'}`,
                    borderRadius: '8px',
                    overflow: 'hidden',
                    width: '120px',
                    transition: 'all 0.2s',
                    backgroundColor: '#fff',
                    boxShadow: isSelected ? '0 2px 8px rgba(50, 31, 219, 0.2)' : 'none',
                  }}
                >
                  <div
                    onClick={() => handleTagFace(person.id)}
                    style={{ cursor: 'pointer' }}
                    onMouseEnter={(e) => {
                      if (!isSelected) {
                        e.currentTarget.parentElement.style.borderColor = '#321fdb';
                        e.currentTarget.parentElement.style.boxShadow = '0 2px 8px rgba(50, 31, 219, 0.2)';
                      }
                    }}
                    onMouseLeave={(e) => {
                      if (!isSelected) {
                        e.currentTarget.parentElement.style.borderColor = '#d8dbe0';
                        e.currentTarget.parentElement.style.boxShadow = 'none';
                      }
                    }}
                  >
                  {/* Profile Photo */}
                  <div
                    style={{
                      width: '120px',
                      height: '120px',
                      background: '#f5f5f5',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      overflow: 'hidden',
                      position: 'relative',
                    }}
                  >
                    {imgSrc ? (() => {
                      // If it's a cropped face or no face box data, display image directly
                      const isCroppedFace = person.profile_photo_path && person.profile_photo_path.startsWith('/');
                      if (isCroppedFace || !fb || !fb.img_width || !fb.img_height) {
                        return (
                          <img
                            src={imgSrc}
                            alt={person.name}
                            style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                          />
                        );
                      }
                      
                      // Crop to face region with padding using CSS background-image
                      const pad = Math.max(fb.w, fb.h) * 0.5;
                      const cropX = Math.max(0, fb.x - pad);
                      const cropY = Math.max(0, fb.y - pad);
                      const cropW = Math.min(fb.img_width - cropX, fb.w + pad * 2);
                      const cropH = Math.min(fb.img_height - cropY, fb.h + pad * 2);
                      const scale = fb.img_width / cropW;
                      const bgSize = `${scale * 100}%`;
                      const bgPosX = cropX === 0 ? '0%' : `${(cropX / (fb.img_width - cropW)) * 100}%`;
                      const bgPosY = cropY === 0 ? '0%' : `${(cropY / (fb.img_height - cropH)) * 100}%`;
                      
                      return (
                        <div
                          style={{
                            width: '100%',
                            height: '100%',
                            backgroundImage: `url(${imgSrc})`,
                            backgroundSize: bgSize,
                            backgroundPosition: `${bgPosX} ${bgPosY}`,
                            backgroundRepeat: 'no-repeat',
                          }}
                        />
                      );
                    })() : (
                      <div style={{ fontSize: '2.5rem', opacity: 0.35 }}>👤</div>
                    )}
                  </div>
                  
                  {/* Person Name */}
                  <div
                    style={{
                      padding: '8px',
                      textAlign: 'center',
                      fontSize: '0.85rem',
                      fontWeight: 500,
                      color: '#333',
                      whiteSpace: 'nowrap',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                    }}
                  >
                    {person.name}
                  </div>
                  </div>
                  
                  {/* Get Pictures Button */}
                  <div style={{ padding: '8px', borderTop: '1px solid #e0e0e0' }}>
                    <CButton
                      color="primary"
                      variant="outline"
                      size="sm"
                      className="w-100"
                      onClick={(e) => {
                        e.stopPropagation();
                        handlePersonSelectForAI(person);
                      }}
                    >
                      Get Pictures
                    </CButton>
                  </div>
                </div>
              );
            })}
          </div>
          
          {/* Year/Month Filters and Face Images Display */}
          {selectedPersonForAI && (
            <div className="border-top pt-3 mt-3">
              <h6 className="mb-3">
                Face Images for {selectedPersonForAI.name}
              </h6>
              
              {/* Year Filter */}
              <div className="mb-3">
                <label className="form-label small fw-bold">Filter by Year:</label>
                <div className="d-flex gap-1 flex-wrap">
                  <button
                    className={`btn btn-sm ${!aiTaggingYear ? 'btn-primary' : 'btn-outline-secondary'}`}
                    onClick={() => handleAIYearChange(null)}
                  >
                    All
                  </button>
                  {availableAIYears.map((year) => (
                    <button
                      key={year}
                      className={`btn btn-sm ${aiTaggingYear === year ? 'btn-primary' : 'btn-outline-secondary'}`}
                      onClick={() => handleAIYearChange(year)}
                    >
                      {year}
                    </button>
                  ))}
                </div>
              </div>
              
              {/* Month Filter */}
              {aiTaggingYear && (
                <div className="mb-3">
                  <label className="form-label small fw-bold">Filter by Month:</label>
                  <div className="d-flex gap-1 flex-wrap">
                    <button
                      className={`btn btn-sm ${!aiTaggingMonth ? 'btn-primary' : 'btn-outline-secondary'}`}
                      onClick={() => handleAIMonthChange(null)}
                    >
                      All
                    </button>
                    {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map((month) => (
                      <button
                        key={month}
                        className={`btn btn-sm ${aiTaggingMonth === month ? 'btn-primary' : 'btn-outline-secondary'}`}
                        onClick={() => handleAIMonthChange(month)}
                      >
                        {new Date(2000, month - 1).toLocaleString('default', { month: 'short' })}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              
              {/* Face Images Grid */}
              <div className="mb-3">
                {loadingFaceImages ? (
                  <div className="text-center py-3">
                    <CSpinner size="sm" />
                    <p className="small text-muted mt-2">Loading face images...</p>
                  </div>
                ) : personFaceImages.length > 0 ? (
                  <>
                    <p className="small text-muted mb-2">
                      Found {personFaceImages.length} face image{personFaceImages.length !== 1 ? 's' : ''}
                    </p>
                    <div
                      style={{
                        display: 'grid',
                        gridTemplateColumns: 'repeat(auto-fill, minmax(80px, 1fr))',
                        gap: '8px',
                        maxHeight: '300px',
                        overflowY: 'auto'
                      }}
                    >
                      {personFaceImages.map((face) => (
                        <div
                          key={face.id}
                          style={{
                            border: '1px solid #ddd',
                            borderRadius: '4px',
                            overflow: 'hidden',
                            aspectRatio: '1',
                            cursor: 'pointer',
                          }}
                          title={`${face.source_file_name} - ${face.year}/${face.month}/${face.day}`}
                        >
                          <img
                            src={`${API_HOST}/api/catalogs/files?path=${encodeURIComponent(face.file_path)}`}
                            alt={`Face ${face.id}`}
                            style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                          />
                        </div>
                      ))}
                    </div>
                  </>
                ) : (
                  <p className="small text-muted">No face images found for the selected filters.</p>
                )}
              </div>
            </div>
          )}
          
          <div className="border-top pt-3">
            <CButton
              color="success"
              onClick={() => {
                setShowTagModal(false);
                setShowAddPersonModal(true);
              }}
            >
              <CIcon icon={cilPlus} className="me-2" />
              Add New Person
            </CButton>
          </div>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" onClick={() => setShowTagModal(false)}>
            Cancel
          </CButton>
        </CModalFooter>
      </CModal>

      {/* Add New Person Modal */}
      <CModal visible={showAddPersonModal} onClose={() => setShowAddPersonModal(false)}>
        <CModalHeader>
          <CModalTitle>Add New Person</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <CForm>
            <div className="mb-3">
              <CFormLabel>Name *</CFormLabel>
              <CFormInput
                value={newPersonName}
                onChange={(e) => setNewPersonName(e.target.value)}
                placeholder="Enter name"
                onKeyPress={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    handleAddNewPerson();
                  }
                }}
              />
            </div>
          </CForm>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" onClick={() => setShowAddPersonModal(false)}>
            Cancel
          </CButton>
          <CButton
            color="primary"
            onClick={handleAddNewPerson}
            disabled={!newPersonName.trim()}
          >
            Add and Tag Face
          </CButton>
        </CModalFooter>
      </CModal>
    </CContainer>
  );
};

export default Photos;
