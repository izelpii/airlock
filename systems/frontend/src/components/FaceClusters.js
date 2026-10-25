import React, { useState, useEffect, useCallback } from 'react';
import {
  CCard, CCardBody, CButton, CBadge, CSpinner, CFormSelect, CRow, CCol,
} from '@coreui/react';
import { faceRecognitionAPI } from '../services/api';
import FaceThumbnail from './FaceThumbnail';

// ─── Cluster review ───────────────────────────────────────────────────────────
// Groups of unidentified faces the contextual workflow judged to be the same
// person, so one confirmation can label the whole group.
//
// The spread is shown rather than hidden. Connected-component clustering can
// chain two different people together through one ambiguous face in between, and
// when that happens the cluster looks just as confident as a good one. A high
// max_member_distance is the only visible symptom, so it is surfaced as a warning
// and the reviewer can uncheck members before assigning.
const CLUSTER_SPREAD_WARN = 0.45;   // CLUSTER_MAX_SPREAD in ai_contextual_recognition_workflow.py

const ClusterCard = ({ cluster, people, catalogId, onAssigned, minSimilarity = 0 }) => {
  const [personId, setPersonId] = useState('');
  // The server's best match for this cluster's centroid. A hint only — see the
  // comment where it is rendered.
  //
  // There is ALWAYS a nearest person, so an unfiltered suggestion is sometimes
  // just noise wearing a name. The minimum-similarity slider is the floor: below
  // it the hint is hidden rather than shown quietly, because a name on screen
  // reads as advice however it is styled.
  const suggestion =
    cluster.suggested_person_id &&
    (cluster.suggested_similarity ?? 0) >= minSimilarity
      ? {
          id: cluster.suggested_person_id,
          name: cluster.suggested_person_name,
          similarity: cluster.suggested_similarity,
        }
      : null;
  const [excluded, setExcluded] = useState(() => new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  const toggle = (faceId) => {
    setExcluded((prev) => {
      const next = new Set(prev);
      if (next.has(faceId)) next.delete(faceId);
      else next.add(faceId);
      return next;
    });
  };

  const members = cluster.members || [];
  const willAssign = members.filter((m) => !excluded.has(m.face_detection_id)).length;
  const spread = cluster.max_member_distance;
  const mixed = spread != null && spread > CLUSTER_SPREAD_WARN;

  const assign = async () => {
    if (!personId) return;
    setBusy(true);
    setError(null);
    try {
      const r = await faceRecognitionAPI.assignCluster(
        catalogId, cluster.id, Number(personId), [...excluded],
      );
      setResult(r.data);
      onAssigned(cluster.id, r.data);
    } catch (e) {
      // Cluster ids are reassigned every time the clustering workflow re-runs,
      // so a page left open across a run will post an id that no longer exists.
      // "Cluster not found" is true but useless; say what to do about it.
      if (e?.response?.status === 404) {
        setError('This group no longer exists — the groups were rebuilt since this '
                 + 'page was loaded. Reload to see the current ones.');
      } else {
        setError(e?.response?.data?.error || 'Could not assign this cluster.');
      }
    } finally {
      setBusy(false);
    }
  };

  if (result) {
    // The endpoint returns 200 with success:true whether it assigned twenty
    // faces or none, so this has to read `assigned` rather than the status.
    // Reporting "Assigned 0 faces" in green - and then removing the card -
    // is what made a no-op look like a success and the page look frozen.
    const nothingHappened = !result.assigned;
    return (
      <div style={{
        border: `1px solid ${nothingHappened ? '#e6a817' : '#dee2e6'}`,
        borderRadius: 8, padding: 12, marginBottom: 12,
      }}>
        {nothingHappened ? (
          <>
            <span style={{ color: '#b8860b', fontWeight: 600, fontSize: '0.85rem' }}>
              Nothing was tagged.
            </span>
            <div style={{ fontSize: '0.78rem', color: '#666', marginTop: 4 }}>
              {result.skipped?.length > 0 && (
                <div>
                  {result.skipped.length} face{result.skipped.length === 1 ? '' : 's'} already
                  belong to someone — an existing name is never overwritten.
                </div>
              )}
              {result.failed?.length > 0 && (
                <div>
                  {result.failed.length} face{result.failed.length === 1 ? '' : 's'} could not be
                  tagged: {[...new Set(result.failed.map((f) => f.error))].join('; ')}
                </div>
              )}
              {!result.skipped?.length && !result.failed?.length && (
                <div>
                  This group has no faces left to tag. It may have been regrouped
                  since the page was loaded — reload to see the current groups.
                </div>
              )}
            </div>
          </>
        ) : (
          <>
            <span style={{ color: '#198754', fontWeight: 600, fontSize: '0.85rem' }}>
              Assigned {result.assigned} face{result.assigned === 1 ? '' : 's'} to {result.person_name}
            </span>
            {result.skipped?.length > 0 && (
              <span style={{ color: '#888', fontSize: '0.78rem' }}>
                {' '}· {result.skipped.length} left alone (already named)
              </span>
            )}
            {result.failed?.length > 0 && (
              <div style={{ fontSize: '0.78rem', color: '#b8860b', marginTop: 4 }}>
                {result.failed.length} could not be tagged:{' '}
                {[...new Set(result.failed.map((f) => f.error))].join('; ')}
              </div>
            )}
          </>
        )}
      </div>
    );
  }

  return (
    <div style={{
      border: `1px solid ${mixed ? '#e6a817' : '#dee2e6'}`,
      borderRadius: 8, padding: 12, marginBottom: 12,
    }}>
      {/* Two columns. Widths come from .cluster-info-col / .cluster-faces-col in
          App.css rather than CCol props: the left column is 70% of the md=4 /
          lg=3 it started from, and the grid cannot express 2.1 of 12 columns.
          xs={12} still stacks them on a narrow screen. */}
      <CRow className="g-3" style={{ alignItems: 'flex-start' }}>
        <CCol xs={12} className="cluster-info-col">
          {/* Inside the left column: what the cluster IS on the left two
              thirds, what you DO with it on the right third, both starting at
              the title's height. */}
          <CRow className="g-2">
            <CCol xs={12} sm={8}>
              <div>
                <strong style={{ fontSize: '0.85rem' }}>Cluster #{cluster.id}</strong>
                <CBadge color="secondary" className="ms-2">{cluster.member_count} faces</CBadge>
              </div>
              <div style={{ fontSize: '0.75rem', color: '#888', marginTop: 2 }}>
                cohesion {cluster.cohesion?.toFixed(2) ?? '—'} · spread {spread?.toFixed(2) ?? '—'}
              </div>

              {/* The likeliest person, offered rather than applied. Clicking
                  fills the select; the reviewer still presses Assign.
                  Auto-selecting it would turn a deliberate act into a default,
                  and a mixed cluster looks just as confident as a good one. */}
              {suggestion && (
                <div style={{ fontSize: '0.78rem', marginTop: 6 }}>
                  <span style={{ color: '#888' }}>Looks like </span>
                  <button
                    type="button"
                    onClick={() => setPersonId(String(suggestion.id))}
                    disabled={busy}
                    title="Use this suggestion"
                    style={{
                      background: 'none', border: 'none', padding: 0,
                      color: '#0d6efd',
                      fontWeight: 600,
                      textDecoration: 'underline', cursor: 'pointer',
                      textAlign: 'left',
                    }}
                  >
                    {suggestion.name}
                  </button>
                  <span style={{ color: '#888' }}> ({suggestion.similarity}%)</span>
                </div>
              )}
            </CCol>

            <CCol xs={12} sm={4}>
              <CFormSelect
                size="sm" value={personId}
                onChange={(e) => setPersonId(e.target.value)}
                disabled={busy}
                title="Assign all to…"
              >
                <option value="">Assign all to…</option>
                {people.map((p) => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </CFormSelect>
              <CButton
                size="sm" color="primary" className="mt-2 w-100"
                disabled={!personId || busy || willAssign === 0}
                onClick={assign}
              >
                {busy ? <CSpinner size="sm" /> : `Assign ${willAssign}`}
              </CButton>
            </CCol>
          </CRow>

          {mixed && (
            <div style={{ fontSize: '0.75rem', color: '#a97a00', marginTop: 8 }}>
              Two members are {spread.toFixed(2)} apart — wider than a single
              person usually is. This group may have chained two people together;
              uncheck any face that does not belong before assigning.
            </div>
          )}

          {error && (
            <div style={{ fontSize: '0.75rem', color: '#dc3545', marginTop: 8 }}>{error}</div>
          )}
        </CCol>

        <CCol xs={12} className="cluster-faces-col">
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {members.map((m) => {
          const off = excluded.has(m.face_detection_id);
          return (
            <div
              key={m.face_detection_id}
              onClick={() => toggle(m.face_detection_id)}
              title={`${m.file_name} · ${m.distance_to_centroid?.toFixed(2)} from centre`}
              style={{
                cursor: 'pointer', textAlign: 'center',
                opacity: off ? 0.35 : 1,
                border: `2px solid ${off ? '#dc3545' : 'transparent'}`,
                borderRadius: 6, padding: 2,
              }}
            >
              <FaceThumbnail
                face={{
                  ai_crop_path: m.ai_crop_path, file_path: m.file_path,
                  x: m.x, y: m.y, w: m.w, h: m.h,
                  image_width: m.image_width, image_height: m.image_height,
                }}
                size={64}
              />
              <div style={{ fontSize: '0.65rem', color: '#888' }}>
                {m.distance_to_centroid?.toFixed(2)}
              </div>
            </div>
          );
        })}
      </div>

      {cluster.members_truncated && (
        <div style={{ fontSize: '0.72rem', color: '#888', marginTop: 4 }}>
          Showing {members.length} of {cluster.member_count}. Assigning covers all
          {' '}{cluster.member_count}, minus anything unchecked above.
        </div>
      )}
        </CCol>
      </CRow>
    </div>
  );
};

// `collapsible` is what the old inline panel needed: it shared the Identify
// page with the candidate queue and had to stay out of the way. On its own
// page the groups ARE the content, so it defaults to always open with no
// Review button to press.
// Groups a page at a time, largest first. `family` has 9,479 of them, and
// fetching every one with its faces was 15.5 MB and ~227,000 thumbnails -
// most of a minute during which the panel said "0 groups". The totals come
// from the server and describe all of them, not the page.
const GROUP_PAGE = 50;

export const ClustersPanel = ({
  catalogId, people, onAssigned, collapsible = false, minSize,
  minSimilarity = 0, emptyMessage = null,
}) => {
  const [clusters, setClusters] = useState([]);
  const [totals, setTotals] = useState({ groups: 0, faces: 0 });
  const [offset, setOffset] = useState(0);
  // Null until the first answer: "no groups" is only said once it is known.
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(!collapsible);

  const load = useCallback(async (at) => {
    if (!catalogId) return;
    setLoading(true);
    try {
      const params = { limit: GROUP_PAGE, offset: at };
      if (minSize) params.min_size = minSize;
      const r = await faceRecognitionAPI.getClusters(catalogId, params);
      setClusters((r.data.clusters || []).filter((c) => !c.resolved_person_id));
      setTotals({ groups: r.data.total || 0, faces: r.data.total_faces || 0 });
      setOffset(r.data.offset ?? at);
    } catch {
      setClusters([]);
      setTotals({ groups: 0, faces: 0 });
    } finally {
      setLoading(false);
      setLoaded(true);
    }
  }, [catalogId, minSize]);

  useEffect(() => { load(0); }, [load]);

  const handleAssigned = (clusterId, data) => {
    onAssigned?.(data);
    // Retire the card ONLY if the group is done: faces were tagged, or - the
    // server resolves it then too - every face already had a person. Removing
    // it after a no-op told the user the group was dealt with when the
    // database was unchanged, so it came back on the next load.
    if (data?.assigned > 0 || data?.resolved) {
      const size = clusters.find((c) => c.id === clusterId)?.member_count || 0;
      setTimeout(() => {
        setClusters((prev) => prev.filter((c) => c.id !== clusterId));
        setTotals((t) => ({ groups: Math.max(0, t.groups - 1),
                            faces: Math.max(0, t.faces - size) }));
      }, 1500);
    }
  };

  // A page named down to nothing, with more waiting: the server's list now
  // continues at this same offset, so ask for it again.
  useEffect(() => {
    if (loaded && !loading && clusters.length === 0 && totals.groups > offset) {
      load(offset);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clusters.length]);

  if (!loaded) {
    return <div className="text-center py-5"><CSpinner color="primary" /></div>;
  }

  // Inline on another page, an empty panel is noise, so it renders nothing.
  // On its own page that would leave a blank screen with no explanation.
  if (!loading && totals.groups === 0) {
    return emptyMessage || null;
  }

  const page = Math.floor(offset / GROUP_PAGE) + 1;
  const pages = Math.max(page, Math.ceil(totals.groups / GROUP_PAGE));
  // Named groups have left the server's list, so the next page starts where
  // what is still on this one ends.
  const next = () => load(offset + clusters.length);
  const prev = () => load(Math.max(0, offset - GROUP_PAGE));
  const pager = pages > 1 && (
    <div className="d-flex align-items-center gap-2">
      <CButton size="sm" color="light" disabled={loading || offset === 0} onClick={prev}>
        ‹ Prev
      </CButton>
      <span style={{ fontSize: '0.82rem', color: '#555' }}>{page} / {pages}</span>
      <CButton size="sm" color="light" disabled={loading || page >= pages} onClick={next}>
        Next ›
      </CButton>
    </div>
  );

  return (
    <CCard className="mb-3">
      <CCardBody className="py-3">
        <div className="d-flex align-items-center justify-content-between">
          <div>
            <strong style={{ fontSize: '0.9rem' }}>Faces grouped as the same person</strong>
            <CBadge color="secondary" className="ms-2">{totals.groups.toLocaleString()}</CBadge>
            <div style={{ fontSize: '0.78rem', color: '#888', marginTop: 2 }}>
              {totals.faces.toLocaleString()} unidentified face{totals.faces === 1 ? '' : 's'} in
              {' '}{totals.groups.toLocaleString()} group{totals.groups === 1 ? '' : 's'}.
              {' '}Naming a group labels every face in it.
            </div>
          </div>
          <div className="d-flex align-items-center gap-2">
            {pager}
            {collapsible && (
              <CButton size="sm" color="secondary" variant="outline"
                       onClick={() => setOpen((o) => !o)}>
                {open ? 'Hide' : 'Review'}
              </CButton>
            )}
          </div>
        </div>

        {open && (loading ? (
          <div className="text-center py-5"><CSpinner color="primary" /></div>
        ) : (
          <div className="mt-3">
            {clusters.map((c) => (
              <ClusterCard
                key={c.id} cluster={c} people={people} catalogId={catalogId}
                onAssigned={handleAssigned} minSimilarity={minSimilarity}
              />
            ))}
            {pager && <div className="d-flex justify-content-center mt-3">{pager}</div>}
          </div>
        ))}
      </CCardBody>
    </CCard>
  );
};


export default ClustersPanel;
