import os
import sqlite3
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)

IS_VERCEL     = bool(os.environ.get('VERCEL'))
# Production uses DATABASE_URL; preview deployments fall back to the separate test database.
DATABASE_URL  = os.environ.get('DATABASE_URL') or os.environ.get('TEST_DATABASE_DATABASE_URL')
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
            pg_run(conn, '''
                CREATE TABLE IF NOT EXISTS app_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
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
            conn.execute('''
                CREATE TABLE IF NOT EXISTS app_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
    _db_initialised = True


@app.before_request
def before_request():
    ensure_db()
    ensure_modules_db()
    return _auth_gate()


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


# ══════════════════════════════════════════════════════════════════════════════
#  Users & roles · Warehouses · Inventory · Orders (pick & pack)
# ══════════════════════════════════════════════════════════════════════════════
import datetime as _dt
import hashlib as _hashlib
import re as _re
import secrets as _secrets
from functools import wraps as _wraps
from flask import session, g
from werkzeug.security import generate_password_hash, check_password_hash

app.secret_key = os.environ.get('SECRET_KEY') or _hashlib.sha256(
    ('logistic-pro-session:' + (DATABASE_URL or DB_PATH)).encode()).hexdigest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=IS_VERCEL,
    PERMANENT_SESSION_LIFETIME=_dt.timedelta(hours=12),
)
BOOTSTRAP_ADMIN = os.environ.get('BOOTSTRAP_ADMIN', 'ehsan@wakecap.com').strip().lower()

ROLES = ['admin', 'requester', 'warehouse', 'logistics']
PERMS = {
    'admin':     {'shipments.edit', 'shipments.delete', 'products.manage', 'stock.adjust',
                  'orders.create', 'orders.process', 'orders.ship', 'orders.cancel',
                  'returns.receive', 'settings'},
    'requester': {'orders.create', 'orders.cancel'},
    'warehouse': {'stock.adjust', 'orders.process', 'orders.cancel', 'returns.receive'},
    'logistics': {'shipments.edit', 'orders.ship'},
}
ORDER_OPEN = ('pick_list', 'picked', 'packed')
ORDER_LABEL = {'pick_list': 'To pick', 'picked': 'To pack', 'packed': 'Ready to ship',
               'shipped': 'Shipped', 'cancelled': 'Cancelled'}


def _now():
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'


class DB:
    """One connection + transaction for a block of statements. SQL uses $1, $2 ... placeholders."""
    def __enter__(self):
        if USE_PG:
            self.c = get_pg()
            self.c.run('BEGIN')
        else:
            self.c = sqlite3.connect(DB_PATH)
            self.c.row_factory = sqlite3.Row
        return self

    def __exit__(self, et, ev, tb):
        try:
            if USE_PG:
                self.c.run('ROLLBACK' if et else 'COMMIT')
            else:
                (self.c.rollback if et else self.c.commit)()
        finally:
            self.c.close()
        return False

    def _lite(self, sql):
        return _re.sub(r'\$\d+', '?', sql)

    def rows(self, sql, p=()):
        if USE_PG:
            return pg_rows(self.c, sql, p)
        return [dict(r) for r in self.c.execute(self._lite(sql), p).fetchall()]

    def one(self, sql, p=()):
        r = self.rows(sql, p)
        return r[0] if r else None

    def run(self, sql, p=()):
        if USE_PG:
            pg_run(self.c, sql, p)
        else:
            self.c.execute(self._lite(sql), p)

    def insert(self, sql, p=()):
        if USE_PG:
            return pg_rows(self.c, sql + ' RETURNING id', p)[0]['id']
        return self.c.execute(self._lite(sql), p).lastrowid


_modules_ready = False


def ensure_modules_db():
    global _modules_ready
    if _modules_ready:
        return
    pk = 'SERIAL PRIMARY KEY' if USE_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    tables = [
        f'''CREATE TABLE IF NOT EXISTS users (id {pk}, email TEXT UNIQUE NOT NULL, name TEXT,
            role TEXT NOT NULL, password_hash TEXT, active INTEGER DEFAULT 1, warehouse_id INTEGER,
            failed_logins INTEGER DEFAULT 0, locked_until TEXT, last_login TEXT, created_at TEXT)''',
        '''CREATE TABLE IF NOT EXISTS user_tokens (token_hash TEXT PRIMARY KEY, user_id INTEGER,
            purpose TEXT, expires_at TEXT, used INTEGER DEFAULT 0)''',
        f'''CREATE TABLE IF NOT EXISTS warehouses (id {pk}, name TEXT UNIQUE NOT NULL, address TEXT,
            manager TEXT, active INTEGER DEFAULT 1, created_at TEXT)''',
        f'''CREATE TABLE IF NOT EXISTS products (id {pk}, sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            unit TEXT, category TEXT, min_qty INTEGER DEFAULT 0, unit_value REAL DEFAULT 0,
            active INTEGER DEFAULT 1, created_at TEXT)''',
        f'''CREATE TABLE IF NOT EXISTS stock_movements (id {pk}, product_id INTEGER NOT NULL,
            warehouse_id INTEGER NOT NULL, qty INTEGER NOT NULL, type TEXT NOT NULL, reason TEXT,
            note TEXT, order_id INTEGER, shipment_id INTEGER, user_id INTEGER, created_at TEXT)''',
        f'''CREATE TABLE IF NOT EXISTS orders (id {pk}, warehouse_id INTEGER NOT NULL,
            requester_id INTEGER, destination TEXT, contact TEXT, urgency TEXT, notes TEXT,
            status TEXT NOT NULL, boxes INTEGER, weight REAL, dimensions TEXT,
            picked_by INTEGER, picked_at TEXT, packed_by INTEGER, packed_at TEXT,
            shipped_by INTEGER, shipped_at TEXT, shipment_id INTEGER, cancel_reason TEXT,
            return_action TEXT, created_at TEXT, updated_at TEXT)''',
        f'''CREATE TABLE IF NOT EXISTS order_items (id {pk}, order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL, qty_requested INTEGER NOT NULL, qty_picked INTEGER)''',
        f'''CREATE TABLE IF NOT EXISTS locations (id {pk}, warehouse_id INTEGER NOT NULL, code TEXT NOT NULL,
            zone TEXT, rack TEXT, shelf TEXT, bin TEXT, description TEXT, active INTEGER DEFAULT 1, created_at TEXT)''',
        f'''CREATE TABLE IF NOT EXISTS order_picks (id {pk}, order_id INTEGER NOT NULL, order_item_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL, location_id INTEGER, qty INTEGER NOT NULL)''',
    ]
    with DB() as db:
        for t in tables:
            db.run(t)
        if USE_PG:
            db.run('ALTER TABLE shipments ADD COLUMN IF NOT EXISTS order_id INTEGER')
            db.run('ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS location_id INTEGER')
        else:
            cols = [r['name'] for r in db.rows('PRAGMA table_info(shipments)')]
            if 'order_id' not in cols:
                db.run('ALTER TABLE shipments ADD COLUMN order_id INTEGER')
            cols = [r['name'] for r in db.rows('PRAGMA table_info(stock_movements)')]
            if 'location_id' not in cols:
                db.run('ALTER TABLE stock_movements ADD COLUMN location_id INTEGER')
    _modules_ready = True


# ── auth ──────────────────────────────────────────────────────────────────────
PUBLIC_API = {'/api/auth/me', '/api/auth/login', '/api/auth/logout', '/api/auth/setup',
              '/api/auth/forgot', '/api/auth/set-password', '/api/auth/token-info',
              '/api/cron/summary'}


def _public_user(u):
    return {'id': u['id'], 'email': u['email'], 'name': u.get('name') or u['email'],
            'role': u['role'], 'active': bool(u.get('active')),
            'warehouse_id': u.get('warehouse_id'), 'last_login': u.get('last_login'),
            'has_password': bool(u.get('password_hash')),
            'perms': sorted(PERMS.get(u['role'], set()))}


def _auth_gate():
    path = request.path
    if not (path.startswith('/api/') or path.startswith('/uploads/')) or path in PUBLIC_API:
        return None
    uid = session.get('uid')
    user = None
    if uid:
        with DB() as db:
            user = db.one('SELECT * FROM users WHERE id = $1', (uid,))
    if not user or not user.get('active') or session.get('pv') != (user.get('password_hash') or '')[-12:]:
        session.clear()
        return jsonify({'success': False, 'error': 'Please sign in.', 'auth': True}), 401
    g.user = user
    return None


def can(perm):
    u = getattr(g, 'user', None)
    return bool(u) and perm in PERMS.get(u['role'], set())


def requires(perm):
    def deco(fn):
        @_wraps(fn)
        def inner(*a, **k):
            if not can(perm):
                return jsonify({'success': False, 'error': 'You don’t have permission to do that.'}), 403
            return fn(*a, **k)
        return inner
    return deco


def _login(user):
    session.clear()
    session.permanent = True
    session['uid'] = user['id']
    session['pv'] = (user.get('password_hash') or '')[-12:]


def _password_problem(pw):
    if len(pw or '') < 8:
        return 'Password must be at least 8 characters.'
    return None


def _make_token(db, user_id, purpose, hours):
    token = _secrets.token_urlsafe(32)
    exp = (_dt.datetime.utcnow() + _dt.timedelta(hours=hours)).replace(microsecond=0).isoformat() + 'Z'
    db.run('INSERT INTO user_tokens (token_hash, user_id, purpose, expires_at, used) VALUES ($1,$2,$3,$4,0)',
           (_hashlib.sha256(token.encode()).hexdigest(), user_id, purpose, exp))
    base = request.host_url.rstrip('/')
    return '{}/?{}={}'.format(base, 'invite' if purpose == 'invite' else 'reset', token)


def _token_row(db, token):
    if not token:
        return None
    t = db.one('SELECT * FROM user_tokens WHERE token_hash = $1', (_hashlib.sha256(token.encode()).hexdigest(),))
    if not t or t.get('used') or (t.get('expires_at') or '') < _now():
        return None
    return t


def _email_link(user, link, purpose):
    if not email_configured():
        return False
    if purpose == 'invite':
        subject = 'You’re invited to Logistic Pro'
        intro = 'You’ve been given access to Logistic Pro as <b>{}</b>. Set your password to get started.'.format(_esc(user['role'].title()))
    else:
        subject = 'Reset your Logistic Pro password'
        intro = 'Use the button below to choose a new password. If you didn’t ask for this, you can ignore this email.'
    body = ('<p style="margin:0 0 18px">' + intro + '</p>'
            '<a href="' + _esc(link) + '" style="display:inline-block;background:#4361ee;color:#fff;'
            'padding:11px 20px;border-radius:8px;text-decoration:none;font-weight:bold">Set password</a>'
            '<p style="margin:18px 0 0;color:#64748b;font-size:12px">This link works once and expires in '
            + ('3 days' if purpose == 'invite' else '2 hours') + '.</p>')
    ok, err = send_email([user['email']], subject, _email_shell(subject, body), '{}\n{}'.format(subject, link))
    if not ok:
        print('[email] link not sent:', err)
    return ok


@app.route('/api/auth/me')
def auth_me():
    with DB() as db:
        n = db.one('SELECT COUNT(*) AS n FROM users')['n']
        uid = session.get('uid')
        u = db.one('SELECT * FROM users WHERE id = $1', (uid,)) if uid else None
    if u and u.get('active') and session.get('pv') == (u.get('password_hash') or '')[-12:]:
        return jsonify({'user': _public_user(u)})
    return jsonify({'user': None, 'setup': n == 0, 'setup_email': BOOTSTRAP_ADMIN if n == 0 else None}), 401


@app.route('/api/auth/setup', methods=['POST'])
def auth_setup():
    d = request.get_json(silent=True) or {}
    email = (d.get('email') or '').strip().lower()
    pw = d.get('password') or ''
    with DB() as db:
        if db.one('SELECT COUNT(*) AS n FROM users')['n'] > 0:
            return jsonify({'success': False, 'error': 'Setup is already complete. Please sign in.'}), 409
        if email != BOOTSTRAP_ADMIN:
            return jsonify({'success': False, 'error': 'The first admin account must use ' + BOOTSTRAP_ADMIN + '.'}), 403
        prob = _password_problem(pw)
        if prob:
            return jsonify({'success': False, 'error': prob}), 400
        uid = db.insert('INSERT INTO users (email, name, role, password_hash, active, created_at, last_login) '
                        'VALUES ($1,$2,$3,$4,1,$5,$6)',
                        (email, (d.get('name') or '').strip() or email.split('@')[0].title(), 'admin',
                         generate_password_hash(pw), _now(), _now()))
        u = db.one('SELECT * FROM users WHERE id = $1', (uid,))
    _login(u)
    return jsonify({'success': True, 'user': _public_user(u)})


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    d = request.get_json(silent=True) or {}
    email = (d.get('email') or '').strip().lower()
    pw = d.get('password') or ''
    bad = jsonify({'success': False, 'error': 'Email or password is incorrect.'}), 401
    with DB() as db:
        u = db.one('SELECT * FROM users WHERE email = $1', (email,))
        if not u or not u.get('active') or not u.get('password_hash'):
            return bad
        if (u.get('locked_until') or '') > _now():
            return jsonify({'success': False, 'error': 'Too many failed attempts. Try again in 15 minutes.'}), 429
        if not check_password_hash(u['password_hash'], pw):
            fails = int(u.get('failed_logins') or 0) + 1
            lock = None
            if fails >= 5:
                fails, lock = 0, (_dt.datetime.utcnow() + _dt.timedelta(minutes=15)).replace(microsecond=0).isoformat() + 'Z'
            db.run('UPDATE users SET failed_logins = $1, locked_until = $2 WHERE id = $3', (fails, lock, u['id']))
            return bad
        db.run('UPDATE users SET failed_logins = 0, locked_until = NULL, last_login = $1 WHERE id = $2', (_now(), u['id']))
    _login(u)
    return jsonify({'success': True, 'user': _public_user(u)})


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/api/auth/forgot', methods=['POST'])
def auth_forgot():
    email = ((request.get_json(silent=True) or {}).get('email') or '').strip().lower()
    with DB() as db:
        u = db.one('SELECT * FROM users WHERE email = $1 AND active = 1', (email,))
        if u and u.get('password_hash'):
            link = _make_token(db, u['id'], 'reset', 2)
        else:
            u = None
    if u:
        _email_link(u, link, 'reset')
    return jsonify({'success': True, 'emailEnabled': email_configured()})


@app.route('/api/auth/token-info')
def auth_token_info():
    with DB() as db:
        t = _token_row(db, request.args.get('token'))
        u = db.one('SELECT * FROM users WHERE id = $1', (t['user_id'],)) if t else None
    if not t or not u or not u.get('active'):
        return jsonify({'valid': False}), 404
    return jsonify({'valid': True, 'email': u['email'], 'name': u.get('name'), 'purpose': t['purpose']})


@app.route('/api/auth/set-password', methods=['POST'])
def auth_set_password():
    d = request.get_json(silent=True) or {}
    prob = _password_problem(d.get('password'))
    if prob:
        return jsonify({'success': False, 'error': prob}), 400
    with DB() as db:
        t = _token_row(db, d.get('token'))
        if not t:
            return jsonify({'success': False, 'error': 'This link has expired or was already used. Ask an admin for a new one.'}), 400
        db.run('UPDATE user_tokens SET used = 1 WHERE user_id = $1', (t['user_id'],))
        db.run('UPDATE users SET password_hash = $1, failed_logins = 0, locked_until = NULL, last_login = $2 WHERE id = $3',
               (generate_password_hash(d['password']), _now(), t['user_id']))
        u = db.one('SELECT * FROM users WHERE id = $1', (t['user_id'],))
    if not u.get('active'):
        return jsonify({'success': False, 'error': 'This account is deactivated.'}), 403
    _login(u)
    return jsonify({'success': True, 'user': _public_user(u)})


# ── users (admin) ─────────────────────────────────────────────────────────────
@app.route('/api/users')
@requires('settings')
def users_list():
    with DB() as db:
        rows = db.rows('SELECT * FROM users ORDER BY active DESC, role, email')
    return jsonify([_public_user(r) for r in rows])


@app.route('/api/users', methods=['POST'])
@requires('settings')
def users_invite():
    d = request.get_json(silent=True) or {}
    email = (d.get('email') or '').strip().lower()
    role = d.get('role')
    if not valid_email(email):
        return jsonify({'success': False, 'error': 'Enter a valid email address.'}), 400
    if role not in ROLES:
        return jsonify({'success': False, 'error': 'Choose a role.'}), 400
    with DB() as db:
        if db.one('SELECT id FROM users WHERE email = $1', (email,)):
            return jsonify({'success': False, 'error': 'That email already has an account.'}), 409
        uid = db.insert('INSERT INTO users (email, name, role, active, warehouse_id, created_at) VALUES ($1,$2,$3,1,$4,$5)',
                        (email, (d.get('name') or '').strip() or email.split('@')[0].title(), role,
                         d.get('warehouse_id') or None, _now()))
        link = _make_token(db, uid, 'invite', 72)
        u = db.one('SELECT * FROM users WHERE id = $1', (uid,))
    sent = _email_link(u, link, 'invite')
    return jsonify({'success': True, 'user': _public_user(u), 'link': link, 'emailed': sent})


def _active_admins(db, excluding=None):
    rows = db.rows("SELECT id FROM users WHERE role = 'admin' AND active = 1")
    return [r for r in rows if r['id'] != excluding]


@app.route('/api/users/<int:uid>', methods=['PUT'])
@requires('settings')
def users_update(uid):
    d = request.get_json(silent=True) or {}
    with DB() as db:
        u = db.one('SELECT * FROM users WHERE id = $1', (uid,))
        if not u:
            return jsonify({'success': False, 'error': 'Not found'}), 404
        role = d.get('role', u['role'])
        active = 1 if d.get('active', bool(u['active'])) else 0
        if role not in ROLES:
            return jsonify({'success': False, 'error': 'Unknown role.'}), 400
        if uid == g.user['id'] and (not active or role != 'admin'):
            return jsonify({'success': False, 'error': 'You can’t remove your own admin access.'}), 400
        if u['role'] == 'admin' and (role != 'admin' or not active) and not _active_admins(db, uid):
            return jsonify({'success': False, 'error': 'There must be at least one active admin.'}), 400
        db.run('UPDATE users SET name = $1, role = $2, active = $3, warehouse_id = $4 WHERE id = $5',
               ((d.get('name') or u.get('name') or '').strip(), role, active,
                d.get('warehouse_id', u.get('warehouse_id')) or None, uid))
        u = db.one('SELECT * FROM users WHERE id = $1', (uid,))
    return jsonify({'success': True, 'user': _public_user(u)})


@app.route('/api/users/<int:uid>/link', methods=['POST'])
@requires('settings')
def users_link(uid):
    with DB() as db:
        u = db.one('SELECT * FROM users WHERE id = $1', (uid,))
        if not u:
            return jsonify({'success': False, 'error': 'Not found'}), 404
        purpose = 'reset' if u.get('password_hash') else 'invite'
        link = _make_token(db, uid, purpose, 72 if purpose == 'invite' else 2)
    sent = _email_link(u, link, purpose)
    return jsonify({'success': True, 'link': link, 'emailed': sent, 'purpose': purpose})


# ── warehouses ────────────────────────────────────────────────────────────────
@app.route('/api/warehouses')
def wh_list():
    with DB() as db:
        return jsonify(db.rows('SELECT * FROM warehouses ORDER BY active DESC, name'))


def _wh_payload(d):
    name = (d.get('name') or '').strip()
    if not name:
        return None, 'Enter a warehouse name.'
    return (name, (d.get('address') or '').strip(), (d.get('manager') or '').strip()), None


@app.route('/api/warehouses', methods=['POST'])
@requires('settings')
def wh_create():
    p, err = _wh_payload(request.get_json(silent=True) or {})
    if err:
        return jsonify({'success': False, 'error': err}), 400
    with DB() as db:
        if db.one('SELECT id FROM warehouses WHERE LOWER(name) = LOWER($1)', (p[0],)):
            return jsonify({'success': False, 'error': 'A warehouse with that name already exists.'}), 409
        wid = db.insert('INSERT INTO warehouses (name, address, manager, active, created_at) VALUES ($1,$2,$3,1,$4)', p + (_now(),))
        return jsonify({'success': True, 'warehouse': db.one('SELECT * FROM warehouses WHERE id = $1', (wid,))})


@app.route('/api/warehouses/<int:wid>', methods=['PUT'])
@requires('settings')
def wh_update(wid):
    d = request.get_json(silent=True) or {}
    p, err = _wh_payload(d)
    if err:
        return jsonify({'success': False, 'error': err}), 400
    with DB() as db:
        if db.one('SELECT id FROM warehouses WHERE LOWER(name) = LOWER($1) AND id != $2', (p[0], wid)):
            return jsonify({'success': False, 'error': 'A warehouse with that name already exists.'}), 409
        active = 1 if d.get('active', True) else 0
        if not active and db.one("SELECT id FROM orders WHERE warehouse_id = $1 AND status IN ('pick_list','picked','packed')", (wid,)):
            return jsonify({'success': False, 'error': 'This warehouse has open orders. Finish or cancel them first.'}), 400
        db.run('UPDATE warehouses SET name = $1, address = $2, manager = $3, active = $4 WHERE id = $5', p + (active, wid))
        return jsonify({'success': True, 'warehouse': db.one('SELECT * FROM warehouses WHERE id = $1', (wid,))})


# ── inventory ─────────────────────────────────────────────────────────────────
# ── storage locations (zone / rack / shelf / bin) ─────────────────────────────
def _loc_code(zone, rack, shelf, bin_):
    return '-'.join(p for p in (zone, rack, shelf, bin_) if p)


def _clean(v):
    return (str(v).strip().upper() if v not in (None, '') else '')


def _loc_ok(db, wid, lid):
    """None/0 means Unassigned. Otherwise the location must belong to the warehouse."""
    if not lid:
        return True
    return bool(db.one('SELECT id FROM locations WHERE id = $1 AND warehouse_id = $2', (lid, wid)))


def _loc_onhand(db):
    """{(product_id, warehouse_id, location_id or 0): on_hand}"""
    out = {}
    for r in db.rows('SELECT product_id, warehouse_id, location_id, SUM(qty) AS q FROM stock_movements '
                     'GROUP BY product_id, warehouse_id, location_id'):
        k = (r['product_id'], r['warehouse_id'], r['location_id'] or 0)
        out[k] = out.get(k, 0) + int(r['q'] or 0)
    return out


def _loc_alloc(db):
    """Quantities already picked from a bin but not yet shipped: {(product_id, warehouse_id, location_id or 0): qty}"""
    out = {}
    for r in db.rows("SELECT p.product_id, o.warehouse_id, p.location_id, p.qty FROM order_picks p "
                     "JOIN orders o ON o.id = p.order_id WHERE o.status IN ('picked','packed')"):
        k = (r['product_id'], r['warehouse_id'], r['location_id'] or 0)
        out[k] = out.get(k, 0) + int(r['qty'] or 0)
    return out


def _loc_free(db, pid, wid, lid, onhand=None, alloc=None):
    onhand = onhand if onhand is not None else _loc_onhand(db)
    alloc = alloc if alloc is not None else _loc_alloc(db)
    k = (pid, wid, lid or 0)
    return onhand.get(k, 0) - alloc.get(k, 0)


def _suggest_bins(db, pid, wid, qty, onhand=None, alloc=None, codes=None):
    """Pick from real bins in code order first, Unassigned last. Returns [(location_id or None, code, qty)]."""
    onhand = onhand if onhand is not None else _loc_onhand(db)
    alloc = alloc if alloc is not None else _loc_alloc(db)
    if codes is None:
        codes = {r['id']: r['code'] for r in db.rows('SELECT id, code FROM locations WHERE warehouse_id = $1', (wid,))}
    cands = []
    for (p, w, l), q in onhand.items():
        if p == pid and w == wid:
            free = q - alloc.get((p, w, l), 0)
            if free > 0:
                cands.append((l, free))
    cands.sort(key=lambda c: (c[0] == 0, codes.get(c[0], '')))
    out, left = [], qty
    for l, free in cands:
        if left <= 0:
            break
        take = min(free, left)
        out.append((l or None, codes.get(l, 'Unassigned') if l else 'Unassigned', take))
        left -= take
    if left > 0:
        out.append((None, 'Unassigned', left))
    return out


@app.route('/api/warehouses/<int:wid>/locations')
def loc_list(wid):
    with DB() as db:
        locs = db.rows('SELECT * FROM locations WHERE warehouse_id = $1 ORDER BY active DESC, code', (wid,))
        onhand = _loc_onhand(db)
    units, prods = {}, {}
    for (p, w, l), q in onhand.items():
        if w == wid and q:
            units[l] = units.get(l, 0) + q
            prods.setdefault(l, set()).add(p)
    for r in locs:
        r['units'] = units.get(r['id'], 0)
        r['products'] = len(prods.get(r['id'], ()))
    return jsonify({'locations': locs, 'unassigned_units': units.get(0, 0),
                    'unassigned_products': len(prods.get(0, ()))})


@app.route('/api/warehouses/<int:wid>/locations', methods=['POST'])
@requires('stock.adjust')
def loc_create(wid):
    d = request.get_json(silent=True) or {}
    zone, rack, shelf, bin_ = (_clean(d.get(k)) for k in ('zone', 'rack', 'shelf', 'bin'))
    code = _clean(d.get('code')) or _loc_code(zone, rack, shelf, bin_)
    if not code:
        return jsonify({'success': False, 'error': 'Enter at least a zone, rack, shelf or bin.'}), 400
    with DB() as db:
        if not db.one('SELECT id FROM warehouses WHERE id = $1', (wid,)):
            return jsonify({'success': False, 'error': 'Warehouse not found.'}), 404
        if db.one('SELECT id FROM locations WHERE warehouse_id = $1 AND code = $2', (wid, code)):
            return jsonify({'success': False, 'error': 'Location ' + code + ' already exists in this warehouse.'}), 409
        lid = db.insert('INSERT INTO locations (warehouse_id, code, zone, rack, shelf, bin, description, active, created_at) '
                        'VALUES ($1,$2,$3,$4,$5,$6,$7,1,$8)',
                        (wid, code, zone, rack, shelf, bin_, (d.get('description') or '').strip(), _now()))
    return jsonify({'success': True, 'id': lid, 'code': code})


def _expand(spec):
    """'1-5' -> 1..5 ; 'A-D' -> A..D ; 'A,B,C' -> list ; '' -> ['']"""
    spec = (spec or '').strip().upper()
    if not spec:
        return ['']
    if ',' in spec:
        return [x.strip() for x in spec.split(',') if x.strip()]
    m = _re.fullmatch(r'(\d+)\s*-\s*(\d+)', spec)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        width = len(m.group(1))
        return [str(i).zfill(width) for i in range(a, b + 1)] if 0 <= b - a < 200 else []
    m = _re.fullmatch(r'([A-Z])\s*-\s*([A-Z])', spec)
    if m:
        return [chr(c) for c in range(ord(m.group(1)), ord(m.group(2)) + 1)]
    return [spec]


@app.route('/api/warehouses/<int:wid>/locations/generate', methods=['POST'])
@requires('stock.adjust')
def loc_generate(wid):
    d = request.get_json(silent=True) or {}
    zone, rack = _clean(d.get('zone')), _clean(d.get('rack'))
    shelves, bins = _expand(d.get('shelves')), _expand(d.get('bins'))
    if not shelves or not bins:
        return jsonify({'success': False, 'error': 'Use ranges like 1-5 or A-D, or a list like A,B,C.'}), 400
    combos = [(sh, b) for sh in shelves for b in bins]
    if len(combos) > 300:
        return jsonify({'success': False, 'error': 'That would create more than 300 locations. Use a smaller range.'}), 400
    made, skipped = 0, 0
    with DB() as db:
        if not db.one('SELECT id FROM warehouses WHERE id = $1', (wid,)):
            return jsonify({'success': False, 'error': 'Warehouse not found.'}), 404
        existing = {r['code'] for r in db.rows('SELECT code FROM locations WHERE warehouse_id = $1', (wid,))}
        for sh, b in combos:
            code = _loc_code(zone, rack, sh, b)
            if not code or code in existing:
                skipped += 1
                continue
            db.run('INSERT INTO locations (warehouse_id, code, zone, rack, shelf, bin, description, active, created_at) '
                   'VALUES ($1,$2,$3,$4,$5,$6,$7,1,$8)', (wid, code, zone, rack, sh, b, '', _now()))
            existing.add(code)
            made += 1
    return jsonify({'success': True, 'created': made, 'skipped': skipped})


@app.route('/api/locations/<int:lid>', methods=['PUT'])
@requires('stock.adjust')
def loc_update(lid):
    d = request.get_json(silent=True) or {}
    with DB() as db:
        loc = db.one('SELECT * FROM locations WHERE id = $1', (lid,))
        if not loc:
            return jsonify({'success': False, 'error': 'Location not found.'}), 404
        active = 1 if d.get('active', bool(loc['active'])) else 0
        if not active and loc['active']:
            r = db.one('SELECT COALESCE(SUM(qty),0) AS q FROM stock_movements WHERE location_id = $1', (lid,))
            if int(r['q'] or 0) != 0:
                return jsonify({'success': False, 'error': 'Move the stock out of ' + loc['code'] + ' before deactivating it.'}), 400
        db.run('UPDATE locations SET description = $1, active = $2 WHERE id = $3',
               ((d.get('description') if 'description' in d else loc.get('description')) or '', active, lid))
    return jsonify({'success': True})


def _stock_map(db):
    """{(product_id, warehouse_id): {'on_hand': n, 'reserved': n}}"""
    out = {}
    for r in db.rows('SELECT product_id, warehouse_id, SUM(qty) AS q FROM stock_movements GROUP BY product_id, warehouse_id'):
        out.setdefault((r['product_id'], r['warehouse_id']), {'on_hand': 0, 'reserved': 0})['on_hand'] = int(r['q'] or 0)
    for r in db.rows("SELECT o.warehouse_id, i.product_id, i.qty_requested, i.qty_picked "
                     "FROM order_items i JOIN orders o ON o.id = i.order_id "
                     "WHERE o.status IN ('pick_list','picked','packed')"):
        q = r['qty_picked'] if r['qty_picked'] is not None else r['qty_requested']
        out.setdefault((r['product_id'], r['warehouse_id']), {'on_hand': 0, 'reserved': 0})['reserved'] += int(q or 0)
    return out


def _available(db, product_id, warehouse_id, smap=None):
    s = (smap or _stock_map(db)).get((product_id, warehouse_id), {'on_hand': 0, 'reserved': 0})
    return s['on_hand'] - s['reserved']


def _total_on_hand(db, product_id):
    r = db.one('SELECT COALESCE(SUM(qty),0) AS q FROM stock_movements WHERE product_id = $1', (product_id,))
    return int(r['q'] or 0)


def _low_stock_check(product_id, before):
    """Email alert recipients when a product drops below its minimum."""
    with DB() as db:
        p = db.one('SELECT * FROM products WHERE id = $1', (product_id,))
        after = _total_on_hand(db, product_id)
    if not p or not p.get('min_qty') or not (before >= p['min_qty'] > after):
        return
    recipients = get_alert_emails()
    if not (email_configured() and recipients):
        return
    subject = 'Low stock: {} ({} left)'.format(p['name'], after)
    body = ('<p style="margin:0 0 14px">Stock has dropped below the minimum level.</p>' + _detail_rows([
        ('Product', _esc(p['name'])), ('SKU', _esc(p['sku'])),
        ('On hand', str(after)), ('Minimum', str(p['min_qty']))]))
    send_email(recipients, subject, _email_shell(subject, body), '{}\nMinimum: {}'.format(subject, p['min_qty']))


@app.route('/api/products')
def products_list():
    wid = request.args.get('warehouse_id', type=int)
    with DB() as db:
        prods = db.rows('SELECT * FROM products ORDER BY active DESC, name')
        smap = _stock_map(db)
        whs = db.rows('SELECT id, name FROM warehouses')
        lonhand = _loc_onhand(db)
        lalloc = _loc_alloc(db)
        codes = {r['id']: r['code'] for r in db.rows('SELECT id, code FROM locations')}
    out = []
    for p in prods:
        per = []
        tot = {'on_hand': 0, 'reserved': 0}
        for w in whs:
            s = smap.get((p['id'], w['id']))
            if not s:
                continue
            locs = sorted(({'location_id': l or None, 'code': codes.get(l, 'Unassigned') if l else 'Unassigned',
                            'on_hand': q, 'free': q - lalloc.get((pp, ww, l), 0)}
                           for (pp, ww, l), q in lonhand.items() if pp == p['id'] and ww == w['id'] and q),
                          key=lambda x: (x['location_id'] is None, x['code']))
            per.append({'warehouse_id': w['id'], 'warehouse': w['name'], 'on_hand': s['on_hand'],
                        'reserved': s['reserved'], 'available': s['on_hand'] - s['reserved'], 'locations': locs})
            if wid in (None, w['id']):
                tot['on_hand'] += s['on_hand']
                tot['reserved'] += s['reserved']
        p.update(tot)
        p['available'] = tot['on_hand'] - tot['reserved']
        p['by_warehouse'] = per
        p['stock_value'] = round(tot['on_hand'] * float(p.get('unit_value') or 0), 2)
        out.append(p)
    return jsonify(out)


def _product_payload(d):
    sku = (d.get('sku') or '').strip().upper()
    name = (d.get('name') or '').strip()
    if not sku or not name:
        return None, 'SKU and product name are required.'
    try:
        min_qty = max(0, int(d.get('min_qty') or 0))
        value = max(0.0, float(d.get('unit_value') or 0))
    except (TypeError, ValueError):
        return None, 'Minimum level and unit value must be numbers.'
    return (sku, name, (d.get('unit') or 'pcs').strip() or 'pcs', (d.get('category') or '').strip(), min_qty, value), None


@app.route('/api/products', methods=['POST'])
@requires('products.manage')
def products_create():
    d = request.get_json(silent=True) or {}
    p, err = _product_payload(d)
    if err:
        return jsonify({'success': False, 'error': err}), 400
    with DB() as db:
        if db.one('SELECT id FROM products WHERE sku = $1', (p[0],)):
            return jsonify({'success': False, 'error': 'That SKU already exists.'}), 409
        pid = db.insert('INSERT INTO products (sku, name, unit, category, min_qty, unit_value, active, created_at) '
                        'VALUES ($1,$2,$3,$4,$5,$6,1,$7)', p + (_now(),))
        for o in d.get('opening') or []:
            try:
                q, w = int(o.get('qty') or 0), int(o.get('warehouse_id'))
                lid = int(o.get('location_id') or 0) or None
            except (TypeError, ValueError):
                continue
            if q > 0 and db.one('SELECT id FROM warehouses WHERE id = $1', (w,)) and _loc_ok(db, w, lid):
                db.run("INSERT INTO stock_movements (product_id, warehouse_id, location_id, qty, type, reason, user_id, created_at) "
                       "VALUES ($1,$2,$3,$4,'opening','Opening stock',$5,$6)", (pid, w, lid, q, g.user['id'], _now()))
        return jsonify({'success': True, 'id': pid})


@app.route('/api/products/<int:pid>', methods=['PUT'])
@requires('products.manage')
def products_update(pid):
    d = request.get_json(silent=True) or {}
    p, err = _product_payload(d)
    if err:
        return jsonify({'success': False, 'error': err}), 400
    with DB() as db:
        if db.one('SELECT id FROM products WHERE sku = $1 AND id != $2', (p[0], pid)):
            return jsonify({'success': False, 'error': 'That SKU already exists.'}), 409
        db.run('UPDATE products SET sku=$1, name=$2, unit=$3, category=$4, min_qty=$5, unit_value=$6, active=$7 WHERE id=$8',
               p + (1 if d.get('active', True) else 0, pid))
    return jsonify({'success': True})


@app.route('/api/products/<int:pid>/movements')
def products_movements(pid):
    with DB() as db:
        rows = db.rows('SELECT m.*, w.name AS warehouse, u.name AS user_name, l.code AS location FROM stock_movements m '
                       'LEFT JOIN warehouses w ON w.id = m.warehouse_id LEFT JOIN users u ON u.id = m.user_id '
                       'LEFT JOIN locations l ON l.id = m.location_id '
                       'WHERE m.product_id = $1 ORDER BY m.id DESC', (pid,))
    return jsonify(rows[:300])


@app.route('/api/stock/adjust', methods=['POST'])
@requires('stock.adjust')
def stock_adjust():
    d = request.get_json(silent=True) or {}
    try:
        pid, wid, qty = int(d.get('product_id')), int(d.get('warehouse_id')), int(d.get('qty'))
        lid = int(d.get('location_id') or 0) or None
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Choose a product, warehouse and quantity.'}), 400
    reason = (d.get('reason') or '').strip()
    if qty == 0 or not reason:
        return jsonify({'success': False, 'error': 'Enter a non-zero quantity and a reason.'}), 400
    with DB() as db:
        if not db.one('SELECT id FROM products WHERE id = $1', (pid,)) or not db.one('SELECT id FROM warehouses WHERE id = $1 AND active = 1', (wid,)):
            return jsonify({'success': False, 'error': 'Product or warehouse not found.'}), 404
        if not _loc_ok(db, wid, lid):
            return jsonify({'success': False, 'error': 'That location isn’t in this warehouse.'}), 400
        before = _total_on_hand(db, pid)
        if qty < 0:
            if _available(db, pid, wid) + qty < 0:
                return jsonify({'success': False, 'error': 'Not enough available stock to remove that many (some may be reserved for orders).'}), 400
            if _loc_free(db, pid, wid, lid) + qty < 0:
                return jsonify({'success': False, 'error': 'Not enough stock in that location.'}), 400
        db.run("INSERT INTO stock_movements (product_id, warehouse_id, location_id, qty, type, reason, note, user_id, created_at) "
               "VALUES ($1,$2,$3,$4,'adjust',$5,$6,$7,$8)", (pid, wid, lid, qty, reason, (d.get('note') or '').strip(), g.user['id'], _now()))
    _low_stock_check(pid, before)
    return jsonify({'success': True})


@app.route('/api/stock/transfer', methods=['POST'])
@requires('stock.adjust')
def stock_transfer():
    d = request.get_json(silent=True) or {}
    try:
        pid, src, dst, qty = int(d.get('product_id')), int(d.get('from_id')), int(d.get('to_id')), int(d.get('qty'))
        sl = int(d.get('from_location_id') or 0) or None
        dl = int(d.get('to_location_id') or 0) or None
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Choose a product, both warehouses and a quantity.'}), 400
    if qty <= 0 or (src == dst and sl == dl):
        return jsonify({'success': False, 'error': 'Choose a different destination and a quantity above zero.'}), 400
    with DB() as db:
        if not db.one('SELECT id FROM warehouses WHERE id = $1 AND active = 1', (dst,)):
            return jsonify({'success': False, 'error': 'Destination warehouse not found.'}), 404
        if not _loc_ok(db, src, sl) or not _loc_ok(db, dst, dl):
            return jsonify({'success': False, 'error': 'A selected location isn’t in that warehouse.'}), 400
        if src != dst and _available(db, pid, src) < qty:
            return jsonify({'success': False, 'error': 'Not enough available stock in the source warehouse.'}), 400
        if _loc_free(db, pid, src, sl) < qty:
            return jsonify({'success': False, 'error': 'Not enough stock in the source location.'}), 400
        note = (d.get('note') or '').strip()
        label = 'Moved between locations' if src == dst else None
        db.run("INSERT INTO stock_movements (product_id, warehouse_id, location_id, qty, type, reason, note, user_id, created_at) "
               "VALUES ($1,$2,$3,$4,'transfer',$5,$6,$7,$8)", (pid, src, sl, -qty, label or 'Transfer out', note, g.user['id'], _now()))
        db.run("INSERT INTO stock_movements (product_id, warehouse_id, location_id, qty, type, reason, note, user_id, created_at) "
               "VALUES ($1,$2,$3,$4,'transfer',$5,$6,$7,$8)", (pid, dst, dl, qty, label or 'Transfer in', note, g.user['id'], _now()))
    return jsonify({'success': True})


# ── orders (pick & pack) ──────────────────────────────────────────────────────
def _order_full(db, oid):
    o = db.one('SELECT o.*, w.name AS warehouse, u.name AS requester, u.email AS requester_email, '
               's.awb_no AS awb_no, s.status AS shipment_status '
               'FROM orders o LEFT JOIN warehouses w ON w.id = o.warehouse_id LEFT JOIN users u ON u.id = o.requester_id '
               'LEFT JOIN shipments s ON s.id = o.shipment_id WHERE o.id = $1', (oid,))
    if not o:
        return None
    o['items'] = db.rows('SELECT i.*, p.sku, p.name, p.unit FROM order_items i JOIN products p ON p.id = i.product_id '
                         'WHERE i.order_id = $1 ORDER BY i.id', (oid,))
    codes = {r['id']: r['code'] for r in db.rows('SELECT id, code FROM locations WHERE warehouse_id = $1', (o['warehouse_id'],))}
    picks = db.rows('SELECT * FROM order_picks WHERE order_id = $1 ORDER BY id', (oid,))
    if o['status'] == 'pick_list':
        onhand, alloc = _loc_onhand(db), _loc_alloc(db)
    for i in o['items']:
        if o['status'] == 'pick_list':
            i['bins'] = [{'code': c, 'qty': q} for _l, c, q in _suggest_bins(db, i['product_id'], o['warehouse_id'], i['qty_requested'], onhand, alloc, codes)]
        else:
            i['bins'] = [{'code': codes.get(p['location_id'], 'Unassigned') if p['location_id'] else 'Unassigned', 'qty': p['qty']}
                         for p in picks if p['order_item_id'] == i['id']]
    names = {r['id']: r['name'] for r in db.rows('SELECT id, name FROM users')}
    for k in ('picked_by', 'packed_by', 'shipped_by'):
        o[k + '_name'] = names.get(o.get(k))
    o['number'] = 'PL-{:04d}'.format(o['id'])
    o['status_label'] = ORDER_LABEL.get(o['status'], o['status'])
    return o


def _notify_roles(roles, subject, lines, extra=None):
    if not email_configured():
        return
    with DB() as db:
        to = [r['email'] for r in db.rows('SELECT email, role FROM users WHERE active = 1') if r['role'] in roles]
    to = sorted(set(to + (extra or [])))
    if to:
        body = _detail_rows([(k, _esc(v)) for k, v in lines])
        send_email(to, subject, _email_shell(subject, body), subject)


@app.route('/api/orders')
def orders_list():
    with DB() as db:
        rows = db.rows('SELECT o.*, w.name AS warehouse, u.name AS requester, s.awb_no AS awb_no FROM orders o '
                       'LEFT JOIN warehouses w ON w.id = o.warehouse_id LEFT JOIN users u ON u.id = o.requester_id '
                       'LEFT JOIN shipments s ON s.id = o.shipment_id ORDER BY o.id DESC')
        counts = {}
        for r in db.rows('SELECT order_id, COUNT(*) AS n, SUM(qty_requested) AS q FROM order_items GROUP BY order_id'):
            counts[r['order_id']] = r
    for r in rows:
        r['number'] = 'PL-{:04d}'.format(r['id'])
        r['status_label'] = ORDER_LABEL.get(r['status'], r['status'])
        c = counts.get(r['id']) or {}
        r['lines'] = int(c.get('n') or 0)
        r['units'] = int(c.get('q') or 0)
    return jsonify(rows)


@app.route('/api/orders/<int:oid>')
def orders_get(oid):
    with DB() as db:
        o = _order_full(db, oid)
    return (jsonify(o) if o else (jsonify({'error': 'Not found'}), 404))


@app.route('/api/orders', methods=['POST'])
@requires('orders.create')
def orders_create():
    d = request.get_json(silent=True) or {}
    try:
        wid = int(d.get('warehouse_id'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Choose a warehouse.'}), 400
    want = {}
    for it in d.get('items') or []:
        try:
            pid, q = int(it.get('product_id')), int(it.get('qty'))
        except (TypeError, ValueError):
            continue
        if q > 0:
            want[pid] = want.get(pid, 0) + q
    if not want:
        return jsonify({'success': False, 'error': 'Add at least one product with a quantity.'}), 400
    if not (d.get('destination') or '').strip():
        return jsonify({'success': False, 'error': 'Enter where the items are going.'}), 400
    with DB() as db:
        if not db.one('SELECT id FROM warehouses WHERE id = $1 AND active = 1', (wid,)):
            return jsonify({'success': False, 'error': 'Warehouse not found.'}), 404
        smap = _stock_map(db)
        for pid, q in want.items():
            p = db.one('SELECT name FROM products WHERE id = $1 AND active = 1', (pid,))
            if not p:
                return jsonify({'success': False, 'error': 'A selected product no longer exists.'}), 400
            avail = _available(db, pid, wid, smap)
            if avail < q:
                return jsonify({'success': False, 'error': 'Only {} of {} available in this warehouse.'.format(max(avail, 0), p['name'])}), 400
        oid = db.insert('INSERT INTO orders (warehouse_id, requester_id, destination, contact, urgency, notes, status, created_at, updated_at) '
                        "VALUES ($1,$2,$3,$4,$5,$6,'pick_list',$7,$8)",
                        (wid, g.user['id'], d['destination'].strip(), (d.get('contact') or '').strip(),
                         'Urgent' if d.get('urgency') == 'Urgent' else 'Normal', (d.get('notes') or '').strip(), _now(), _now()))
        for pid, q in want.items():
            db.run('INSERT INTO order_items (order_id, product_id, qty_requested) VALUES ($1,$2,$3)', (oid, pid, q))
        o = _order_full(db, oid)
    _notify_roles({'warehouse', 'admin'}, 'New pick list {} – {}'.format(o['number'], o['warehouse']),
                  [('Order', o['number']), ('Warehouse', o['warehouse']), ('Destination', o['destination']),
                   ('Urgency', o['urgency']), ('Requested by', o['requester'] or ''),
                   ('Items', ', '.join('{} × {}'.format(i['qty_requested'], i['name']) for i in o['items']))])
    return jsonify({'success': True, 'order': o})


def _order_for(db, oid, statuses):
    o = db.one('SELECT * FROM orders WHERE id = $1', (oid,))
    if not o:
        return None, (jsonify({'success': False, 'error': 'Order not found.'}), 404)
    if o['status'] not in statuses:
        return None, (jsonify({'success': False, 'error': 'This order is already “{}”. Refresh to see the latest.'.format(ORDER_LABEL.get(o['status'], o['status']))}), 409)
    return o, None


@app.route('/api/orders/<int:oid>/pick', methods=['POST'])
@requires('orders.process')
def orders_pick(oid):
    d = request.get_json(silent=True) or {}
    picked = {}
    for it in d.get('items') or []:
        try:
            picked[int(it.get('item_id'))] = int(it.get('qty_picked'))
        except (TypeError, ValueError):
            pass
    with DB() as db:
        o, err = _order_for(db, oid, ('pick_list',))
        if err:
            return err
        items = db.rows('SELECT * FROM order_items WHERE order_id = $1', (oid,))
        total = 0
        for i in items:
            q = picked.get(i['id'], i['qty_requested'])
            if q < 0 or q > i['qty_requested']:
                return jsonify({'success': False, 'error': 'Picked quantity must be between 0 and the requested quantity.'}), 400
            total += q
        if total == 0:
            return jsonify({'success': False, 'error': 'Nothing was picked. Cancel the order instead.'}), 400
        onhand, alloc = _loc_onhand(db), _loc_alloc(db)
        db.run('DELETE FROM order_picks WHERE order_id = $1', (oid,))
        for i in items:
            q = picked.get(i['id'], i['qty_requested'])
            db.run('UPDATE order_items SET qty_picked = $1 WHERE id = $2', (q, i['id']))
            if q:
                for lid, _code, take in _suggest_bins(db, i['product_id'], o['warehouse_id'], q, onhand, alloc):
                    db.run('INSERT INTO order_picks (order_id, order_item_id, product_id, location_id, qty) VALUES ($1,$2,$3,$4,$5)',
                           (oid, i['id'], i['product_id'], lid, take))
                    k = (i['product_id'], o['warehouse_id'], lid or 0)
                    alloc[k] = alloc.get(k, 0) + take
        db.run("UPDATE orders SET status = 'picked', picked_by = $1, picked_at = $2, updated_at = $3 WHERE id = $4",
               (g.user['id'], _now(), _now(), oid))
        o = _order_full(db, oid)
    return jsonify({'success': True, 'order': o})


@app.route('/api/orders/<int:oid>/pack', methods=['POST'])
@requires('orders.process')
def orders_pack(oid):
    d = request.get_json(silent=True) or {}
    try:
        boxes = int(d.get('boxes'))
        weight = float(d.get('weight')) if d.get('weight') not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Enter the number of boxes (and weight as a number).'}), 400
    if boxes < 1:
        return jsonify({'success': False, 'error': 'Enter at least 1 box.'}), 400
    with DB() as db:
        o, err = _order_for(db, oid, ('picked',))
        if err:
            return err
        db.run("UPDATE orders SET status = 'packed', boxes = $1, weight = $2, dimensions = $3, packed_by = $4, packed_at = $5, updated_at = $6 WHERE id = $7",
               (boxes, weight, (d.get('dimensions') or '').strip(), g.user['id'], _now(), _now(), oid))
        o = _order_full(db, oid)
    _notify_roles({'logistics', 'admin'}, 'Ready to ship: {} – {}'.format(o['number'], o['destination']),
                  [('Order', o['number']), ('Warehouse', o['warehouse']), ('Destination', o['destination']),
                   ('Contact', o.get('contact') or '—'), ('Boxes', str(boxes)), ('Weight (kg)', str(weight or '—')),
                   ('Urgency', o['urgency'])])
    return jsonify({'success': True, 'order': o})


@app.route('/api/orders/<int:oid>/ship', methods=['POST'])
@requires('orders.ship')
def orders_ship(oid):
    d = request.get_json(silent=True) or {}
    awb = (d.get('awb_no') or '').strip()
    if not awb:
        return jsonify({'success': False, 'error': 'Enter the AWB number.'}), 400
    befores = {}
    with DB() as db:
        o, err = _order_for(db, oid, ('packed',))
        if err:
            return err
        if db.one('SELECT id FROM shipments WHERE awb_no = $1', (awb,)):
            return jsonify({'success': False, 'error': 'AWB No. already exists'}), 409
        sid = db.insert('INSERT INTO shipments (date, awb_no, cost, status, order_id) VALUES ($1,$2,$3,$4,$5)',
                        ((d.get('date') or _now()[:10]), awb, d.get('cost') or None, 'Transit', oid))
        for pk in db.rows('SELECT * FROM order_picks WHERE order_id = $1', (oid,)):
            befores.setdefault(pk['product_id'], _total_on_hand(db, pk['product_id']))
            db.run("INSERT INTO stock_movements (product_id, warehouse_id, location_id, qty, type, reason, order_id, shipment_id, user_id, created_at) "
                   "VALUES ($1,$2,$3,$4,'ship','Shipped',$5,$6,$7,$8)",
                   (pk['product_id'], o['warehouse_id'], pk['location_id'], -int(pk['qty']), oid, sid, g.user['id'], _now()))
        db.run("UPDATE orders SET status = 'shipped', shipment_id = $1, shipped_by = $2, shipped_at = $3, updated_at = $4 WHERE id = $5",
               (sid, g.user['id'], _now(), _now(), oid))
        o = _order_full(db, oid)
        ship = db.one('SELECT * FROM shipments WHERE id = $1', (sid,))
    notify_shipment(ship, 'added')
    _notify_roles(set(), 'Your order {} has shipped (AWB {})'.format(o['number'], awb),
                  [('Order', o['number']), ('AWB No.', awb), ('Destination', o['destination']), ('Boxes', str(o.get('boxes') or '—'))],
                  extra=[o['requester_email']] if o.get('requester_email') else [])
    for pid, before in befores.items():
        _low_stock_check(pid, before)
    return jsonify({'success': True, 'order': o, 'shipment': ship})


@app.route('/api/orders/<int:oid>/cancel', methods=['POST'])
@requires('orders.cancel')
def orders_cancel(oid):
    reason = ((request.get_json(silent=True) or {}).get('reason') or '').strip()
    with DB() as db:
        o, err = _order_for(db, oid, ORDER_OPEN)
        if err:
            return err
        if g.user['role'] == 'requester' and o['requester_id'] != g.user['id']:
            return jsonify({'success': False, 'error': 'You can only cancel your own pick lists.'}), 403
        if g.user['role'] == 'requester' and o['status'] != 'pick_list':
            return jsonify({'success': False, 'error': 'Picking has started. Ask the warehouse to cancel it.'}), 403
        db.run('DELETE FROM order_picks WHERE order_id = $1', (oid,))
        db.run("UPDATE orders SET status = 'cancelled', cancel_reason = $1, updated_at = $2 WHERE id = $3", (reason, _now(), oid))
        o = _order_full(db, oid)
    return jsonify({'success': True, 'order': o})


@app.route('/api/orders/<int:oid>/return', methods=['POST'])
@requires('returns.receive')
def orders_return(oid):
    body = request.get_json(silent=True) or {}
    action = body.get('action')
    try:
        ret_loc = int(body.get('location_id') or 0) or None
    except (TypeError, ValueError):
        ret_loc = None
    if action not in ('restock', 'scrap'):
        return jsonify({'success': False, 'error': 'Choose restock or scrap.'}), 400
    with DB() as db:
        o, err = _order_for(db, oid, ('shipped',))
        if err:
            return err
        s = db.one('SELECT status FROM shipments WHERE id = $1', (o['shipment_id'],))
        if not s or s['status'] != 'Returned':
            return jsonify({'success': False, 'error': 'Mark the shipment as Returned first.'}), 400
        if o.get('return_action'):
            return jsonify({'success': False, 'error': 'This return was already recorded.'}), 409
        if action == 'restock':
            if not _loc_ok(db, o['warehouse_id'], ret_loc):
                return jsonify({'success': False, 'error': 'That location isn’t in this warehouse.'}), 400
            for i in db.rows('SELECT * FROM order_items WHERE order_id = $1', (oid,)):
                if i['qty_picked']:
                    db.run("INSERT INTO stock_movements (product_id, warehouse_id, location_id, qty, type, reason, order_id, shipment_id, user_id, created_at) "
                           "VALUES ($1,$2,$3,$4,'return','Returned to stock',$5,$6,$7,$8)",
                           (i['product_id'], o['warehouse_id'], ret_loc, int(i['qty_picked']), oid, o['shipment_id'], g.user['id'], _now()))
        db.run('UPDATE orders SET return_action = $1, updated_at = $2 WHERE id = $3', (action, _now(), oid))
        o = _order_full(db, oid)
    return jsonify({'success': True, 'order': o})


def unlink_order_shipment(db, sid):
    """Called before a shipment is deleted: put shipped stock back and reopen the order as packed."""
    o = db.one('SELECT * FROM orders WHERE shipment_id = $1', (sid,))
    if not o:
        return
    if o.get('return_action') != 'restock':
        for pk in db.rows('SELECT * FROM order_picks WHERE order_id = $1', (o['id'],)):
            db.run("INSERT INTO stock_movements (product_id, warehouse_id, location_id, qty, type, reason, order_id, user_id, created_at) "
                   "VALUES ($1,$2,$3,$4,'adjust','Shipment deleted',$5,$6,$7)",
                   (pk['product_id'], o['warehouse_id'], pk['location_id'], int(pk['qty']), o['id'], g.user['id'], _now()))
    db.run("UPDATE orders SET status = 'packed', shipment_id = NULL, shipped_by = NULL, shipped_at = NULL, "
           "return_action = NULL, updated_at = $1 WHERE id = $2", (_now(), o['id']))


def modules_dashboard():
    with DB() as db:
        counts = {r['status']: int(r['n']) for r in db.rows('SELECT status, COUNT(*) AS n FROM orders GROUP BY status')}
        prods = db.rows('SELECT id, min_qty, unit_value FROM products WHERE active = 1')
        smap = _stock_map(db)
    low = 0
    value = 0.0
    for p in prods:
        on_hand = sum(v['on_hand'] for (pid, _w), v in smap.items() if pid == p['id'])
        value += on_hand * float(p.get('unit_value') or 0)
        if p.get('min_qty') and on_hand < p['min_qty']:
            low += 1
    return {'to_pick': counts.get('pick_list', 0), 'to_pack': counts.get('picked', 0),
            'to_ship': counts.get('packed', 0), 'low_stock': low, 'stock_value': round(value, 2),
            'products': len(prods)}


# ── email (SMTP) ──────────────────────────────────────────────────────────────
# Configure in Vercel → Settings → Environment Variables:
#   SMTP_USER         sending address, e.g. you@gmail.com
#   SMTP_PASSWORD     Gmail app password (16 characters, not your normal password)
#   ALERT_EMAILS      comma-separated recipients for alerts and summary reports
#   SMTP_HOST / SMTP_PORT   optional, default smtp.gmail.com / 587
#   SMTP_FROM_NAME    optional sender name, default "Logistic Pro"
#   REPORT_FREQUENCY  optional "daily" (default) or "weekly" (sent on Mondays)
#   CRON_SECRET       optional; Vercel sends it to the cron job automatically
SMTP_HOST      = os.environ.get('SMTP_HOST', 'smtp.gmail.com').strip()
SMTP_PORT      = int(os.environ.get('SMTP_PORT', '587') or 587)
SMTP_USER      = os.environ.get('SMTP_USER', '').strip()
SMTP_PASSWORD  = os.environ.get('SMTP_PASSWORD', '').replace(' ', '')
SMTP_FROM_NAME = os.environ.get('SMTP_FROM_NAME', 'Logistic Pro').strip()
ALERT_EMAILS   = [e.strip() for e in os.environ.get('ALERT_EMAILS', '').split(',') if e.strip()]
REPORT_FREQ    = os.environ.get('REPORT_FREQUENCY', 'daily').strip().lower()
CRON_SECRET    = os.environ.get('CRON_SECRET', '').strip()
APP_URL        = os.environ.get('APP_URL', 'https://logisticproweb.vercel.app').rstrip('/')

_EMAIL_RE = None


def email_configured():
    return bool(SMTP_USER and SMTP_PASSWORD)


MAX_RECIPIENTS = 20


def get_setting(key):
    if USE_PG:
        conn = get_pg()
        try:
            row = pg_one(conn, 'SELECT value FROM app_settings WHERE key = $1', (key,))
        finally:
            conn.close()
        return row['value'] if row else None
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else None


def set_setting(key, value):
    if USE_PG:
        conn = get_pg()
        try:
            pg_run(conn, 'INSERT INTO app_settings (key, value) VALUES ($1, $2) '
                         'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value', (key, value))
        finally:
            conn.close()
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT INTO app_settings (key, value) VALUES (?, ?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def get_alert_emails():
    """Recipients saved on the Settings page; falls back to ALERT_EMAILS until first saved."""
    import json
    try:
        saved = get_setting('alert_emails')
    except Exception as exc:
        print('[email] could not read recipients:', exc)
        saved = None
    if saved is not None:
        try:
            return [e for e in json.loads(saved) if isinstance(e, str) and e]
        except ValueError:
            pass
    return list(ALERT_EMAILS)


def valid_email(addr):
    global _EMAIL_RE
    import re
    if _EMAIL_RE is None:
        _EMAIL_RE = re.compile(r'^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]{2,}$')
    return bool(_EMAIL_RE.match(addr or ''))


def send_email(to, subject, html, text=None, attachments=None):
    """Send one email. Returns (ok, error_message). Never raises."""
    import smtplib
    from email.message import EmailMessage
    from email.utils import formataddr
    if not email_configured():
        return False, 'Email is not set up yet (SMTP_USER / SMTP_PASSWORD missing).'
    to = [t for t in (to or []) if t]
    if not to:
        return False, 'No recipients.'
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = formataddr((SMTP_FROM_NAME, SMTP_USER))
    msg['To'] = ', '.join(to)
    msg.set_content(text or 'This email needs an HTML-capable mail app.')
    msg.add_alternative(html, subtype='html')
    for name, data, mime in attachments or []:
        maintype, subtype = mime.split('/', 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    try:
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
            server.starttls()
        with server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True, None
    except smtplib.SMTPAuthenticationError:
        return False, 'Gmail rejected the login. Check SMTP_USER and the app password.'
    except Exception as exc:
        return False, 'Could not send email: {}'.format(exc)


def _esc(v):
    import html as _h
    return _h.escape('' if v is None else str(v))


def _money(v):
    try:
        return '{:,.2f}'.format(float(v))
    except (TypeError, ValueError):
        return '—'


def _email_shell(title, body_html):
    """Wrap content in a simple, mail-client-safe layout."""
    return (
        '<div style="background:#f1f5fd;padding:24px 12px;font-family:Arial,Helvetica,sans-serif;color:#1e293b">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden">'
        '<tr><td style="background:#4361ee;padding:18px 24px;color:#ffffff">'
        '<div style="font-size:12px;opacity:.8">Logistic Pro</div>'
        '<div style="font-size:18px;font-weight:bold;margin-top:2px">' + _esc(title) + '</div></td></tr>'
        '<tr><td style="padding:22px 24px;font-size:14px;line-height:1.5">' + body_html + '</td></tr>'
        '<tr><td style="padding:14px 24px;border-top:1px solid #eaedf5;font-size:12px;color:#94a3b8">'
        '<a href="' + APP_URL + '" style="color:#4361ee;text-decoration:none">Open Logistic Pro</a></td></tr>'
        '</table></div>'
    )


def _detail_rows(pairs):
    cells = ''.join(
        '<tr><td style="padding:7px 0;color:#64748b;width:140px">' + _esc(k) + '</td>'
        '<td style="padding:7px 0;font-weight:bold">' + v + '</td></tr>'
        for k, v in pairs)
    return '<table role="presentation" cellpadding="0" cellspacing="0" width="100%">' + cells + '</table>'


def notify_shipment(row, event, old_status=None):
    """Email ALERT_EMAILS about a new shipment or a status change."""
    recipients = get_alert_emails()
    if not (email_configured() and recipients and row):
        return
    awb = row.get('awb_no') or '—'
    status = row.get('status') or '—'
    if event == 'added':
        subject = 'New shipment added: AWB {}'.format(awb)
        intro = 'A new shipment was added.'
        status_html = _esc(status)
    else:
        subject = 'Shipment {} is now {}'.format(awb, status)
        intro = 'A shipment’s status changed.'
        status_html = _esc(old_status or '—') + ' &rarr; ' + _esc(status)
    body = ('<p style="margin:0 0 14px">' + intro + '</p>' + _detail_rows([
        ('AWB No.', _esc(awb)),
        ('Date', _esc(row.get('date') or '—')),
        ('Cost (AED)', _money(row.get('cost'))),
        ('Status', status_html),
    ]))
    text = '{}\nAWB No.: {}\nDate: {}\nCost (AED): {}\nStatus: {}\n{}'.format(
        intro, awb, row.get('date') or '—', _money(row.get('cost')),
        (old_status + ' -> ' + status) if event != 'added' else status, APP_URL)
    ok, err = send_email(recipients, subject, _email_shell(subject, body), text)
    if not ok:
        print('[email] alert not sent:', err)


def all_shipments():
    if USE_PG:
        conn = get_pg()
        try:
            return pg_rows(conn, 'SELECT * FROM shipments ORDER BY date DESC')
        finally:
            conn.close()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute('SELECT * FROM shipments ORDER BY date DESC')]


def build_summary(rows, days):
    """Return (subject, html, text) for the summary report."""
    import datetime as dt
    today = dt.date.today()
    since = (today - dt.timedelta(days=days)).isoformat()
    period = [r for r in rows if (r.get('date') or '') >= since]
    def cost(rs):
        return sum(float(r.get('cost') or 0) for r in rs)
    def count(rs, st):
        return sum(1 for r in rs if r.get('status') == st)
    label = 'Daily' if days == 1 else 'Weekly'
    span = 'period since ' + (today - dt.timedelta(days=days)).strftime('%d %b')
    subject = '{} shipment summary – {}'.format(label, today.strftime('%d %b %Y'))

    def stat(n, l, color):
        return ('<td style="padding:12px;background:' + color + ';color:#fff;border-radius:8px;text-align:center">'
                '<div style="font-size:20px;font-weight:bold">' + n + '</div>'
                '<div style="font-size:11px">' + l + '</div></td>')
    cards = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="6"><tr>'
             + stat(str(len(rows)), 'Total Shipments', '#4361ee')
             + stat('{:,.0f}'.format(cost(rows)), 'Total Cost', '#7c3aed')
             + stat(str(count(rows, 'Transit')), 'In Transit', '#d97706')
             + stat(str(count(rows, 'Delivered')), 'Delivered', '#059669')
             + stat(str(count(rows, 'Returned')), 'Returned', '#dc2626')
             + '</tr></table>')
    period_html = ('<h3 style="font-size:15px;margin:22px 0 6px">In the ' + span + '</h3>' + _detail_rows([
        ('New shipments', str(len(period))),
        ('Cost (AED)', _money(cost(period))),
        ('Delivered', str(count(period, 'Delivered'))),
        ('Returned', str(count(period, 'Returned'))),
    ]))
    transit = [r for r in rows if r.get('status') == 'Transit']
    if transit:
        trs = ''.join(
            '<tr><td style="padding:6px 0;border-top:1px solid #eaedf5">' + _esc(r.get('awb_no')) + '</td>'
            '<td style="padding:6px 0;border-top:1px solid #eaedf5">' + _esc(r.get('date')) + '</td>'
            '<td style="padding:6px 0;border-top:1px solid #eaedf5;text-align:right">' + _money(r.get('cost')) + '</td></tr>'
            for r in transit[:50])
        transit_html = ('<h3 style="font-size:15px;margin:22px 0 6px">Still in transit (' + str(len(transit)) + ')</h3>'
                        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px">'
                        '<tr style="color:#64748b"><td>AWB No.</td><td>Date</td><td style="text-align:right">Cost (AED)</td></tr>'
                        + trs + '</table>')
    else:
        transit_html = '<p style="margin:22px 0 0;color:#64748b">No shipments are in transit.</p>'
    html = _email_shell(subject, cards + period_html + transit_html)
    text = ('{}\n\nTotal shipments: {}\nTotal cost (AED): {}\nIn transit: {}\nDelivered: {}\nReturned: {}\n\n'
            'In the {}: {} new shipments, AED {}\n\n{}').format(
        subject, len(rows), _money(cost(rows)), count(rows, 'Transit'), count(rows, 'Delivered'),
        count(rows, 'Returned'), span, len(period), _money(cost(period)), APP_URL)
    return subject, html, text


@app.route('/api/email/status')
def email_status():
    return jsonify({'configured': email_configured(), 'alerts': bool(get_alert_emails()),
                    'reportFrequency': 'weekly' if REPORT_FREQ == 'weekly' else 'daily'})


@app.route('/api/cron/summary')
def cron_summary():
    """Send the summary report. Called by Vercel Cron every day at 08:00 Dubai time."""
    if CRON_SECRET and request.headers.get('Authorization') != 'Bearer ' + CRON_SECRET:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    import datetime as dt
    weekly = REPORT_FREQ == 'weekly'
    force = request.args.get('force') == '1' and bool(CRON_SECRET)
    if weekly and dt.date.today().weekday() != 0 and not force:
        return jsonify({'success': True, 'skipped': 'weekly report is sent on Mondays'})
    recipients = get_alert_emails()
    if not (email_configured() and recipients):
        return jsonify({'success': False, 'error': 'Email or recipients not set up.'})
    subject, html, text = build_summary(all_shipments(), 7 if weekly else 1)
    ok, err = send_email(recipients, subject, html, text)
    return jsonify({'success': ok, 'error': err})


@app.route('/api/settings/recipients', methods=['GET'])
@requires('settings')
def get_recipients():
    return jsonify({'emails': get_alert_emails(), 'configured': email_configured(),
                    'max': MAX_RECIPIENTS})


@app.route('/api/settings/recipients', methods=['PUT'])
@requires('settings')
def save_recipients():
    import json
    d = request.get_json(silent=True) or {}
    raw = d.get('emails')
    if not isinstance(raw, list):
        return jsonify({'success': False, 'error': 'Invalid request.'}), 400
    emails, seen = [], set()
    for e in raw:
        e = str(e).strip()
        if not e:
            continue
        if not valid_email(e):
            return jsonify({'success': False, 'error': 'Not a valid email address: ' + e}), 400
        if e.lower() not in seen:
            seen.add(e.lower())
            emails.append(e)
    if len(emails) > MAX_RECIPIENTS:
        return jsonify({'success': False,
                        'error': 'You can add up to {} addresses.'.format(MAX_RECIPIENTS)}), 400
    set_setting('alert_emails', json.dumps(emails))
    return jsonify({'success': True, 'emails': emails})


@app.route('/api/settings/test-email', methods=['POST'])
@requires('settings')
def test_email():
    recipients = get_alert_emails()
    if not recipients:
        return jsonify({'success': False, 'error': 'Add at least one email address first.'}), 400
    subject = 'Test notification from Logistic Pro'
    body = ('<p style="margin:0 0 10px">Email notifications are working.</p>'
            '<p style="margin:0;color:#64748b">This address will receive new-shipment alerts, '
            'status changes and the summary report.</p>')
    ok, err = send_email(recipients, subject, _email_shell(subject, body),
                         'Email notifications from Logistic Pro are working.')
    if not ok:
        return jsonify({'success': False, 'error': err}), 502
    return jsonify({'success': True, 'sentTo': recipients})


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
@requires('shipments.edit')
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

    notify_shipment(row, 'added')
    return jsonify({'success': True, 'record': row})


@app.route('/api/shipments/<int:sid>', methods=['PUT'])
@requires('shipments.edit')
def update_shipment(sid):
    awb_file     = save_file('awb_file')
    invoice_file = save_file('invoice_file')
    d      = request.form
    awb_no = d.get('awb_no', '').strip()

    if USE_PG:
        conn = get_pg()
        try:
            prev = pg_one(conn, 'SELECT id, status FROM shipments WHERE id = $1', (sid,))
            if not prev:
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
            prev = conn.execute('SELECT id, status FROM shipments WHERE id=?', (sid,)).fetchone()
            if not prev:
                return jsonify({'success': False, 'error': 'Not found'}), 404
            prev = dict(prev)
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

    if row and (prev.get('status') or '') != (row.get('status') or ''):
        notify_shipment(row, 'status', prev.get('status'))
    return jsonify({'success': True, 'record': row})


@app.route('/api/shipments/<int:sid>', methods=['DELETE'])
@requires('shipments.delete')
def delete_shipment(sid):
    with DB() as db:
        unlink_order_shipment(db, sid)
        db.run('DELETE FROM shipments WHERE id = $1', (sid,))
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
        'monthly': monthly,
        'modules': modules_dashboard()
    })


@app.route('/uploads/<filename>')
def uploads(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


if __name__ == '__main__':
    ensure_db()
    ensure_modules_db()
    print('Logistic Pro running at http://localhost:5002')
    app.run(debug=True, port=5002)
