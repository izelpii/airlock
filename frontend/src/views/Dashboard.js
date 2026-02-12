import React, { useState } from 'react';
import { CContainer, CRow, CCol } from '@coreui/react';
import WorkflowList from '../components/WorkflowList';
import WorkflowForm from '../components/WorkflowForm';
import WorkflowExecutionCard from '../components/WorkflowExecutionCard';

const Dashboard = () => {
  const [showForm, setShowForm] = useState(false);
  const [selectedWorkflow, setSelectedWorkflow] = useState(null);
  const [executingWorkflows, setExecutingWorkflows] = useState([]);
  const [refreshKey, setRefreshKey] = useState(0);

  const handleCreate = () => {
    setSelectedWorkflow(null);
    setShowForm(true);
  };

  const handleEdit = (workflow) => {
    setSelectedWorkflow(workflow);
    setShowForm(true);
  };

  const handleExecute = (workflow) => {
    // Add workflow to executing list with unique key
    const executionKey = `${workflow.id}-${Date.now()}`;
    setExecutingWorkflows((prev) => [
      ...prev,
      { ...workflow, executionKey }
    ]);
  };

  const handleFormClose = () => {
    setShowForm(false);
    setSelectedWorkflow(null);
  };

  const handleFormSuccess = () => {
    setRefreshKey((prev) => prev + 1);
  };

  const handleExecutionClose = (executionKey) => {
    setExecutingWorkflows((prev) =>
      prev.filter((w) => w.executionKey !== executionKey)
    );
  };

  return (
    <CContainer fluid className="py-4">
      <CRow>
        <CCol>
          <h1 className="mb-4">Workflow Manager</h1>
          <WorkflowList
            key={refreshKey}
            onEdit={handleEdit}
            onCreate={handleCreate}
            onExecute={handleExecute}
          />
        </CCol>
      </CRow>

      {executingWorkflows.length > 0 && (
        <CRow className="mt-4">
          <CCol>
            <h3 className="mb-3">Active Executions</h3>
            {executingWorkflows.map((workflow) => (
              <WorkflowExecutionCard
                key={workflow.executionKey}
                workflow={workflow}
                onClose={() => handleExecutionClose(workflow.executionKey)}
              />
            ))}
          </CCol>
        </CRow>
      )}

      <WorkflowForm
        visible={showForm}
        onClose={handleFormClose}
        workflow={selectedWorkflow}
        onSuccess={handleFormSuccess}
      />
    </CContainer>
  );
};

export default Dashboard;

// Made with Bob
