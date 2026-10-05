"""Results server.  Run:  gunicorn server:app --workers 1 --threads 4
Env: DATA_DIR (default data), TRUST_PROXY=1 behind a proxy, TURNSTILE_SITE_KEY / TURNSTILE_SECRET (optional CAPTCHA)."""
import collections, io, json, os, pathlib, re, threading, time
from flask import Flask, jsonify, request, send_file, abort, Response
from common import Source, sha256, student_image, images_to_pdf, S, tight, build_base, general_pdf

BASE = pathlib.Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)
DATA = pathlib.Path(os.environ.get('DATA_DIR', BASE / 'data'))
LOCK = threading.Lock()          # pdfium is not thread-safe
BASE_LOCK = threading.Lock()
PDFGEN = threading.Semaphore(2)    # at most 2 PDF builds at once (each briefly uses ~40 MB)
CELLS = collections.OrderedDict()   # small server-side LRU of student crops (bytes), so bursts of visits are cheap
CELL_MAX = 400
COURSE_CACHE = {}                # course-name images are identical for everyone: render once
NOTFOUND = 'لم يتم العثور على نتيجة لهذا الرقم الجامعي.'
DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')
KNOWN = {'cum_pts': 'نقاط تراكمية', 'cum_hrs': 'ساعات تراكمية', 'cum_gpa': 'المعدل التراكمي', 'cum_gpa_prev_a': 'المعدل التراكمي الأسبق',
         'cum_gpa_prev_b': 'المعدل التراكمي السابق', 'sem_pts': 'نقاط الفصل', 'sem_hrs': 'ساعات الفصل', 'sem_gpa': 'معدل الفصل'}

# ---------- load datasets ----------
# Files are matched by a normalised name, so demo_s8 / demo-s8 / "demo_s8 (1)" still pair up.
# A dataset that is incomplete or whose PDF does not match its JSON (sha256) is NEVER served; it is skipped
# and reported in the log, and the rest of the site keeps working.
import sys


def norm(base):
    return re.sub(r'[^a-z0-9]', '', re.sub(r'\(\d+\)', '', base.lower()))


def log(*a):
    print('[results]', *a, file=sys.stderr, flush=True)


def ranks(j):
    """Unofficial rank by printed semester average; ties broken by cumulative average, then semester points.
    Enabled ONLY when every student who has results also has a semester average (a pending/partial sheet gets no ranking).
    Students with an F or a missing grade are excluded. Equal on every key => shared rank."""
    lab = {c['key']: c['idx'] for c in j['columns'] if c.get('key')}
    cc = [c['idx'] for c in j['columns'] if c['kind'] == 'course']
    if 'sem_gpa' not in lab:
        return {}
    def num(r, k):
        try: return float(r['cells'][lab[k]])
        except Exception: return 0.0
    active = [r for r in j['students'].values() if not r['no_result']]
    if not active or any(not r['cells'][lab['sem_gpa']] for r in active):
        return {}
    keys = {}
    for sid, r in j['students'].items():
        if r['no_result'] or any(not r['cells'][i] or r['cells'][i] == 'F' for i in cc):
            continue
        keys[sid] = (num(r, 'sem_gpa'), num(r, 'cum_gpa'), num(r, 'sem_pts'))
    return {sid: (1 + sum(k > v for k in keys.values()), sum(k == v for k in keys.values()) > 1) for sid, v in keys.items()}


DS, INDEX = {}, collections.defaultdict(list)
GROUPS = collections.defaultdict(dict)
for f in sorted(DATA.iterdir()) if DATA.exists() else []:
    n = f.name
    if n.lower().endswith('.general.pdf'): GROUPS[norm(n[:-12])]['general'] = f
    elif n.lower().endswith('.pdf'): GROUPS[norm(n[:-4])]['pdf'] = f
    elif n.lower().endswith('.json'): GROUPS[norm(n[:-5])]['json'] = f
log('files in data:', sorted(x.name for x in DATA.iterdir()) if DATA.exists() else 'NO data FOLDER')
for key, g in GROUPS.items():
    missing = [k for k in ('json', 'pdf', 'general') if k not in g]
    if missing:
        log(f'SKIPPED "{key}": missing {missing}. Needs 3 files: NAME.json, NAME.pdf, NAME.general.pdf')
        continue
    try:
        j = json.loads(g['json'].read_text('utf-8'))
        if sha256(g['pdf']) != j['meta']['sha256']:
            log(f'SKIPPED "{key}": the PDF does not match its JSON (sha256)')
            continue
        name = re.sub(r'[^A-Za-z0-9_-]', '', g['json'].stem) or key
        DS[name] = dict(j=j, src=Source(g['pdf']), general=g['general'], rank=ranks(j))
        for sid in j['students']:
            INDEX[sid].append(name)
        log(f'loaded "{name}": {len(j["students"])} students')
    except Exception as e:
        log(f'SKIPPED "{key}": {e!r}')

# ---------- abuse protection ----------
REQ, DLQ, FAIL = (collections.defaultdict(collections.deque) for _ in range(3))
# Campus Wi-Fi / mobile carriers put MANY students behind ONE public IP, so these are generous and configurable.
RATE_REQ = int(os.environ.get('RATE_REQ', 300))     # lookups per IP per 10 min
RATE_DL = int(os.environ.get('RATE_DL', 1500))      # image/PDF requests per IP per 10 min
FAIL_MAX = int(os.environ.get('FAIL_MAX', 40))      # unknown IDs per IP per 15 min before a temporary block


def ip():
    if os.environ.get('TRUST_PROXY'):
        return request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    return request.remote_addr or '?'


def hit(store, key, limit, window):
    now, q = time.time(), store[key]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def blocked(key):   # many unknown IDs in a row => someone is enumerating
    now, q = time.time(), FAIL[key]
    while q and q[0] < now - 900:
        q.popleft()
    return len(q) >= FAIL_MAX


def captcha_ok(token, addr):
    secret = os.environ.get('TURNSTILE_SECRET')
    if not secret:
        return True
    try:
        import requests
        r = requests.post('https://challenges.cloudflare.com/turnstile/v0/siteverify',
                          data=dict(secret=secret, response=token or '', remoteip=addr), timeout=5)
        return bool(r.json().get('success'))
    except Exception:
        return False


# ---------- views ----------
def view(j, sid):
    rec = j['students'][sid]
    v = dict(id=sid, name='', summary=[], courses=[], notes='', no_result=rec['no_result'])
    for c in j['columns']:
        val = rec['cells'][c['idx']]
        k = c['kind']
        if k == 'name': v['name'] = val
        elif k == 'note': v['notes'] = val
        elif k == 'course':
            info = j['courses'].get(c['course_no'], {})
            v['courses'].append(dict(no=c['course_no'], code=info.get('code', ''), name=info.get('name', ''),
                                     hours=info.get('hours', ''), grade=val,
                                     points=j['scale'].get(val, '')))
        elif k == 'summary':
            v['summary'].append(dict(label=c['label'], key=c.get('key'), value=val,
                                     meaning=KNOWN.get(c.get('key')) or j['abbr'].get(c['label'], '')))
    v['courses'].sort(key=lambda x: int(x['no']) if x['no'].isdigit() else 0)
    return v


def general(j):
    return dict(meta={k: v for k, v in j['meta'].items()}, courses=[{k: v for k, v in c.items() if k != 'name_box'}
                for c in j['courses'].values()], distribution=j['distribution'], abbr=j['abbr'], scale=j['scale'])


def links(ds, sid):
    return {k: f'/dl/{ds}/{sid}/{k}' for k in ('student.pdf', 'general.pdf', 'student.json', 'general.json')}


@app.after_request
def headers(r):
    r.headers['X-Content-Type-Options'] = 'nosniff'
    r.headers['X-Robots-Tag'] = 'noindex, nofollow'
    r.headers['Referrer-Policy'] = 'no-referrer'
    if '/course/' in request.path:
        r.headers['Cache-Control'] = 'public, max-age=86400'
    elif request.path.startswith(('/api', '/dl', '/cell')):
        r.headers['Cache-Control'] = 'no-store'
    else:
        r.headers['Content-Security-Policy'] = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                                                "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
                                                "frame-src https://challenges.cloudflare.com")
    return r


@app.get('/')
def index():
    return send_file(BASE / 'index.html', mimetype='text/html')


@app.get('/api/config')
def config():
    return jsonify(turnstile=os.environ.get('TURNSTILE_SITE_KEY', ''))


@app.post('/api/result')
def api_result():
    a = ip()
    if blocked(a) or not hit(REQ, a, RATE_REQ, 600):
        return jsonify(error='محاولات كثيرة. حاول مرة أخرى بعد قليل.'), 429
    body = request.get_json(silent=True) or {}
    if not captcha_ok(body.get('cf'), a):
        return jsonify(error='فشل التحقق الأمني، أعد المحاولة.'), 400
    sid = str(body.get('id', '')).strip().translate(DIGITS)
    if not re.fullmatch(r'\d{3,12}', sid) or sid not in INDEX:
        FAIL[a].append(time.time())
        return jsonify(error=NOTFOUND), 404
    out = []
    for ds in INDEX[sid]:
        j = DS[ds]['j']
        rk = DS[ds]['rank'].get(sid)   # only ranks 1-3 are ever sent to the client
        out.append(dict(dataset=ds, label=j['meta']['label'], student=view(j, sid),
                        rank=dict(n=rk[0], tie=rk[1]) if rk and rk[0] <= 3 else None, general=general(j),
                        source=dict(file=j['meta']['source_pdf'], sha256=j['meta']['sha256']), links=links(ds, sid),
                        name_img=f'/cell/{ds}/name/{sid}.png',
                        note_img=f'/cell/{ds}/note/{sid}.png' if j['students'][sid].get('note_box') else None, row_img=f'/cell/{ds}/row/{sid}.png',
                        course_imgs={c['no']: f'/cell/{ds}/course/{c["no"]}.png' for c in j['courses'].values() if c.get('name_box')}))
    return jsonify(results=out)


def to_bytes(im):
    b = io.BytesIO(); im.save(b, 'PNG', optimize=False); return b.getvalue()


def png(im):
    b = io.BytesIO(); im.save(b, 'PNG'); b.seek(0)
    return send_file(b, mimetype='image/png')


def guard(ds, sid):
    a = ip()
    if blocked(a) or not hit(DLQ, a, RATE_DL, 600):
        abort(429)
    d = DS.get(ds)
    if not d or sid not in d['j']['students']:
        FAIL[a].append(time.time())
        abort(404)
    return d


@app.get('/cell/<ds>/<kind>/<key>.png')
def cell(ds, kind, key):
    if kind == 'course':      # public data (same for every student of this dataset)
        d = DS.get(ds)
        c = d and d['j']['courses'].get(key)
        if not c or not c.get('name_box'):
            abort(404)
        k = (ds, key)
        if k not in COURSE_CACHE:
            with LOCK:
                COURSE_CACHE[k] = to_bytes(tight(d['src'].clip(c['name_box']['page'], c['name_box']['bbox'], pad=0, scale=4)))
        return send_file(io.BytesIO(COURSE_CACHE[k]), mimetype='image/png')
    d = guard(ds, key)
    rec = d['j']['students'][key]
    ck = (ds, kind, key)
    if ck not in CELLS:
        with LOCK:
            if kind == 'name' and rec.get('name_box'):
                im = tight(d['src'].clip(rec['name_box']['page'], rec['name_box']['bbox'], pad=0, scale=4))
            elif kind == 'note' and rec.get('note_box'):
                im = tight(d['src'].clip(rec['note_box']['page'], rec['note_box']['bbox'], pad=0, scale=4))
            elif kind == 'row':
                im = student_image(d['src'], d['j']['header'], rec, '', scale=3)
            else:
                abort(404)
            CELLS[ck] = to_bytes(im)
        while len(CELLS) > CELL_MAX:
            CELLS.popitem(last=False)
    return send_file(io.BytesIO(CELLS[ck]), mimetype='image/png')


@app.get('/dl/<ds>/<sid>/<kind>')
def download(ds, sid, kind):
    d = guard(ds, sid)
    j = d['j']
    if kind == 'general.pdf':
        rec = j['students'][sid]
        with BASE_LOCK:
            if 'base' not in d:
                with LOCK:
                    d['base'] = build_base(d['src'], j['masks'])
        with LOCK:
            row = d['src'].clip(rec['page'], rec['bbox'], pad=0.5, scale=3)
        with PDFGEN:
            b = io.BytesIO(); general_pdf(d['base'], j['masks'], rec, row, b); b.seek(0)
        return send_file(b, mimetype='application/pdf', as_attachment=True, download_name=f'{ds}_{sid}_stamps.pdf')
    if kind == 'student.pdf':
        rec = j['students'][sid]
        foot = f'Source: {j["meta"]["source_pdf"]} | page {rec["page"] + 1} | SHA-256 {j["meta"]["sha256"]}'
        with LOCK:
            im = student_image(d['src'], j['header'], rec, foot)
        with PDFGEN:
            b = io.BytesIO(); images_to_pdf([im], S, b); b.seek(0)
        del im
        return send_file(b, mimetype='application/pdf', as_attachment=True, download_name=f'{ds}_{sid}.pdf')
    if kind in ('student.json', 'general.json'):
        data = (dict(source=dict(file=j['meta']['source_pdf'], sha256=j['meta']['sha256'],
                                 page=j['students'][sid]['page'] + 1),
                     note_ar='النص العربي المستخرج قد يحتوي على اختلاف إملائي بسيط بسبب الملف الأصلي؛ المرجع هو صورة الصف في الملف الأصلي.',
                     student=view(j, sid)) if kind == 'student.json' else general(j))
        name = f'{ds}_{sid}.json' if kind == 'student.json' else f'{ds}_general.json'
        return Response(json.dumps(data, ensure_ascii=False, indent=1), mimetype='application/json',
                        headers={'Content-Disposition': f'attachment; filename="{name}"'})
    abort(404)


if __name__ == '__main__':
    app.run(port=int(os.environ.get('PORT', 8000)))
