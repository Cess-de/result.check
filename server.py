"""Results server.  Run:  gunicorn server:app --workers 1 --threads 4
Env: DATA_DIR (default data), TRUST_PROXY=1 behind a proxy, TURNSTILE_SITE_KEY / TURNSTILE_SECRET (optional CAPTCHA)."""
import collections, io, json, os, pathlib, re, threading, time
from flask import Flask, jsonify, request, send_file, abort, Response
from common import Source, sha256, student_image, images_to_pdf, S

BASE = pathlib.Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)
DATA = pathlib.Path(os.environ.get('DATA_DIR', BASE / 'data'))
LOCK = threading.Lock()          # pdfium is not thread-safe
NOTFOUND = 'لم يتم العثور على نتيجة لهذا الرقم الجامعي.'
DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')
KNOWN = {'ن.ت': 'نقاط تراكمية', 'س.ت': 'ساعات تراكمية', 'م.ت': 'المعدل التراكمي', 'م.ت.ا': 'المعدل التراكمي الأسبق',
         'م.ت.س': 'المعدل التراكمي السابق', 'ن.ف': 'نقاط الفصل', 'س.ف': 'ساعات الفصل', 'م.ف': 'معدل الفصل'}

# ---------- load datasets (fail fast if JSON does not belong to its PDF) ----------
DS, INDEX = {}, collections.defaultdict(list)
for jf in sorted(DATA.glob('*.json')):
    name = jf.stem
    j = json.loads(jf.read_text('utf-8'))
    pdf, gen = DATA / f'{name}.pdf', DATA / f'{name}.general.pdf'
    if not pdf.exists() or not gen.exists():
        raise RuntimeError(f'{name}: missing pdf files')
    if sha256(pdf) != j['meta']['sha256']:
        raise RuntimeError(f'{name}: PDF does not match its JSON (sha256) - refusing to start')
    DS[name] = dict(j=j, src=Source(pdf), general=gen)
    for sid in j['students']:
        INDEX[sid].append(name)

# ---------- abuse protection ----------
REQ, DLQ, FAIL = (collections.defaultdict(collections.deque) for _ in range(3))


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
    return len(q) >= 10


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
            v['summary'].append(dict(label=c['label'], value=val,
                                     meaning=KNOWN.get(c['label']) or j['abbr'].get(c['label'], '')))
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
    if request.path.startswith(('/api', '/dl', '/cell')):
        r.headers['Cache-Control'] = 'no-store'
    else:
        r.headers['Content-Security-Policy'] = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
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
    if blocked(a) or not hit(REQ, a, 40, 600):
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
        out.append(dict(dataset=ds, label=j['meta']['label'], student=view(j, sid), general=general(j),
                        source=dict(file=j['meta']['source_pdf'], sha256=j['meta']['sha256']), links=links(ds, sid),
                        name_img=f'/cell/{ds}/name/{sid}.png', row_img=f'/cell/{ds}/row/{sid}.png',
                        course_imgs={c['no']: f'/cell/{ds}/course/{c["no"]}.png' for c in j['courses'].values() if c.get('name_box')}))
    return jsonify(results=out)


def png(im):
    b = io.BytesIO(); im.save(b, 'PNG'); b.seek(0)
    return send_file(b, mimetype='image/png')


def guard(ds, sid):
    a = ip()
    if blocked(a) or not hit(DLQ, a, 120, 600):
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
        with LOCK:
            return png(d['src'].clip(c['name_box']['page'], c['name_box']['bbox'], scale=3))
    d = guard(ds, key)
    rec = d['j']['students'][key]
    with LOCK:
        if kind == 'name' and rec.get('name_box'):
            return png(d['src'].clip(rec['name_box']['page'], rec['name_box']['bbox'], scale=3))
        if kind == 'row':
            return png(student_image(d['src'], d['j']['header'], rec, '', scale=3))
    abort(404)


@app.get('/dl/<ds>/<sid>/<kind>')
def download(ds, sid, kind):
    d = guard(ds, sid)
    j = d['j']
    if kind == 'general.pdf':
        return send_file(d['general'], mimetype='application/pdf', as_attachment=True, download_name=f'{ds}_general.pdf')
    if kind == 'student.pdf':
        rec = j['students'][sid]
        foot = f'Source: {j["meta"]["source_pdf"]} | page {rec["page"] + 1} | SHA-256 {j["meta"]["sha256"]}'
        with LOCK:
            im = student_image(d['src'], j['header'], rec, foot)
        b = io.BytesIO(); images_to_pdf([im], S, b); b.seek(0)
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
