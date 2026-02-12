import { io } from 'socket.io-client';

const SOCKET_URL = 'http://localhost:5000';

class SocketService {
  constructor() {
    this.socket = null;
    this.listeners = new Map();
  }

  connect() {
    if (!this.socket) {
      this.socket = io(SOCKET_URL, {
        transports: ['websocket', 'polling'],
      });

      this.socket.on('connect', () => {
        console.log('Socket connected');
      });

      this.socket.on('disconnect', () => {
        console.log('Socket disconnected');
      });

      this.socket.on('workflow_progress', (data) => {
        const callbacks = this.listeners.get('workflow_progress') || [];
        callbacks.forEach(callback => callback(data));
      });
    }
    return this.socket;
  }

  disconnect() {
    if (this.socket) {
      this.socket.disconnect();
      this.socket = null;
      this.listeners.clear();
    }
  }

  onWorkflowProgress(callback) {
    if (!this.listeners.has('workflow_progress')) {
      this.listeners.set('workflow_progress', []);
    }
    this.listeners.get('workflow_progress').push(callback);
    
    // Return unsubscribe function
    return () => {
      const callbacks = this.listeners.get('workflow_progress') || [];
      const index = callbacks.indexOf(callback);
      if (index > -1) {
        callbacks.splice(index, 1);
      }
    };
  }
}

const socketService = new SocketService();
export default socketService;

// Made with Bob
