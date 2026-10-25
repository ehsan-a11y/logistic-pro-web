import os
import sqlite3
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)

IS_VERCEL     = bool(os.environ.get('VERCEL'))
DATABASE_URL  = os.environ.get('DATABASE_URL')
UPLOAD_FOLDER = '/tmp/lp_uploads' if IS_VERCEL else os.path.join(os.path.dirname(__file__), 'uploads')
DB_PATH       = '/tmp/logisticpro.db' if IS_VERCEL else os.path.join(os.path.dirname(__file__), 'logisticpro.db')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

USE_PG = bool(DATABASE_URL)

_db_initialised = False   # lazy-init flag


# ── DB connection ─────────────────────────────────────────────────────────────

def get_pg():
    """Open a new PostgreSQL connection using pg8000 (pure Python)."""
    import pg8000.native
    from urllib.parse import urlparse
    u = urlparse(DATABASE_URL)
    return pg8000.native.Connection(
        host=u.hostname,
        port=u.port or 5432,
        database=u.path.lstrip('/'),
        user=u.username,
        password=u.password,
        ssl_context=True,
    )


def get_db():
    if USE_PG:
        return get_pg()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── lazy DB init ──────────────────────────────────────────────────────────────

def ensure_db():
    global _db_initialised
    if _db_initialised:
        return
    if USE_PG:
        conn = get_pg()
        try:
            pg_run(conn, '''
                CREATE TABLE IF NOT EXISTS shipments (
                    id           SERIAL PRIMARY KEY,
                    date         TEXT,
                    awb_no       TEXT UNIQUE,
                    cost         REAL,
                    status       TEXT,
                    awb_file     TEXT,
                    invoice_file TEXT,
                    created_at   TIMESTAMP DEFAULT NOW()
                )
            ''')
        finally:
            conn.close()
    else:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS shipments (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    date         TEXT,
                    awb_no       TEXT UNIQUE,
                    cost         REAL,
                    status       TEXT,
                    awb_file     TEXT,
                    invoice_file TEXT,
                    created_at   TEXT DEFAULT (datetime('now'))
                )
            ''')
            try:
                conn.execute(
                    'CREATE UNIQUE INDEX IF NOT EXISTS idx_awb_no ON shipments(awb_no)'
                )
            except Exception:
                pass
    _db_initialised = True


@app.before_request
def before_request():
    ensure_db()


# ── helpers ───────────────────────────────────────────────────────────────────

def save_file(key):
    f = request.files.get(key)
    if f and f.filename:
        fn = secure_filename(f.filename)
        f.save(os.path.join(UPLOAD_FOLDER, fn))
        return fn
    return None


def pg_rows(conn, sql, params=()):
    """Run a SELECT on pg8000 and return list of dicts.
    pg8000 uses $1,$2,... placeholders passed as keyword args p1, p2, ...
    """
    kwargs = {'p{}'.format(i+1): v for i, v in enumerate(params)}
    # replace :1/:2 style or $1/$2 style → p1,p2 style
    import re
    sql = re.sub(r'\$(\d+)', lambda m: ':p'+m.group(1), sql)
    rows = conn.run(sql, **kwargs)
    cols = [c['name'] for c in conn.columns]
    return [dict(zip(cols, row)) for row in rows]


def pg_run(conn, sql, params=()):
    """Run a non-SELECT statement on pg8000."""
    kwargs = {'p{}'.format(i+1): v for i, v in enumerate(params)}
    import re
    sql = re.sub(r'\$(\d+)', lambda m: ':p'+m.group(1), sql)
    conn.run(sql, **kwargs)


def pg_one(conn, sql, params=()):
    rows = pg_rows(conn, sql, params)
    return rows[0] if rows else None


# ── routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/sw.js')
def sw():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')


@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')


@app.route('/api/shipments', methods=['GET'])
def get_shipments():
    if USE_PG:
        conn = get_pg()
        try:
            rows = pg_rows(conn, 'SELECT * FROM shipments ORDER BY created_at DESC')
        finally:
            conn.close()
    else:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                'SELECT * FROM shipments ORDER BY created_at DESC'
            ).fetchall()]
    return jsonify(rows)


@app.route('/api/shipments', methods=['POST'])
def add_shipment():
    awb_file     = save_file('awb_file')
    invoice_file = save_file('invoice_file')
    d      = request.form
    awb_no = d.get('awb_no', '').strip()

    if USE_PG:
        conn = get_pg()
        try:
            dup = pg_one(conn, 'SELECT id FROM shipments WHERE awb_no = $1', (awb_no,))
            if dup:
                return jsonify({'success': False, 'error': 'AWB No. already exists'}), 409
            pg_run(conn,
                'INSERT INTO shipments (date,awb_no,cost,status,awb_file,invoice_file) '
                'VALUES ($1,$2,$3,$4,$5,$6)',
                (d.get('date'), awb_no, d.get('cost') or None,
                 d.get('status'), awb_file, invoice_file))
            row = pg_one(conn, 'SELECT * FROM shipments WHERE awb_no = $1', (awb_no,))
        finally:
            conn.close()
    else:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            if conn.execute('SELECT id FROM shipments WHERE awb_no=?', (awb_no,)).fetchone():
                return jsonify({'success': False, 'error': 'AWB No. already exists'}), 409
            cur = conn.execute(
                'INSERT INTO shipments (date,awb_no,cost,status,awb_file,invoice_file) '
                'VALUES (?,?,?,?,?,?)',
                (d.get('date'), awb_no, d.get('cost') or None,
                 d.get('status'), awb_file, invoice_file)
            )
            row = dict(conn.execute(
                'SELECT * FROM shipments WHERE id=?', (cur.lastrowid,)
            ).fetchone())

    return jsonify({'success': True, 'record': row})


@app.route('/api/shipments/<int:sid>', methods=['PUT'])
def update_shipment(sid):
    awb_file     = save_file('awb_file')
    invoice_file = save_file('invoice_file')
    d      = request.form
    awb_no = d.get('awb_no', '').strip()

    if USE_PG:
        conn = get_pg()
        try:
            if not pg_one(conn, 'SELECT id FROM shipments WHERE id = $1', (sid,)):
                return jsonify({'success': False, 'error': 'Not found'}), 404
            if pg_one(conn,
                'SELECT id FROM shipments WHERE awb_no = $1 AND id != $2', (awb_no, sid)):
                return jsonify({'success': False, 'error': 'AWB No. already exists'}), 409
            pg_run(conn,
                'UPDATE shipments SET date=$1,awb_no=$2,cost=$3,status=$4,'
                'awb_file=COALESCE($5,awb_file),invoice_file=COALESCE($6,invoice_file) '
                'WHERE id=$7',
                (d.get('date'), awb_no, d.get('cost') or None,
                 d.get('status'), awb_file, invoice_file, sid))
            row = pg_one(conn, 'SELECT * FROM shipments WHERE id = $1', (sid,))
        finally:
            conn.close()
    else:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            if not conn.execute('SELECT id FROM shipments WHERE id=?', (sid,)).fetchone():
                return jsonify({'success': False, 'error': 'Not found'}), 404
            if conn.execute(
                'SELECT id FROM shipments WHERE awb_no=? AND id!=?', (awb_no, sid)
            ).fetchone():
                return jsonify({'success': False, 'error': 'AWB No. already exists'}), 409
            conn.execute(
                'UPDATE shipments SET date=?,awb_no=?,cost=?,status=?,'
                'awb_file=COALESCE(?,awb_file),invoice_file=COALESCE(?,invoice_file) '
                'WHERE id=?',
                (d.get('date'), awb_no, d.get('cost') or None,
                 d.get('status'), awb_file, invoice_file, sid)
            )
            row = dict(conn.execute(
                'SELECT * FROM shipments WHERE id=?', (sid,)
            ).fetchone())

    return jsonify({'success': True, 'record': row})


@app.route('/api/shipments/<int:sid>', methods=['DELETE'])
def delete_shipment(sid):
    if USE_PG:
        conn = get_pg()
        try:
            pg_run(conn, 'DELETE FROM shipments WHERE id = $1', (sid,))
        finally:
            conn.close()
    else:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('DELETE FROM shipments WHERE id=?', (sid,))
    return jsonify({'success': True})


@app.route('/api/dashboard')
def dashboard():
    if USE_PG:
        conn = get_pg()
        try:
            total     = pg_one(conn, 'SELECT COUNT(*) AS n FROM shipments')['n']
            transit   = pg_one(conn, "SELECT COUNT(*) AS n FROM shipments WHERE status='Transit'")['n']
            delivered = pg_one(conn, "SELECT COUNT(*) AS n FROM shipments WHERE status='Delivered'")['n']
            returned  = pg_one(conn, "SELECT COUNT(*) AS n FROM shipments WHERE status='Returned'")['n']
            total_cost = pg_one(conn, 'SELECT COALESCE(SUM(cost), 0) AS n FROM shipments')['n']
            monthly   = pg_rows(conn,
                "SELECT LEFT(date,7) AS month, COUNT(*) AS count "
                "FROM shipments WHERE date IS NOT NULL AND date != '' "
                "GROUP BY month ORDER BY month"
            )
        finally:
            conn.close()
    else:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            total     = conn.execute('SELECT COUNT(*) FROM shipments').fetchone()[0]
            transit   = conn.execute("SELECT COUNT(*) FROM shipments WHERE status='Transit'").fetchone()[0]
            delivered = conn.execute("SELECT COUNT(*) FROM shipments WHERE status='Delivered'").fetchone()[0]
            returned  = conn.execute("SELECT COUNT(*) FROM shipments WHERE status='Returned'").fetchone()[0]
            total_cost = conn.execute('SELECT COALESCE(SUM(cost), 0) FROM shipments').fetchone()[0]
            monthly   = [dict(r) for r in conn.execute(
                "SELECT strftime('%Y-%m',date) AS month, COUNT(*) AS count "
                "FROM shipments WHERE date IS NOT NULL "
                "GROUP BY month ORDER BY month"
            ).fetchall()]

    return jsonify({
        'total': total, 'transit': transit,
        'delivered': delivered, 'returned': returned,
        'total_cost': round(float(total_cost or 0), 2),
        'monthly': monthly
    })


# ── shipment tracking (Parcels API) ───────────────────────────────────────────
PARCELS_API_KEY = os.environ.get('PARCELS_API_KEY', '').strip()
PARCELS_API_URL = 'https://parcelsapp.com/api/v3/shipments/tracking'


def _parcels_call(method, params=None, body=None):
    """Call the Parcels API and return parsed JSON (or an error dict)."""
    import json
    import urllib.parse
    import urllib.request
    url = PARCELS_API_URL
    data = None
    headers = {'Accept': 'application/json'}
    if params:
        url += '?' + urllib.parse.urlencode(params)
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as exc:  # network error, timeout, bad JSON
        return {'error': 'NETWORK', 'description': str(exc)}


def _shape_tracking(result, awb):
    """Reduce a Parcels result to what the tracking panel needs."""
    shipments = result.get('shipments') or []
    match = next((s for s in shipments
                  if str(s.get('trackingId', '')).upper() == awb.upper()),
                 shipments[0] if shipments else None)
    if not match:
        return None
    services = match.get('services') or []
    carrier = (match.get('detectedCarrier') or {}).get('name') or \
              (services[0].get('name') if services else '')
    events = []
    for st in match.get('states') or []:
        idx = st.get('carrier')
        ev_carrier = ''
        if isinstance(idx, int) and 0 <= idx < len(services):
            ev_carrier = services[idx].get('name', '')
        events.append({
            'date': st.get('date'),
            'state': st.get('state', ''),
            'location': st.get('location', ''),
            'carrier': ev_carrier or carrier,
        })
    return {
        'trackingId': match.get('trackingId', awb),
        'status': match.get('status', ''),
        'origin': match.get('origin', ''),
        'destination': match.get('destination', ''),
        'carrier': carrier,
        'events': events,
    }


@app.route('/api/track')
def track():
    """Start (awb=...) or continue (uuid=...) a tracking lookup.

    Returns {done, uuid, shipment} or {error, message}.
    """
    awb = (request.args.get('awb') or '').strip()
    uuid = (request.args.get('uuid') or '').strip()
    if not awb:
        return jsonify({'error': 'MISSING_AWB', 'message': 'No AWB number given.'}), 400
    if not PARCELS_API_KEY:
        return jsonify({'error': 'NOT_CONFIGURED',
                        'message': 'Tracking is not set up yet. Add PARCELS_API_KEY in Vercel.'})

    if uuid:
        result = _parcels_call('GET', params={'uuid': uuid, 'apiKey': PARCELS_API_KEY})
    else:
        result = _parcels_call('POST', body={
            'apiKey': PARCELS_API_KEY,
            'language': 'en',
            'shipments': [{'trackingId': awb}],
        })

    if result.get('error'):
        return jsonify({'error': result['error'],
                        'message': result.get('description') or result['error']})

    shipment = _shape_tracking(result, awb)
    done = bool(result.get('done'))
    return jsonify({'done': done, 'uuid': result.get('uuid') or uuid, 'shipment': shipment})


@app.route('/uploads/<filename>')
def uploads(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


if __name__ == '__main__':
    ensure_db()
    print('Logistic Pro running at http://localhost:5002')
    app.run(debug=True, port=5002)
