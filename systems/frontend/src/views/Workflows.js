import React, { useState, useEffect, useCallback } from 'react';
import { CContainer, CAlert, CButton } from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilPlus } from '@coreui/icons';
import WorkflowQueue from '../components/WorkflowQueue';
import WorkflowCanvas from '../components/WorkflowCanvas';
import WorkflowForm from '../components/WorkflowForm';
import { useCatalog } from '../context/CatalogContext';
import { queueAPI, workflowAPI } from '../services/api';

// ─── Workflows ────────────────────────────────────────────────────────────────
// The queue on top - "what is happening now" - and below it the pipeline drawn
// as it actually runs: "what runs after what, and why". This replaced a list of
// cards, which could show every workflow but had no way to show an ordering.
// The catalog's backups are drawn in the same picture, in a row of their own and
// connected to nothing (WorkflowCanvas); they used to be a card of their own
// above it.
//
// Everything the cards could do is on a node: run, queue, edit, remove.
const Workflows = () => {
  const { activeCatalog } = useCatalog();
  const [notice, setNotice] = useState(null);
  const [error, setError] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [editingWorkflow, setEditingWorkflow] = useState(null);
  // The canvas reloads on its own every few seconds, but a create or a delete
  // should land immediately rather than after the next poll.
  const [refreshKey, setRefreshKey] = useState(0);
  // Default workflows this catalog does not have, as a FALLBACK: the backend
  // adds them when this page loads its graph (workflow_defaults
  // .sync_default_workflows), so this normally finds nothing and shows nothing.
  // It exists for the case where that sync could not write, which it is built
  // to survive rather than fail on.
  const [missing, setMissing] = useState([]);
  const [noImport, setNoImport] = useState(false);
  const [seeding, setSeeding] = useState(false);

  const checkMissing = useCallback(async () => {
    if (!activeCatalog) { setMissing([]); setNoImport(false); return; }
    try {
      const [manifest, graph] = await Promise.all([
        workflowAPI.getDefaults(),
        workflowAPI.graph(activeCatalog.id),
      ]);
      const nodes = graph.data?.nodes || [];
      const have = new Set(nodes.map((n) => n.script));
      const entries = manifest.data || [];
      // `default` and `ingestion` are both load-bearing here, and ignoring them
      // is what made this banner lie: it listed "Ingest Files" as a missing
      // default, when an import workflow is NOT seeded at all. One is created
      // per STORAGE ROUTE, named after it, by
      // _create_import_workflow_for_route - which is why both import entries
      // are marked default:false in the manifest. Seeding one would produce a
      // workflow with no route that cannot import anything.
      setMissing(entries.filter(
        (d) => d.default && !d.ingestion && !have.has(d.script)));
      // A catalog with no import workflow is a real problem with a different
      // fix, so it gets its own notice rather than being folded into the one
      // above.
      setNoImport(nodes.length > 0
                  && !entries.some((d) => d.ingestion && have.has(d.script)));
    } catch (e) {
      // A check, not a feature: if it cannot run, say nothing rather than
      // putting an error on a page that is otherwise working.
      setMissing([]);
      setNoImport(false);
    }
  }, [activeCatalog]);

  useEffect(() => { checkMissing(); }, [checkMissing, refreshKey]);

  const seedDefaults = async () => {
    setSeeding(true);
    setError(null);
    try {
      const res = await workflowAPI.seedDefaults(activeCatalog.id);
      const created = res.data?.created || [];
      setNotice(created.length
        ? `Added ${created.join(', ')}. They are in the pipeline below now, and `
          + 'will run in order with everything else.'
        : 'Nothing to add — this catalog already has every default workflow.');
      setRefreshKey((k) => k + 1);
    } catch (e) {
      setNotice(null);
      setError(e?.response?.data?.error || 'Could not add the default workflows.');
    } finally {
      setSeeding(false);
    }
  };

  // Both buttons go through the QUEUE, exactly as the workflow cards do. A
  // direct workflowAPI.execute() would run it, but with no queue row its
  // progress would appear nowhere - the queue panel above is the one place a
  // run is watched, so it is the one way in.
  //
  // The only difference between them is run_now: front of the queue and started
  // on this scheduling pass, versus the ordinary position.
  const enqueue = async (node, runNow) => {
    if (node.needs_configuration) {
      setError(`"${node.name}" has no source configured, so it would import `
               + 'nothing. Give it a storage route first.');
      setNotice(null);
      return;
    }
    setError(null);
    try {
      await queueAPI.enqueue(
        activeCatalog.id,
        node.id,
        // A route beats a literal path, and the backend resolves it - so an
        // unavailable folder is refused here, naming the route, rather than
        // becoming an import of nothing.
        node.source_route_id
          ? { source_route_id: Number(node.source_route_id),
              source_subpath: node.source_subpath || '' }
          : node.source_directory,
        runNow,
      );
      setNotice(runNow
        ? `Started "${node.name}". Watch the queue above — anything that depends `
          + 'on it will follow automatically.'
        : `Queued "${node.name}". It will start when the workflows before it finish.`);
    } catch (e) {
      setNotice(null);
      setError(e?.response?.data?.error
               || `Could not ${runNow ? 'start' : 'queue'} "${node.name}".`);
    }
  };

  // The canvas has the workflow's id and name; the form wants the whole row, so
  // it is fetched here rather than widening the graph payload for one screen.
  const edit = async (node) => {
    setError(null);
    try {
      const res = await workflowAPI.getById(activeCatalog.id, node.id);
      setEditingWorkflow(res.data);
      setShowForm(true);
    } catch (e) {
      setError(e?.response?.data?.error || `Could not open "${node.name}".`);
    }
  };

  const remove = async (node) => {
    if (!window.confirm(
      `Remove the workflow "${node.name}"?\n\n`
      + 'Its past executions are kept; nothing it has already imported is touched.'
    )) return;
    setError(null);
    try {
      await workflowAPI.delete(activeCatalog.id, node.id);
      setNotice(`Removed "${node.name}".`);
      setRefreshKey((k) => k + 1);
    } catch (e) {
      setNotice(null);
      setError(e?.response?.data?.error || `Could not remove "${node.name}".`);
    }
  };

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
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Workflows</h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            Your pipeline, arranged by the order it runs in
          </div>
        </div>
        <div className="d-flex gap-2">
          {missing.length > 0 && (
            <CButton color="success" variant="outline" disabled={seeding}
                     title={`Not in this catalog yet: ${missing.map((m) => m.name).join(', ')}`}
                     onClick={seedDefaults}>
              <CIcon icon={cilPlus} className="me-2" />
              {seeding ? 'Adding…' : `Add ${missing.length} default workflow${missing.length === 1 ? '' : 's'}`}
            </CButton>
          )}
          <CButton color="primary"
                   onClick={() => { setEditingWorkflow(null); setShowForm(true); }}>
            <CIcon icon={cilPlus} className="me-2" />
            Create Workflow
          </CButton>
        </div>
      </div>

      {/* Named rather than left to the button's tooltip: a workflow this
          catalog does not have is one that silently never runs, and the
          pipeline below looks complete without it. */}
      {missing.length > 0 && (
        <CAlert color="info" style={{ fontSize: '0.85rem' }}>
          This catalog is missing {missing.length} default workflow
          {missing.length === 1 ? '' : 's'}:{' '}
          <strong>{missing.map((m) => m.name).join(', ')}</strong>. These are
          normally added for you; if this persists, adding them here will do it.
        </CAlert>
      )}

      {/* A different problem with a different fix. Import workflows are not
          defaults and are never seeded: the catalog gets one per storage
          route, named after it, because a route is where the files come from
          and a workflow without one has nothing to read. */}
      {noImport && (
        <CAlert color="warning" style={{ fontSize: '0.85rem' }}>
          This catalog has no import workflow, so nothing brings files into it.
          Add a <strong>storage route</strong> — on the Catalogs page, beside
          this catalog — and it gets its own import workflow, named after the
          route and wired into the pipeline below. There is no Storage page to
          go to: a route belongs to one catalog, so the routes are shown with
          it.
        </CAlert>
      )}

      <div className="mb-4">
        <WorkflowQueue catalogId={activeCatalog.id} />
      </div>

      {notice && (
        <CAlert color="success" dismissible onClose={() => setNotice(null)}
                style={{ fontSize: '0.85rem' }}>
          {notice}
        </CAlert>
      )}
      {error && (
        <CAlert color="warning" dismissible onClose={() => setError(null)}
                style={{ fontSize: '0.85rem' }}>
          {error}
        </CAlert>
      )}

      <WorkflowCanvas
        key={`${activeCatalog.id}-${refreshKey}`}
        catalogId={activeCatalog.id}
        onRun={(n) => enqueue(n, true)}
        onQueue={(n) => enqueue(n, false)}
        onEdit={edit}
        onDelete={remove}
        canManageBackups={!activeCatalog.role || activeCatalog.role === 'owner'}
      />

      <WorkflowForm
        visible={showForm}
        onClose={() => { setShowForm(false); setEditingWorkflow(null); }}
        workflow={editingWorkflow}
        onSuccess={() => setRefreshKey((k) => k + 1)}
        catalogId={activeCatalog.id}
      />
    </CContainer>
  );
};

export default Workflows;
