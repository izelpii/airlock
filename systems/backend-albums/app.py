import os
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

MODEL = os.environ.get('ALBUMS_LLM_MODEL', 'granite3.3:8b')
OLLAMA_HOST = os.environ.get('OLLAMA_HOST', 'http://ollama.airlock.lan')


def _call_ollama(prompt: str) -> str:
    """Send a prompt to Ollama and return the response text."""
    try:
        import ollama
        # Addressed explicitly, the way backend-vision does it. Relying on the
        # library to pick OLLAMA_HOST out of the environment works, but it means
        # this service's dependency on Ollama is invisible in the source - and a
        # missing env var then looks like Ollama being down.
        client = ollama.Client(host=OLLAMA_HOST)
        response = client.generate(model=MODEL, prompt=prompt, options={'temperature': 0.1})
        return response.get('response', '').strip()
    except Exception as e:
        raise RuntimeError(f'Ollama error: {e}')


def _extract_sql(raw: str) -> str:
    """
    Pull only the WHERE clause body from the model output.
    The model is instructed to return just the predicate, but sometimes it
    wraps output in markdown fences or adds a leading 'WHERE'.
    """
    # Strip markdown code fences
    raw = re.sub(r'```[a-zA-Z]*', '', raw).replace('```', '').strip()
    # Strip a leading WHERE keyword
    raw = re.sub(r'(?i)^\s*WHERE\s+', '', raw).strip()
    # Remove any trailing semicolons
    raw = raw.rstrip(';').strip()
    return raw


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'model': MODEL})


@app.route('/translate', methods=['POST'])
def translate():
    """
    Translate a natural-language photo filter description into a SQLite
    WHERE clause body that operates on the per-catalog workflow_results.db.

    Request JSON:
    {
      "nl_text": "Photos of all events that include Izelpi in 2025",
      "schema_hint": "...",          // optional — override the default schema
      "previous_filters": ["...", ...]  // already-applied SQL fragments
    }

    Response JSON:
    {
      "sql_fragment": "<WHERE clause body>",
      "explanation": "..."
    }
    """
    data = request.json or {}
    nl_text = data.get('nl_text', '').strip()
    if not nl_text:
        return jsonify({'error': 'nl_text is required'}), 400

    schema_hint = data.get('schema_hint') or DEFAULT_SCHEMA_HINT
    previous_filters = data.get('previous_filters', [])

    previous_section = ''
    if previous_filters:
        joined = '\n'.join(f'  {i+1}. {f}' for i, f in enumerate(previous_filters))
        previous_section = f"""
Previously applied filters (already narrowing the set — your new filter is ADDITIONAL):
{joined}
"""

    prompt = f"""You are a SQLite query expert. Your job is to translate a natural-language
photo filter description into a valid SQLite WHERE clause body.

IMPORTANT RULES:
- Return ONLY the WHERE clause body — no SELECT, no FROM, no WHERE keyword, no semicolon.
- Use only the tables and columns listed in the schema below.
- Use SQLite syntax (strftime for dates, LIKE for text search, subqueries for joins).
- The base query always has file_contents aliased as fc and exif_data aliased as ed.
- If you need to join additional tables, use subquery patterns (e.g. fc.id IN (SELECT ...)).
- If the request is ambiguous or cannot be mapped to the schema, return: 1=1

DATABASE SCHEMA:
{schema_hint}
{previous_section}
FILTER REQUEST: {nl_text}

SQL WHERE CLAUSE BODY (no WHERE keyword, no semicolon):"""

    try:
        raw = _call_ollama(prompt)
        sql_fragment = _extract_sql(raw)
        if not sql_fragment:
            sql_fragment = '1=1'
        return jsonify({
            'sql_fragment': sql_fragment,
            'explanation': f'Generated from: {nl_text}',
        })
    except Exception as e:
        # If Ollama is unavailable, return a safe fallback fragment
        safe = nl_text.replace("'", "''")
        fallback = f"(fc.description LIKE '%{safe}%' OR fc.file_name LIKE '%{safe}%')"
        return jsonify({
            'sql_fragment': fallback,
            'explanation': f'Ollama unavailable ({e}); using keyword fallback.',
            'fallback': True,
        })


DEFAULT_SCHEMA_HINT = """
DATABASE: SQLite — per-catalog file: workflow_results.db
Base query context: FROM file_contents fc LEFT JOIN exif_data ed ON ed.file_id = fc.id

TABLES AND COLUMNS:

file_contents (alias: fc)
  id INTEGER PRIMARY KEY
  file_name TEXT        -- canonical filename
  file_type TEXT        -- extension e.g. '.jpg', '.heic'
  description TEXT      -- AI-generated natural-language description of the photo
  added_at TIMESTAMP    -- when the file was imported

exif_data (alias: ed)
  file_id INTEGER       -- FK → file_contents.id
  creation_date TEXT    -- capture date, formats: 'YYYY-MM-DD HH:MM:SS' or 'YYYY:MM:DD HH:MM:SS'
  latitude REAL
  longitude REAL
  camera_make TEXT      -- e.g. 'Apple', 'Canon'
  camera_model TEXT     -- e.g. 'iPhone 15 Pro', 'EOS R5'

events (alias: ev)
  id INTEGER PRIMARY KEY
  name TEXT             -- e.g. 'Birthday party', 'Walk in the park'
  start_time TEXT       -- ISO datetime
  end_time TEXT
  type TEXT             -- 'Auto' or user-defined

event_files (alias: ef)
  event_id INTEGER      -- FK → events.id
  file_id INTEGER       -- FK → file_contents.id

people (alias: p)
  id INTEGER PRIMARY KEY
  name TEXT             -- display name e.g. 'Izelpi', 'Dani', 'Rex'
  kind TEXT             -- 'person' or 'pet'. A PET IS A people ROW.
  species TEXT          -- 'dog', 'cat', … ; NULL for a person

subject_appearances (alias: sa)   -- who and what appears in a file
  file_id INTEGER       -- FK -> file_contents.id
  person_id INTEGER     -- FK -> people.id  (a pet is a people row)
  kind TEXT             -- 'person' or 'pet'
  -- USE THIS for "who is in this photo", for people AND pets. It is a view
  -- over face_detections and pet_detections, so one query covers both and
  -- there is no UNION to get right.

face_detections (alias: fd)
  id INTEGER PRIMARY KEY
  file_id INTEGER       -- FK → file_contents.id
  person_id INTEGER     -- FK → people.id (NULL if unidentified)
  source TEXT           -- 'Automatic' or 'Manual'

locations (alias: loc)
  id INTEGER PRIMARY KEY
  name TEXT

location_events (alias: le)
  location_id INTEGER   -- FK → locations.id
  event_id INTEGER      -- FK → events.id

tags (alias: t)
  id INTEGER PRIMARY KEY
  name TEXT

QUERY PATTERNS — use these subquery patterns when joining:

  Person OR pet in photo - USE THIS ONE for "photos of X":
    fc.id IN (SELECT sa2.file_id FROM subject_appearances sa2
              JOIN people p2 ON p2.id = sa2.person_id
              WHERE p2.name = 'Izelpi')

  Any pet at all:
    fc.id IN (SELECT sa5.file_id FROM subject_appearances sa5
              WHERE sa5.kind = 'pet')

  A particular species:
    fc.id IN (SELECT sa6.file_id FROM subject_appearances sa6
              JOIN people p6 ON p6.id = sa6.person_id
              WHERE p6.kind = 'pet' AND p6.species = 'dog')

  Person in photo, by face detection - ONLY when the query needs the BOX or
  needs to distinguish a hand-tagged face (fd.source = 'Manual'). For
  "who is in this photo", prefer subject_appearances above:
    fc.id IN (SELECT fd2.file_id FROM face_detections fd2
              JOIN people p2 ON p2.id = fd2.person_id
              WHERE p2.name = 'Izelpi')

  All photos in events where a person appears:
    fc.id IN (SELECT ef2.file_id FROM event_files ef2
              WHERE ef2.event_id IN (
                SELECT DISTINCT ef3.event_id FROM event_files ef3
                JOIN face_detections fd3 ON fd3.file_id = ef3.file_id
                JOIN people p3 ON p3.id = fd3.person_id
                WHERE p3.name = 'Izelpi'))

  Year from event start_time:
    fc.id IN (SELECT ef4.file_id FROM event_files ef4
              JOIN events ev4 ON ev4.id = ef4.event_id
              WHERE strftime('%Y', ev4.start_time) = '2025'
                 OR ev4.start_time LIKE '2025%')

  Year from EXIF capture date (handles both date formats):
    (strftime('%Y', ed.creation_date) = '2024'
     OR strftime('%Y', replace(ed.creation_date, ':', '-')) = '2024'
     OR ed.creation_date LIKE '2024%')

  Event by name:
    fc.id IN (SELECT ef5.file_id FROM event_files ef5
              JOIN events ev5 ON ev5.id = ef5.event_id
              WHERE ev5.name LIKE '%Birthday%')

  Location:
    fc.id IN (SELECT ef6.file_id FROM event_files ef6
              JOIN events ev6 ON ev6.id = ef6.event_id
              JOIN location_events le6 ON le6.event_id = ev6.id
              JOIN locations loc6 ON loc6.id = le6.location_id
              WHERE loc6.name LIKE '%Paris%')

  Camera brand:
    (ed.camera_make LIKE '%Canon%' OR ed.camera_model LIKE '%Canon%')

  Description keyword:
    fc.description LIKE '%beach%'
"""


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5005)


