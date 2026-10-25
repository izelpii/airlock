import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { CContainer, CSpinner, CAlert, CBadge } from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilCalendar, cilChevronRight } from '@coreui/icons';
import { MapContainer, TileLayer, CircleMarker, Tooltip, Popup, useMapEvents } from 'react-leaflet';
import 'leaflet/dist/leaflet.css';
import { locationAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import YearRange from '../components/YearRange';

// ─── Locations · Map view ─────────────────────────────────────────────────────
// documentation/specs/location-map-spec.md.
//
// One map of the whole catalog, where a marker says how much happened somewhere
// and clicking through reaches the places. The per-location map already exists
// on each card and on a location's own page, so this page earns its keep only
// by showing everything at once - and by letting the year filter change what
// "everything" means.
//
// The grouping is the SERVER'S job: it rounds coordinates to a decimal place
// chosen from the zoom, which bounds the response between 88 and 1,041 markers
// on the measured catalog. The browser sends its zoom and draws what comes
// back; it never sees the grid.

// Which zoom levels share a grouping, mirroring MAP_ZOOM_ROUNDING in app.py.
// Only used to decide whether a zoom change is worth a request - the server
// remains the authority on what the grouping actually is.
const roundingFor = (zoom) => (zoom <= 5 ? 0 : zoom <= 8 ? 1 : zoom <= 11 ? 2 : 3);

// Area tracks the count, so a place with a hundred times the events looks a
// hundred times heavier. Radius would make the largest swallow the map.
const radiusFor = (count, max) => (
  max && count ? 6 + 16 * Math.sqrt(count / max) : 6
);

const n = (v) => Number(v || 0).toLocaleString();

// Same width open or closed, per the brief; only the height and the visibility
// change.
const PANEL_WIDTH = 260;

// Reports zoom changes upwards. Debounced by the caller, not here: zoomend and
// moveend fire freely while a wheel is still turning.
const ZoomWatcher = ({ onZoom }) => {
  useMapEvents({
    zoomend: (e) => onZoom(e.target.getZoom()),
  });
  return null;
};

const LocationsMap = () => {
  const navigate = useNavigate();
  const { activeCatalog } = useCatalog();
  const catalogId = activeCatalog?.id;

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [zoom, setZoom] = useState(4);
  const [range, setRange] = useState(null);     // null until years are known
  // Shown by default: the year range and the totals are the point of this page
  // as much as the markers are, and a control nobody can see is a control
  // nobody uses. It hides on request, for when the map wants the whole width.
  const [panelOpen, setPanelOpen] = useState(true);
  const timer = useRef(null);

  const load = useCallback(async (z, from, to) => {
    if (!catalogId) { setLoading(false); return; }
    try {
      const res = await locationAPI.map(catalogId, { zoom: z, from, to });
      setData(res.data);
      setError(null);
      // The first response is what tells us the selectable range. Set once:
      // `years` is deliberately not filtered server-side, so re-reading it on
      // every response would be harmless but pointless.
      setRange((prev) => prev || (res.data?.years?.min != null
        ? { from: res.data.years.min, to: res.data.years.max }
        : null));
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not load the map.');
    } finally {
      setLoading(false);
    }
  }, [catalogId]);

  // First load, and any change of catalog or year range.
  useEffect(() => {
    setLoading(true);
    load(zoom, range?.from, range?.to);
    // zoom is deliberately absent: a zoom change goes through the debounced
    // path below, and listing it here would fire a second request for every
    // notch of the wheel.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, range?.from, range?.to]);

  // A zoom change only matters when it crosses into a different grouping.
  const onZoom = useCallback((z) => {
    setZoom((prev) => {
      if (roundingFor(z) === roundingFor(prev)) return z;
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => load(z, range?.from, range?.to), 250);
      return z;
    });
  }, [load, range?.from, range?.to]);

  useEffect(() => () => timer.current && clearTimeout(timer.current), []);

  const groups = data?.groups || [];
  const busiest = useMemo(
    () => Math.max(0, ...groups.map((g) => g.events || 0)), [groups]);

  // Mean of the markers, with a zoom from their spread. react-leaflet 4 has no
  // declarative bounds, and a mean centre is honest about being approximate
  // without needing a map ref.
  const view = useMemo(() => {
    if (!groups.length) return { centre: [20, 0], zoom: 2 };
    const lat = groups.reduce((a, g) => a + g.lat, 0) / groups.length;
    const lng = groups.reduce((a, g) => a + g.lng, 0) / groups.length;
    const spread = Math.max(
      ...groups.map((g) => Math.abs(g.lat - lat)),
      ...groups.map((g) => Math.abs(g.lng - lng)), 0.05);
    return {
      centre: [lat, lng],
      zoom: Math.max(2, Math.min(12, Math.round(Math.log2(180 / spread)) - 1)),
    };
  }, [groups]);

  // Permanent labels only while there are few enough to read. At 1,041 markers
  // they would be a wall of numbers, and the circle's size already carries the
  // magnitude; hovering gives the exact figure.
  const labelled = groups.length <= 120;

  // What the closed button shows, so a filter is never active and invisible.
  const allYears = !range || !data?.years
    || (range.from === data.years.min && range.to === data.years.max);
  const filterLabel = allYears ? 'All years'
    : range.from === range.to ? String(range.from)
    : `${range.from} – ${range.to}`;

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
    <CContainer fluid className="px-0" style={{ position: 'relative' }}>
      {error && (
        <CAlert color="danger" className="m-3" dismissible onClose={() => setError(null)}>
          {error}
        </CAlert>
      )}

      {/* Full bleed. This page IS the map; a card frame would only take space
          from it. The height is the viewport less the header strip. */}
      <div style={{ height: 'calc(100vh - 140px)', minHeight: 420, position: 'relative' }}>
        {groups.length > 0 ? (
          <MapContainer center={view.centre} zoom={view.zoom}
                        style={{ height: '100%', width: '100%' }}>
            <TileLayer
              attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
              url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
            />
            <ZoomWatcher onZoom={onZoom} />
            {groups.map((g, i) => (
              <CircleMarker
                key={`${g.lat.toFixed(5)},${g.lng.toFixed(5)}`}
                center={[g.lat, g.lng]}
                radius={radiusFor(g.events, busiest)}
                pathOptions={{
                  color: '#1a73e8', fillColor: '#1a73e8',
                  fillOpacity: 0.45, weight: 1.5,
                }}
              >
                {labelled && (
                  <Tooltip permanent direction="center" className="airlock-count">
                    {n(g.events)}
                  </Tooltip>
                )}
                {/* One location gets a popup you can act on. Several get none:
                    a popup listing seven places is a worse list than the map
                    already is, so clicking zooms instead. */}
                {g.locations === 1 ? (
                  <Popup>
                    <div style={{ minWidth: 170 }}>
                      <div style={{ fontWeight: 600, marginBottom: 4 }}>
                        {g.name || 'Unnamed location'}
                        {g.verified ? (
                          <CBadge color="success" className="ms-2"
                                  style={{ fontSize: '0.62rem' }}>✓</CBadge>
                        ) : null}
                      </div>
                      <div style={{ fontSize: '0.8rem', color: '#555' }}>
                        {n(g.events)} event{g.events === 1 ? '' : 's'} ·{' '}
                        {n(g.files)} photo{g.files === 1 ? '' : 's'}
                      </div>
                      {g.ids?.[0] && (
                        <button className="btn btn-sm btn-primary mt-2 w-100"
                                onClick={() => navigate(
                                  `/locations/${catalogId}/${g.ids[0]}`)}>
                          Open this location
                        </button>
                      )}
                    </div>
                  </Popup>
                ) : (
                  <Tooltip>
                    {n(g.locations)} locations · {n(g.events)} events
                    <div style={{ fontSize: '0.75rem', opacity: 0.8 }}>
                      zoom in to separate them
                    </div>
                  </Tooltip>
                )}
              </CircleMarker>
            ))}
          </MapContainer>
        ) : (
          <div className="d-flex align-items-center justify-content-center h-100 text-muted"
               style={{ fontSize: '0.9rem' }}>
            {loading ? <CSpinner color="primary" /> : (
              <div className="text-center">
                <div style={{ fontWeight: 600, color: '#495057' }}>
                  Nothing to map yet
                </div>
                <div className="mt-2">
                  {data?.years?.min == null
                    ? <>Run <strong>Extract Locations</strong> and{' '}
                        <strong>Resolve Locations</strong> to group the photos
                        that carry coordinates.</>
                    : 'No locations fall in the selected years.'}
                </div>
              </div>
            )}
          </div>
        )}

        {/* ── The overlay ──────────────────────────────────────────────────
            Dark and translucent, the full height of the map, above Leaflet's
            panes (it uses 400-700; 800 is clear). Shown by default, and it
            hides on request for when the map wants the whole width.

            Full height rather than a box, so there is somewhere to put the
            search the note asks for next without the panel having to grow. */}
        {panelOpen ? (
          <div style={{
            position: 'absolute', top: 0, right: 0, bottom: 0, zIndex: 800,
            width: PANEL_WIDTH, maxWidth: '100%',
            background: 'rgba(18, 20, 24, 0.72)',
            backdropFilter: 'blur(3px)',
            WebkitBackdropFilter: 'blur(3px)',
            borderLeft: '1px solid rgba(255,255,255,0.12)',
            color: 'rgba(255,255,255,0.92)',
            padding: '10px 12px',
            display: 'flex', flexDirection: 'column', gap: 10,
            overflowY: 'auto',
          }}>
            <div className="d-flex justify-content-between align-items-center">
              <strong style={{ fontSize: '0.86rem' }}>Map view</strong>
              <div className="d-flex align-items-center gap-2">
                {loading && <CSpinner size="sm" style={{ color: '#6ea8fe' }} />}
                <button
                  onClick={() => setPanelOpen(false)}
                  title="Hide the panel"
                  style={{
                    background: 'none', border: '1px solid rgba(255,255,255,0.25)',
                    borderRadius: 4, padding: '2px 6px', cursor: 'pointer',
                    lineHeight: 1,
                  }}
                >
                  <CIcon icon={cilChevronRight}
                         style={{ width: 14, height: 14, color: 'rgba(255,255,255,0.85)' }} />
                </button>
              </div>
            </div>

            {data?.years?.min != null && range && (
              <YearRange
                dark
                min={data.years.min} max={data.years.max}
                from={range.from} to={range.to}
                counts={data.years.counts}
                onChange={(from, to) => setRange({ from, to })}
                onReset={() => setRange({ from: data.years.min, to: data.years.max })}
              />
            )}

            <div style={{ borderTop: '1px solid rgba(255,255,255,0.12)',
                          paddingTop: 8, fontSize: '0.78rem',
                          color: 'rgba(255,255,255,0.78)' }}>
              <div>{n(data?.total?.locations)} locations</div>
              <div>{n(data?.total?.events)} events</div>
              <div>{n(data?.total?.files)} photos</div>
              {groups.length > 0 && (
                <div style={{ fontSize: '0.72rem', marginTop: 4,
                              color: 'rgba(255,255,255,0.55)' }}>
                  shown as {n(groups.length)} marker{groups.length === 1 ? '' : 's'}
                </div>
              )}
            </div>

            {/* Deliberately empty. Search and the rest go here, which is why
                the panel is full height before it needs to be. */}
            <div style={{ flex: 1 }} />
          </div>
        ) : (
          /* Closed, it still says what is filtered. A hidden panel with an
             active year range would otherwise be an invisible filter - the map
             showing a third of the catalog with nothing on screen explaining
             why. */
          <button
            onClick={() => setPanelOpen(true)}
            title="Show the panel"
            style={{
              position: 'absolute', top: 12, right: 12, zIndex: 800,
              display: 'flex', alignItems: 'center', gap: 6,
              background: 'rgba(18, 20, 24, 0.72)',
              backdropFilter: 'blur(3px)',
              WebkitBackdropFilter: 'blur(3px)',
              border: '1px solid rgba(255,255,255,0.18)',
              borderRadius: 8, padding: '5px 9px', cursor: 'pointer',
              color: 'rgba(255,255,255,0.92)', fontSize: '0.78rem',
            }}
          >
            <CIcon icon={cilCalendar} style={{ width: 13, height: 13 }} />
            {filterLabel}
            {loading && <CSpinner size="sm" style={{ width: 12, height: 12 }} />}
          </button>
        )}

      </div>
    </CContainer>
  );
};

export default LocationsMap;
