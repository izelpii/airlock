import React from 'react';
import { CButton, CCard, CCardBody, CCardHeader, CSpinner } from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilActionRedo, cilActionUndo } from '@coreui/icons';
import { summary } from '../utils/orientation';

// ─── Basic Editor tools ───────────────────────────────────────────────────────
// The right column of the photo page while editing
// (documentation/specs/basic-photo-editing.md section 3.2). Holds no state of
// its own: Photos.js keeps the pending value and previews it on the image.

const TOOLS = [
  { tool: 'rotate_left', label: 'Rotate left', icon: cilActionUndo, key: '[' },
  { tool: 'rotate_right', label: 'Rotate right', icon: cilActionRedo, key: ']' },
  { tool: 'flip_horizontal', label: 'Flip horizontally', glyph: '⇋' },
  { tool: 'flip_vertical', label: 'Flip vertically', glyph: '⇵' },
];

const BasicEditorTools = ({ value, saved, onTool, onReset, onSave, onCancel, saving, error }) => (
  <CCard>
    <CCardHeader className="py-2"><strong>Basic Editor</strong></CCardHeader>
    <CCardBody className="py-2 d-flex flex-column gap-2">
      {TOOLS.map((t) => (
        <CButton key={t.tool} size="sm" color="primary" variant="outline" disabled={saving}
                 className="d-flex align-items-center" onClick={() => onTool(t.tool)}
                 title={t.key ? `${t.label} (${t.key})` : t.label}>
          {t.icon
            ? <CIcon icon={t.icon} className="me-2" />
            : <span className="me-2" style={{ width: 16, textAlign: 'center' }}>{t.glyph}</span>}
          {t.label}
        </CButton>
      ))}
      <hr className="my-1" />
      <CButton size="sm" color="primary" variant="outline" disabled={saving || value === 1}
               onClick={onReset}>
        Reset to original
      </CButton>
      <div style={{ fontSize: '0.78rem', color: '#666' }}>{summary(value)}</div>
      {error && <div style={{ fontSize: '0.78rem' }} className="text-danger">{error}</div>}
      <hr className="my-1" />
      <div className="d-flex gap-2 justify-content-end">
        <CButton size="sm" color="primary" variant="outline" onClick={onSave}
                 disabled={saving || value === saved}
                 title="Save (Ctrl+S)">
          {saving ? <><CSpinner size="sm" className="me-1" />Saving…</> : 'Save'}
        </CButton>
        <CButton size="sm" color="primary" variant="outline" onClick={onCancel} disabled={saving}
                 title="Cancel (Esc)">
          Cancel
        </CButton>
      </div>
      {saving && (
        <div style={{ fontSize: '0.72rem', color: '#888' }}>
          Rebuilding this photo&apos;s previews and thumbnails…
        </div>
      )}
    </CCardBody>
  </CCard>
);

export default BasicEditorTools;
