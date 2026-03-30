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
if USE_PG:
    import psycopg2
    import psycopg2.extras


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    if USE_PG:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('''
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
            conn.commit()
    else:
        with get_db() as conn:
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


init_db()


def save_file(key):
    f = request.files.get(key)
    if f and f.filename:
        fn = secure_filename(f.filename)
        f.save(os.path.join(UPLOAD_FOLDER, fn))
        return fn
    return None


def rows_to_list(rows):
    if USE_PG:
        return [dict(r) for r in rows]
    return [dict(r) for r in rows]


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
    with get_db() as conn:
        if USE_PG:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM shipments ORDER BY created_at DESC')
                rows = cur.fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM shipments ORDER BY created_at DESC'
            ).fetchall()
    return jsonify(rows_to_list(rows))


@app.route('/api/shipments', methods=['POST'])
def add_shipment():
    awb_file     = save_file('awb_file')
    invoice_file = save_file('invoice_file')
    d      = request.form
    awb_no = d.get('awb_no', '').strip()

    with get_db() as conn:
        if USE_PG:
            with conn.cursor() as cur:
                cur.execute('SELECT id FROM shipments WHERE awb_no=%s', (awb_no,))
                if cur.fetchone():
                    return jsonify({'success': False, 'error': 'AWB No. already exists'}), 409
                cur.execute(
                    '''INSERT INTO shipments (date,awb_no,cost,status,awb_file,invoice_file)
                       VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',
                    (d.get('date'), awb_no, d.get('cost') or None,
                     d.get('status'), awb_file, invoice_file)
                )
                row = dict(cur.fetchone())
            conn.commit()
        else:
            dup = conn.execute(
                'SELECT id FROM shipments WHERE awb_no=?', (awb_no,)
            ).fetchone()
            if dup:
                return jsonify({'success': False, 'error': 'AWB No. already exists'}), 409
            cur = conn.execute(
                '''INSERT INTO shipments (date,awb_no,cost,status,awb_file,invoice_file)
                   VALUES (?,?,?,?,?,?)''',
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

    with get_db() as conn:
        if USE_PG:
            with conn.cursor() as cur:
                cur.execute('SELECT id FROM shipments WHERE id=%s', (sid,))
                if not cur.fetchone():
                    return jsonify({'success': False, 'error': 'Not found'}), 404
                cur.execute(
                    'SELECT id FROM shipments WHERE awb_no=%s AND id!=%s', (awb_no, sid)
                )
                if cur.fetchone():
                    return jsonify({'success': False, 'error': 'AWB No. already exists'}), 409
                cur.execute(
                    '''UPDATE shipments
                       SET date=%s, awb_no=%s, cost=%s, status=%s,
                           awb_file=COALESCE(%s,awb_file),
                           invoice_file=COALESCE(%s,invoice_file)
                       WHERE id=%s RETURNING *''',
                    (d.get('date'), awb_no, d.get('cost') or None,
                     d.get('status'), awb_file, invoice_file, sid)
                )
                row = dict(cur.fetchone())
            conn.commit()
        else:
            if not conn.execute(
                'SELECT id FROM shipments WHERE id=?', (sid,)
            ).fetchone():
                return jsonify({'success': False, 'error': 'Not found'}), 404
            if conn.execute(
                'SELECT id FROM shipments WHERE awb_no=? AND id!=?', (awb_no, sid)
            ).fetchone():
                return jsonify({'success': False, 'error': 'AWB No. already exists'}), 409
            conn.execute(
                '''UPDATE shipments
                   SET date=?, awb_no=?, cost=?, status=?,
                       awb_file=COALESCE(?,awb_file),
                       invoice_file=COALESCE(?,invoice_file)
                   WHERE id=?''',
                (d.get('date'), awb_no, d.get('cost') or None,
                 d.get('status'), awb_file, invoice_file, sid)
            )
            row = dict(conn.execute(
                'SELECT * FROM shipments WHERE id=?', (sid,)
            ).fetchone())

    return jsonify({'success': True, 'record': row})


@app.route('/api/shipments/<int:sid>', methods=['DELETE'])
def delete_shipment(sid):
    with get_db() as conn:
        if USE_PG:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM shipments WHERE id=%s', (sid,))
            conn.commit()
        else:
            conn.execute('DELETE FROM shipments WHERE id=?', (sid,))
    return jsonify({'success': True})


@app.route('/api/dashboard')
def dashboard():
    with get_db() as conn:
        if USE_PG:
            with conn.cursor() as cur:
                cur.execute('SELECT COUNT(*) AS n FROM shipments')
                total = cur.fetchone()['n']
                cur.execute("SELECT COUNT(*) AS n FROM shipments WHERE status='Transit'")
                transit = cur.fetchone()['n']
                cur.execute("SELECT COUNT(*) AS n FROM shipments WHERE status='Delivered'")
                delivered = cur.fetchone()['n']
                cur.execute("SELECT COUNT(*) AS n FROM shipments WHERE status='Returned'")
                returned = cur.fetchone()['n']
                cur.execute(
                    "SELECT TO_CHAR(date::date,'YYYY-MM') AS month, COUNT(*) AS count "
                    "FROM shipments WHERE date IS NOT NULL AND date!='' "
                    "GROUP BY month ORDER BY month"
                )
                monthly = [dict(r) for r in cur.fetchall()]
        else:
            total     = conn.execute('SELECT COUNT(*) FROM shipments').fetchone()[0]
            transit   = conn.execute(
                "SELECT COUNT(*) FROM shipments WHERE status='Transit'"
            ).fetchone()[0]
            delivered = conn.execute(
                "SELECT COUNT(*) FROM shipments WHERE status='Delivered'"
            ).fetchone()[0]
            returned  = conn.execute(
                "SELECT COUNT(*) FROM shipments WHERE status='Returned'"
            ).fetchone()[0]
            monthly   = [dict(r) for r in conn.execute(
                "SELECT strftime('%Y-%m',date) AS month, COUNT(*) AS count "
                "FROM shipments WHERE date IS NOT NULL "
                "GROUP BY month ORDER BY month"
            ).fetchall()]

    return jsonify({
        'total': total, 'transit': transit,
        'delivered': delivered, 'returned': returned,
        'monthly': monthly
    })


@app.route('/uploads/<filename>')
def uploads(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


if __name__ == '__main__':
    print('Logistic Pro running at http://localhost:5002')
    app.run(debug=True, port=5002)
