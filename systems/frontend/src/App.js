import React, { useState } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate, useParams } from 'react-router-dom';
import { CSpinner } from '@coreui/react';
import '@coreui/coreui/dist/css/coreui.min.css';
import './App.css';
import { CatalogProvider } from './context/CatalogContext';
import { AuthProvider, useAuth } from './context/AuthContext';
import Login from './views/Login';
import Dashboard from './views/Dashboard';
import AppSidebar from './components/Sidebar';
import TopMenu from './components/TopMenu';
import SourceStatusBanner from './components/SourceStatusBanner';
import Workflows from './views/Workflows';
import Albums from './views/Albums';
import Deployments from './views/Deployments';
import AlbumTransfers from './views/AlbumTransfers';
import AlbumTransfer from './views/AlbumTransfer';
import AlbumTransferBatch from './views/AlbumTransferBatch';
import Systems from './views/Systems';
import {
  AlbumEditorWords,
  AlbumEditorTools,
} from './views/AlbumEditor';
import Catalogs from './views/Catalogs';
import Operations from './views/Operations';
import ContentReport from './views/ContentReport';
import Alerts from './views/Alerts';
import ImportProgress from './views/ImportProgress';
import People from './views/People';
import PersonDetail from './views/PersonDetail';
import Pets from './views/Pets';
import PetDetail from './views/PetDetail';
import IdentifyPets from './views/IdentifyPets';
import ConfirmFaceDetections from './views/ConfirmFaceDetections';
import IdentifyGroups from './views/IdentifyGroups';
import Locations from './views/Locations';
import LocationsMap from './views/LocationsMap';
import CalendarView from './views/CalendarView';
import Search from './views/Search';
import SavedSearches from './views/SavedSearches';
import SavedSearch from './views/SavedSearch';
import LocationDetail from './views/LocationDetail';
import Events from './views/Events';
import EventDetail from './views/EventDetail';
import EventDateEditor from './views/EventDateEditor';
import Photos from './views/Photos';
import Videos from './views/Videos';
import RemovedFiles from './views/RemovedFiles';
import PhotoDetail from './views/PhotoDetail';
import Admin from './views/Admin';
import UserSettings from './views/UserSettings';
import SiteSettings from './views/SiteSettings';
import Contents from './views/Contents';
import WorkflowExecutions from './views/WorkflowExecutions';
import Users from './views/Users';

/**
 * The signed-in application.
 *
 * Split out so the gate below can decide whether to mount it at all. Nothing
 * here renders — and so nothing here fetches — until there is a session.
 */
/**
 * The transfer detail page moved from /albums/transfer/:id to
 * /albums/transfers/:id when receiving and sending were grouped under one
 * "Transfers" area. This keeps the old link working, id and all - a transfer
 * view is exactly the kind of URL someone leaves open in a second tab.
 */
function TransferRedirect() {
  const { transferId } = useParams();
  return <Navigate to={`/albums/transfers/${transferId}`} replace />;
}

function AppShell() {
  const [sidebarVisible, setSidebarVisible] = useState(true);

  return (
    <CatalogProvider>
      <div style={{ display: 'flex', minHeight: '100vh', margin: 0, padding: 0 }}>
        <AppSidebar
          visible={sidebarVisible}
          setVisible={setSidebarVisible}
        />
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            flexGrow: 1,
            marginLeft: sidebarVisible ? '280px' : '80px',
            transition: 'margin-left 0.3s ease',
            minHeight: '100vh',
            width: `calc(100% - ${sidebarVisible ? '280px' : '80px'})`,
          }}
        >
          <TopMenu sidebarVisible={sidebarVisible} setSidebarVisible={setSidebarVisible} />
          {/* Renders nothing unless the active catalog indexes files in place
              and cannot currently reach them. */}
          <SourceStatusBanner />
          <div className="wrapper d-flex flex-column flex-grow-1 bg-light" style={{ margin: 0, padding: 0 }}>
            <div className="body flex-grow-1">
              <Routes>
                {/* The Dashboard is home: the bare address, and where an
                    unknown path lands (dashboard-specs.md section 2). Workflows
                    lived at "/" until then. */}
                <Route path="/" element={<Dashboard />} />
                <Route path="/workflows" element={<Workflows />} />
                {/* The canvas replaced the card list; this keeps a bookmark
                    from the experiment working. */}
                <Route path="/workflows-2" element={<Navigate to="/workflows" replace />} />
                {/* Every album in the catalog, wherever it is published.
                    /albums is its own page again: the cards were spread across
                    the deployment rows below, which drew an album once per
                    deployment publishing it. */}
                <Route path="/albums" element={<Albums />} />
                <Route path="/albums/deployments" element={<Deployments />} />
                <Route path="/albums/systems" element={<Systems />} />
                <Route path="/albums/transfers" element={<AlbumTransfers />} />
                <Route path="/albums/transfers/:transferId" element={<AlbumTransfer />} />
                <Route path="/albums/transfers/batches/:batchId" element={<AlbumTransferBatch />} />
                {/* The first spelling of these, kept so a bookmark or an open
                    tab still lands somewhere - the same courtesy /workflows-2
                    gets above. */}
                <Route path="/albums/receive" element={<Navigate to="/albums/transfers" replace />} />
                <Route path="/systems" element={<Navigate to="/albums/systems" replace />} />
                <Route path="/albums/transfer/:transferId" element={<TransferRedirect />} />
                <Route path="/albums/words/:albumId" element={<AlbumEditorWords />} />
                <Route path="/albums/tools/:albumId" element={<AlbumEditorTools />} />
                <Route path="/people" element={<People />} />
                <Route path="/people/:catalogId/:personId" element={<PersonDetail />} />
                {/* Pets are `people` rows with kind='pet', so these mirror the
                    People routes above. Identify pets sits with Identify groups
                    under Recognition: both are "unknown things to name". */}
                <Route path="/pets" element={<Pets />} />
                <Route path="/pets/:catalogId/:petId" element={<PetDetail />} />
                <Route path="/identify-pets" element={<IdentifyPets />} />
                <Route path="/photos" element={<Photos />} />
                <Route path="/photos/:catalogId/:fileId" element={<Photos />} />
                <Route path="/videos" element={<Videos />} />
                <Route path="/removed-files" element={<RemovedFiles />} />
                <Route path="/videos/:catalogId/:fileId" element={<Videos />} />
                <Route path="/identify-people" element={<ConfirmFaceDetections />} />
                <Route path="/identify-groups" element={<IdentifyGroups />} />
                <Route path="/locations" element={<Locations />} />
                <Route path="/locations-map" element={<LocationsMap />} />
                <Route path="/calendar-view" element={<CalendarView />} />
                <Route path="/search" element={<Search />} />
                <Route path="/saved-searches" element={<SavedSearches />} />
                <Route path="/saved-searches/:id" element={<SavedSearch />} />
                <Route path="/locations/:catalogId/:locationId" element={<LocationDetail />} />
                <Route path="/events" element={<Events />} />
                <Route path="/events/:catalogId/:eventId" element={<EventDetail />} />
                {/* Re-date an event's photos in one pass; its own route so Back
                    leaves it and a reload keeps it
                    (mass-edit-event-photo-dates-spec.md section 4.1). */}
                <Route path="/events/:catalogId/:eventId/dates" element={<EventDateEditor />} />
                <Route path="/catalogs/:catalogId/photos/:photoId" element={<PhotoDetail />} />
                <Route path="/catalogs" element={<Catalogs />} />
                <Route path="/operations" element={<Operations />} />
                <Route path="/content-report" element={<ContentReport />} />
                <Route path="/alerts" element={<Alerts />} />
                <Route path="/import-progress" element={<ImportProgress />} />
                {/* "Contents" is what this report gets called, so the name
                    leads to it. The catalog's file inspector - the Files table
                    and its per-file EXIF - keeps its own route below rather
                    than becoming unreachable. */}
                <Route path="/contents" element={<Navigate to="/content-report" replace />} />
                <Route path="/catalog-files" element={<Contents />} />
                <Route path="/workflow-executions" element={<WorkflowExecutions />} />
                <Route path="/admin" element={<Admin />} />
                <Route path="/users" element={<Users />} />
                {/* /storage is gone: storage routes are shown beside the open
                    catalog on the Catalogs page. Redirected rather than dropped,
                    so an old bookmark lands somewhere real. */}
                <Route path="/storage" element={<Navigate to="/catalogs" replace />} />
                {/* The gear: site settings. Your own account is /users/<you>. */}
                <Route path="/settings" element={<SiteSettings />} />
                <Route path="/users/:userId" element={<UserSettings />} />
                {/* Any other path - a typo, a page that moved - goes home
                    rather than to an empty page. */}
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </div>
          </div>
        </div>
      </div>
    </CatalogProvider>
  );
}

/**
 * The gate.
 *
 * A spinner while the boot probe runs, the login screen when there is no
 * session, and the application once there is. See user-authentication-spec.md
 * section 8.1 — and note section 3: this is a usability gate, not the security
 * boundary. The bundle is served before any API call; the enforcement is that
 * every data route answers 401 without a session.
 */
function Gate() {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div style={{
        minHeight: '100vh', display: 'flex', alignItems: 'center',
        justifyContent: 'center', background: '#ebedef',
      }}>
        <CSpinner color="primary" />
      </div>
    );
  }

  // A user carrying must_change_password is handled by Login itself, which
  // shows the forced-change form rather than the sign-in form.
  if (!user || user.must_change_password) {
    return <Login />;
  }

  return <AppShell />;
}

function App() {
  return (
    <Router>
      <AuthProvider>
        <Gate />
      </AuthProvider>
    </Router>
  );
}

export default App;
