import axios from 'axios';

const API_BASE_URL = 'http://localhost:5000/api';

const api = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

export const workflowAPI = {
  // Get all workflows
  getAll: () => api.get('/workflows'),
  
  // Get a specific workflow
  getById: (id) => api.get(`/workflows/${id}`),
  
  // Create a new workflow
  create: (workflow) => api.post('/workflows', workflow),
  
  // Update a workflow
  update: (id, workflow) => api.put(`/workflows/${id}`, workflow),
  
  // Delete a workflow
  delete: (id) => api.delete(`/workflows/${id}`),
  
  // Execute a workflow
  execute: (id) => api.post(`/workflows/${id}/execute`),
  
  // Get workflow executions
  getExecutions: (id) => api.get(`/workflows/${id}/executions`),
};

export const executionAPI = {
  // Get execution status
  getById: (id) => api.get(`/executions/${id}`),
};

export const catalogAPI = {
  // Get all catalogs
  getAll: () => api.get('/catalogs'),
  
  // Get a specific catalog with all related data
  getById: (id) => api.get(`/catalogs/${id}`),
  
  // Create a new catalog
  create: (catalog) => api.post('/catalogs', catalog),
  
  // Update a catalog
  update: (id, catalog) => api.put(`/catalogs/${id}`, catalog),
  
  // Delete a catalog
  delete: (id) => api.delete(`/catalogs/${id}`),
  
  // Add a file to catalog
  addFile: (id, file) => api.post(`/catalogs/${id}/files`, file),
  
  // Add an operation to catalog
  addOperation: (id, operation) => api.post(`/catalogs/${id}/operations`, operation),
  
  // Add a workflow execution to catalog
  addWorkflowExecution: (id, execution) => api.post(`/catalogs/${id}/workflow-executions`, execution),
};

export default api;

// Made with Bob
