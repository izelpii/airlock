import { io } from 'socket.io-client';
import { SOCKET_URL } from '../config';

class SocketService {
  constructor() {
    this.socket = null;
    this.listeners = new Map();
  }

  connect() {
    if (!this.socket) {
      // POLLING FIRST, and it is not a downgrade.
      //
      // The backend is gunicorn `gthread` with async_mode='threading', which
      // cannot serve websockets at all - only HTTP long-polling. Asking for a
      // websocket first meant every connection began with an upgrade that could
      // never succeed, leaving the client to fall back. Through nginx over TLS
      // that fallback does not reliably happen, and the symptom is the whole
      // point of this connection failing silently: progress bars that never
      // move while the work itself runs fine.
      //
      // Listing what the server can actually do removes the round trip and the
      // dependency on fallback behaviour. If the backend ever moves to an
      // eventlet/gevent worker, put 'websocket' back in front.
      // `|| undefined` is load-bearing. SOCKET_URL is '' by default, meaning
      // "the page's own host" - but socket.io only falls back to the page
      // origin when the URL is null or undefined. An empty string is a string,
      // so it gets parsed: '' has no scheme, so the client prepends the page
      // protocol and arrives at "https://" - a URL with an empty host. Passing
      // undefined asks for the documented default instead of a URL that only
      // resembles it.
      this.socket = io(SOCKET_URL || undefined, {
        transports: ['polling'],
        withCredentials: true,
      });

      this.socket.on('connect', () => {
        console.log('Socket connected');
      });

      this.socket.on('disconnect', () => {
        console.log('Socket disconnected');
      });

      // One dispatcher per server event. Adding an event means adding it to
      // this list and giving it an on<Event> subscriber below.
      ['workflow_progress', 'queue_updated',
       'transfer_progress', 'transfer_file', 'transfer_done',
       'transfer_files_ready', 'date_batch_progress', 'alerts_updated'].forEach((event) => {
        this.socket.on(event, (data) => {
          const callbacks = this.listeners.get(event) || [];
          callbacks.forEach((callback) => callback(data));
        });
      });
    }
    return this.socket;
  }

  _subscribe(event, callback) {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, []);
    }
    this.listeners.get(event).push(callback);
    return () => {
      const callbacks = this.listeners.get(event) || [];
      const index = callbacks.indexOf(callback);
      if (index > -1) {
        callbacks.splice(index, 1);
      }
    };
  }

  disconnect() {
    if (this.socket) {
      this.socket.disconnect();
      this.socket = null;
      this.listeners.clear();
    }
  }

  // One subscriber per transfer event. Album transfers stream far more events
  // than workflows do, so a view subscribes to exactly what it draws rather
  // than re-rendering on everything.
  onTransferProgress(callback) {
    return this._subscribe('transfer_progress', callback);
  }

  onTransferFile(callback) {
    return this._subscribe('transfer_file', callback);
  }

  onTransferDone(callback) {
    return this._subscribe('transfer_done', callback);
  }

  onTransferFilesReady(callback) {
    return this._subscribe('transfer_files_ready', callback);
  }

  // Storage alerts changed: {active, critical, unacknowledged}.
  onAlertsUpdated(callback) {
    return this._subscribe('alerts_updated', callback);
  }

  // A date batch's progress: {catalog_id, batch_id, done, total, status}.
  // The event page also polls the batch, because a missed event must not
  // leave its banner stuck (mass-edit-event-photo-dates-spec.md section 4.5).
  onDateBatchProgress(callback) {
    return this._subscribe('date_batch_progress', callback);
  }

  onWorkflowProgress(callback) {
    return this._subscribe('workflow_progress', callback);
  }

  // Fired whenever the queue changes: an item started, finished, was cancelled,
  // or a trigger enqueued a chain. Carries the whole queue, so the subscriber
  // replaces its list rather than reconciling a delta.
  onQueueUpdated(callback) {
    return this._subscribe('queue_updated', callback);
  }
}

const socketService = new SocketService();
export default socketService;
