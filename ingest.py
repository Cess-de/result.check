#!/usr/bin/env python3
"""Turn an official results PDF into a verified JSON dataset.

  python ingest.py results.pdf --program civil --term 1 --label "الهندسة المدنية - الفصل الأول"

Nothing is published unless ALL checks pass (use --force only after reading review/<name>/report.html).
Layout is detected from the PDF itself (header names, ID column, course table), not hard-coded.
"""
import argparse, base64, collections, datetime, html, io, json, pathlib, re, shutil, sys, unicodedata
import pdfplumber
from common import Source, sha256, student_image, general_images, images_to_pdf, GS, S

AR = re.compile('[\u0600-\u06ff\ufb50-\ufdff\ufe70-\ufeff]')
RUN = re.compile(r'[A-Za-z0-9.+*/-]+')
ANCHORS = ['رقم الطالب', 'اسم الطالب', 'اسم المادة', 'الرمز']
TOK = re.compile(r'[A-Za-z][A-Za-z+*]*|\d+(?:\.\d+)?')
IDRE = re.compile(r'\d{3,12}')
PAREN = str.maketrans('()', ')(')
# lam-alef ligatures must be expanded in *visual* order before the string is reversed
LIG = str.maketrans({'\ufefb': 'ال', '\ufefc': 'ال', '\ufef5': 'آل', '\ufef6': 'آل', '\ufef7': 'أل', '\ufef8': 'أل', '\ufef9': 'إل', '\ufefa': 'إل'})


def fixer(rev):
    """PDF stores Arabic in visual (reversed) order + presentation forms: normalise."""
    def f(s):
        if s is None:
            return ''
        s = re.sub(r'\s+', ' ', s.replace('\n', ' ')).strip()
        if not AR.search(s):
            return s
        if rev:
            s = s.translate(LIG)
        s = unicodedata.normalize('NFKC', s)
        if rev:
            s = RUN.sub(lambda m: m.group()[::-1], s[::-1]).translate(PAREN)
        return re.sub(r'\s+', ' ', s).strip()
    return f


def load(path):
    out = []
    with pdfplumber.open(path) as pdf:
        for pi, pg in enumerate(pdf.pages):
            for t in pg.find_tables():
                raw = t.extract()
                rows = [[float(x) for x in r.bbox] for r in t.rows]
                cells = [[[float(v) for v in c] if c else None for c in r.cells] for r in t.rows]
                if len(raw) == len(rows):
                    out.append(dict(page=pi, bbox=[float(x) for x in t.bbox], raw=raw, rows=rows, cells=cells))
    return out


def pick_mode(tables):
    score = {}
    for rev in (True, False):
        f = fixer(rev)
        score[rev] = sum(1 for t in tables for r in t['raw'] for c in r if f(c) in ANCHORS)
    return score[True] >= score[False], max(score.values())


def parse(T, errors, warnings):
    # ---- student table (possibly split over several pages) ----
    pieces = []
    for t in T:
        d = t['data']
        ncol = max((len(r) for r in d), default=0)
        best = None
        for j in range(ncol):
            vals = [r[j] for r in d if j < len(r) and IDRE.fullmatch(r[j])]
            key = (len(vals), len(set(vals)), 'رقم' in (d[0][j] if j < len(d[0]) else ''))
            if best is None or key > best[0]:
                best = (key, j)
        if best and best[0][0] >= 2:
            pieces.append((t, best[1]))
    if not pieces:
        errors.append('لم يتم العثور على جدول الطلاب (عمود الأرقام الجامعية)')
        return None
    t0, j0 = pieces[0]
    first = next(i for i, r in enumerate(t0['data']) if IDRE.fullmatch(r[j0]))
    if first == 0:
        errors.append('لم يتم العثور على صف عناوين الأعمدة')
        return None
    hdr = t0['data'][:first]
    ncol = len(t0['data'][0])
    cols = []
    for j in range(ncol):
        parts = []
        for r in hdr:
            if r[j] and r[j] not in parts:
                parts.append(r[j])
        label = '|'.join(parts)
        head = label.split('|')[0]
        if j == j0: k = 'id'
        elif 'اسم الطالب' in label: k = 'name'
        elif label == 'م': k = 'index'
        elif re.fullmatch(r'\d+', head): k = 'course'
        elif 'ملاحظات' in label: k = 'note'
        else: k = 'summary'
        c = dict(idx=j, label=label, kind=k)
        if k == 'course':
            c['course_no'] = head
        cols.append(c)
    for k in ('id', 'name'):
        if not any(c['kind'] == k for c in cols):
            errors.append(f'لم يتم التعرف على عمود: {k}')
    header = dict(page=t0['page'], bbox=[t0['bbox'][0], t0['rows'][0][1], t0['bbox'][2], t0['rows'][first - 1][3]])
    students, masks = {}, []
    for t, j in pieces:
        if len(t['data'][0]) != ncol:
            errors.append(f'عدد أعمدة الجدول في الصفحة {t["page"] + 1} يختلف عن الصفحة الأولى')
            continue
        ids = [i for i, r in enumerate(t['data']) if IDRE.fullmatch(r[j])]
        if not ids:
            continue
        for i in range(ids[0], len(t['data'])):
            r = t['data'][i]
            if i not in ids:
                if any(r):
                    warnings.append(f'صف بلا رقم جامعي تم تجاهله (صفحة {t["page"] + 1}): {" | ".join(x for x in r if x)}')
                continue
            sid = r[j]
            if sid in students:
                errors.append(f'رقم جامعي مكرر: {sid}')
                continue
            body = [r[c['idx']] for c in cols if c['kind'] in ('course', 'summary', 'note')]
            nj = next((c['idx'] for c in cols if c['kind'] == 'name'), None)
            nb = t['cells'][i][nj] if nj is not None else None
            students[sid] = dict(cells=r, page=t['page'], bbox=t['rows'][i], no_result=not any(body),
                                 name_box=dict(page=t['page'], bbox=nb) if nb else None)
        masks.append(dict(page=t['page'], bbox=[t['bbox'][0], t['rows'][ids[0]][1], t['bbox'][2], t['rows'][ids[-1]][3]]))
    # ---- other tables ----
    courses, dist, abbr, scale = {}, None, {}, {}
    for t in T:
        d = t['data']
        if not d or any(t is p[0] for p in pieces):
            continue
        h = d[0]
        if 'الرمز' in h and 'اسم المادة' in h:
            ci, ni = h.index('الرمز'), h.index('اسم المادة')
            hi = next((j for j, c in enumerate(h) if 'الساعات' in c), None)
            mi = h.index('م') if 'م' in h else None
            for i, r in enumerate(d):
                if i == 0:
                    continue
                no = r[mi] if mi is not None else str(len(courses) + 1)
                rec = dict(no=no, code=r[ci], name=r[ni], hours=r[hi] if hi is not None else '')
                nb = t['cells'][i][ni]
                rec['name_box'] = dict(page=t['page'], bbox=nb) if nb else None
                if no in courses and {k: v for k, v in courses[no].items() if k != 'name_box'} != {k: v for k, v in rec.items() if k != 'name_box'}:
                    errors.append(f'تعارض في جدول المواد للرقم {no}')
                courses[no] = rec
        elif 'المجموع' in h and 'اسم المادة' in h:
            ni = h.index('اسم المادة')
            dist = dict(columns=[c for j, c in enumerate(h) if j != ni],
                        rows=[dict(course=r[ni], counts={c: r[j] for j, c in enumerate(h) if j != ni}) for r in d[1:]])
        elif 'ن.ت' in h and 'س.ت' in h and len(d) > 1:
            abbr = dict(zip(h, d[1]))
        elif 'A+' in h and 'F' in h and len(d) > 1:
            scale = dict(zip(h, d[1]))
    return dict(columns=cols, header=header, students=students, masks=masks, courses=courses,
                distribution=dist, abbr=abbr, scale=scale)


def validate(src, P, errors, warnings):
    cols, S_ = P['columns'], P['students']
    lab = {c['label']: c['idx'] for c in cols}
    courses_cols = [c for c in cols if c['kind'] == 'course']
    ok_grade = (lambda g: g in P['scale']) if P['scale'] else (lambda g: re.fullmatch(r'[A-F]\+?', g) is not None)
    if not courses_cols:
        errors.append('لم يتم التعرف على أعمدة المواد')
    for c in courses_cols:
        if c['course_no'] not in P['courses']:
            errors.append(f'العمود {c["label"]} لا يقابله مقرر في جدول المواد')
    lens = collections.Counter(len(s) for s in S_)
    common = lens.most_common(1)[0][0] if lens else 0
    for sid, r in S_.items():
        cells = r['cells']
        # 1) independent re-extraction (pdfium) of all digits / latin tokens in the row
        a = collections.Counter(TOK.findall(src.text_in(r['page'], r['bbox'])))
        b = collections.Counter(TOK.findall(' '.join(cells)))
        if a != b:
            errors.append(f'{sid}: عدم تطابق بين محركي الاستخراج. فقط في الأول: {dict(b - a)} | فقط في الثاني: {dict(a - b)}')
        # 2) grades must be valid
        got = []
        for c in courses_cols:
            g = cells[c['idx']]
            got.append(g)
            if g and not ok_grade(g):
                errors.append(f'{sid}: تقدير غير معروف "{g}" في {c["label"]}')
        if not r['no_result'] and any(not g for g in got):
            warnings.append(f'{sid}: نتيجة ناقصة (بعض المواد بلا تقدير)')
        # 3) arithmetic consistency of the printed averages
        for n_, h_, m_ in (('ن.ف', 'س.ف', 'م.ف'), ('ن.ت', 'س.ت', 'م.ت')):
            if all(x in lab for x in (n_, h_, m_)) and all(cells[lab[x]] for x in (n_, h_, m_)):
                try:
                    n, h, m = (float(cells[lab[x]]) for x in (n_, h_, m_))
                    diff = 9 if h == 0 else abs(n / h - m)
                    if diff > 0.1:   # large gap => probably a column misalignment
                        errors.append(f'{sid}: {n_}/{h_} لا يطابق {m_} = {m} (فرق كبير)')
                    elif diff > 0.0101:   # the source document itself is not self-consistent
                        warnings.append(f'{sid}: في المصدر نفسه {n_}/{h_} = {n / h:.3f} بينما المطبوع {m_} = {m}')
                except ValueError:
                    errors.append(f'{sid}: قيمة غير رقمية في {n_}/{h_}/{m_}')
        if len(sid) != common:
            warnings.append(f'{sid}: طول الرقم الجامعي مختلف عن المعتاد، راجعه بصرياً')
    # 4) counts / distribution cross-check
    dist = P['distribution']
    if dist and 'المجموع' in dist['columns']:
        tot = {x['counts']['المجموع'] for x in dist['rows']}
        if len(tot) == 1 and next(iter(tot)).isdigit() and int(next(iter(tot))) != len(S_):
            warnings.append(f'عدد الطلاب المستخرج {len(S_)} ≠ المجموع في جدول التوزيع {next(iter(tot))}')
        if not any(r['no_result'] for r in S_.values()):
            for c in courses_cols:
                info = P['courses'].get(c['course_no'])
                row = next((x for x in dist['rows'] if info and x['course'] == info['name']), None)
                if row:
                    tally = collections.Counter(r['cells'][c['idx']] for r in S_.values())
                    for g in dist['columns']:
                        if g != 'المجموع' and str(tally.get(g, 0)) != row['counts'].get(g, '0'):
                            warnings.append(f'توزيع التقدير {g} في {info["name"]}: المستخرج {tally.get(g, 0)} مقابل {row["counts"].get(g)}')
    nr = sum(r['no_result'] for r in S_.values())
    if nr:
        warnings.append(f'{nr} طالب/طلاب في الكشف بلا درجات مسجلة (سيظهر لهم أن لا نتيجة مسجلة): ' +
                        ', '.join(s for s, r in S_.items() if r['no_result']))


def report(path, name, src, meta, P, errors, warnings):
    d = path / name
    d.mkdir(parents=True, exist_ok=True)
    rows = []
    for sid, r in P['students'].items():
        buf = io.BytesIO()
        student_image(src, P['header'], r, '', scale=2).save(buf, 'PNG')
        b64 = base64.b64encode(buf.getvalue()).decode()
        kv = ''.join(f'<tr><td>{html.escape(c["label"])}</td><td>{html.escape(r["cells"][c["idx"]])}</td></tr>'
                     for c in P['columns'])
        rows.append(f'<section><h3>{html.escape(sid)}</h3><img src="data:image/png;base64,{b64}" style="max-width:100%"><table border=1 cellpadding=3>{kv}</table></section>')
    li = lambda xs: ''.join(f'<li>{html.escape(x)}</li>' for x in xs) or '<li>لا شيء</li>'
    (d / 'report.html').write_text(
        f'<meta charset=utf-8><body dir=rtl style="font-family:sans-serif"><h1>تقرير المراجعة: {name}</h1>'
        f'<p>الطلاب: {len(P["students"])} — SHA-256: {meta["sha256"]}</p><h2>أخطاء</h2><ul>{li(errors)}</ul>'
        f'<h2>تنبيهات</h2><ul>{li(warnings)}</ul><h2>مقارنة الصف الأصلي بالبيانات المستخرجة</h2>{"".join(rows)}', 'utf-8')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pdf'); ap.add_argument('--program', required=True); ap.add_argument('--term', required=True)
    ap.add_argument('--label', default=''); ap.add_argument('--data', default='data'); ap.add_argument('--review', default='review')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()
    name = re.sub(r'[^A-Za-z0-9_-]', '', f'{a.program}_s{a.term}')
    errors, warnings = [], []
    tables = load(a.pdf)
    rev, hits = pick_mode(tables)
    if not hits:
        errors.append('لم يتم العثور على عناوين الجداول المعروفة')
    f = fixer(rev)
    T = [dict(t, data=[[f(c) for c in r] for r in t['raw']]) for t in tables]
    P = parse(T, errors, warnings)
    src = Source(a.pdf)
    if P:
        validate(src, P, errors, warnings)
    meta = dict(program=a.program, term=a.term, label=a.label or name, source_pdf=f'{name}.pdf', sha256=sha256(a.pdf),
                pages=len(src.pdf), generated=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                students=len(P['students']) if P else 0, forced=bool(a.force and errors))
    if P:
        report(pathlib.Path(a.review), name, src, meta, P, errors, warnings)
    print(f'الطلاب: {meta["students"]} | أخطاء: {len(errors)} | تنبيهات: {len(warnings)}')
    for e in errors: print('  [خطأ]', e)
    for w in warnings: print('  [تنبيه]', w)
    if errors and not a.force:
        print(f'\nلم يُنشر شيء. راجع {a.review}/{name}/report.html ثم صحّح الملف أو استخدم --force')
        sys.exit(1)
    out = pathlib.Path(a.data); out.mkdir(exist_ok=True)
    shutil.copyfile(a.pdf, out / f'{name}.pdf')
    images_to_pdf(general_images(src, P['masks']), GS, str(out / f'{name}.general.pdf'))
    (out / f'{name}.json').write_text(json.dumps(dict(meta=meta, **P), ensure_ascii=False, indent=1), 'utf-8')
    print(f'تم: {out / name}.json  + .pdf + .general.pdf')


if __name__ == '__main__':
    main()
