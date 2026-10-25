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
    if not (email_configured() and ALERT_EMAILS and row):
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
    ok, err = send_email(ALERT_EMAILS, subject, _email_shell(subject, body), text)
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
    return jsonify({'configured': email_configured(), 'alerts': bool(ALERT_EMAILS),
                    'reportFrequency': 'weekly' if REPORT_FREQ == 'weekly' else 'daily'})


@app.route('/api/email/export', methods=['POST'])
def email_export():
    """Email a CSV (generated in the browser from the current table view)."""
    d = request.get_json(silent=True) or {}
    to = [t.strip() for t in str(d.get('to', '')).replace(';', ',').split(',') if t.strip()]
    csv_text = d.get('csv') or ''
    count = int(d.get('count') or 0)
    if not to:
        return jsonify({'success': False, 'error': 'Enter at least one email address.'}), 400
    if len(to) > 5:
        return jsonify({'success': False, 'error': 'Send to 5 addresses or fewer.'}), 400
    bad = [t for t in to if not valid_email(t)]
    if bad:
        return jsonify({'success': False, 'error': 'Not a valid email address: ' + ', '.join(bad)}), 400
    if not csv_text or len(csv_text) > 2_000_000:
        return jsonify({'success': False, 'error': 'Nothing to send.'}), 400
    import datetime as dt
    fname = 'shipments-{}.csv'.format(dt.date.today().isoformat())
    subject = 'Shipment data export ({} record{})'.format(count, '' if count == 1 else 's')
    body = ('<p style="margin:0 0 10px">Your shipment data is attached as <b>' + _esc(fname) + '</b>.</p>'
            '<p style="margin:0;color:#64748b">' + str(count) + ' record' + ('' if count == 1 else 's')
            + '. Opens in Excel, Google Sheets or Numbers.</p>')
    ok, err = send_email(to, subject, _email_shell(subject, body),
                         'Your shipment data is attached ({} records).'.format(count),
                         [(fname, csv_text.encode('utf-8'), 'text/csv')])
    if not ok:
        return jsonify({'success': False, 'error': err}), 502
    return jsonify({'success': True})


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
    if not (email_configured() and ALERT_EMAILS):
        return jsonify({'success': False, 'error': 'Email or ALERT_EMAILS not set up.'})
    subject, html, text = build_summary(all_shipments(), 7 if weekly else 1)
    ok, err = send_email(ALERT_EMAILS, subject, html, text)
    return jsonify({'success': ok, 'error': err})


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

    notify_shipment(row, 'added')
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


@app.route('/uploads/<filename>')
def uploads(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


if __name__ == '__main__':
    ensure_db()
    print('Logistic Pro running at http://localhost:5002')
    app.run(debug=True, port=5002)
