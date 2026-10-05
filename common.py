"""Shared helpers (used by ingest.py and server.py)."""
import functools, hashlib, io
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

S = 4    # render scale for student crops (288 dpi)
GS = 3   # render scale for the general file


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


class Source:
    """Wraps the ORIGINAL pdf. Every crop is rendered straight from it."""

    def __init__(self, path):
        self.path = str(path)
        self.pdf = pdfium.PdfDocument(self.path)

    @functools.lru_cache(maxsize=1)
    def page_img(self, i, scale=S):
        return self.pdf[i].render(scale=scale).to_pil().convert('RGB')

    def clip(self, i, bbox, pad=0.5, scale=S):
        """Render ONLY the requested rectangle (never a whole page): small and fast, so memory stays low."""
        page = self.pdf[i]
        W, H = page.get_size()
        x0, t, x1, b = bbox
        l, tp, r, bt = max(0, x0 - pad), max(0, t - pad), min(W, x1 + pad), min(H, b + pad)
        return page.render(scale=scale, crop=(l, H - bt, W - r, tp)).to_pil().convert('RGB')

    def text_in(self, i, bbox, inset=1.0):
        """Independent text extraction (pdfium) inside a bbox (top-left coords)."""
        page = self.pdf[i]
        H = page.get_height()
        x0, t, x1, b = bbox
        return page.get_textpage().get_text_bounded(
            left=x0, bottom=H - b + inset, right=x1, top=H - t - inset)


def images_to_pdf(images, scale, path_or_buf):
    c = canvas.Canvas(path_or_buf)
    for im in images:
        w, h = im.width / scale, im.height / scale
        c.setPageSize((w, h))
        buf = io.BytesIO()
        im.save(buf, 'PNG')
        buf.seek(0)
        c.drawImage(ImageReader(buf), 0, 0, w, h)
        c.showPage()
    c.save()


def student_image(src, header, rec, footer, scale=S):
    """Column header + the student's own row, cut from the original page.
    Rendered as pixels so no other student's text can remain in the file."""
    parts = [src.clip(header['page'], header['bbox'], scale=scale),
             src.clip(rec['page'], rec['bbox'], scale=scale)]
    m = int(10 * scale)
    w = max(p.width for p in parts) + 2 * m
    try:
        font = ImageFont.load_default(size=int(6 * scale))
    except Exception:
        font = ImageFont.load_default()
    foot_h = int(14 * scale)
    h = sum(p.height for p in parts) + 2 * m + foot_h
    im = Image.new('RGB', (w, h), 'white')
    y = m
    for p in parts:
        im.paste(p, (m, y))
        y += p.height
    ImageDraw.Draw(im).text((m, y + int(3 * scale)), footer, fill=(90, 90, 90), font=font)
    return im


def general_images(src, masks, scale=GS):
    """All pages with the students' rows painted out (on the pixels)."""
    out = []
    for i in range(len(src.pdf)):
        im = src.page_img(i, scale).copy()
        d = ImageDraw.Draw(im)
        for m in masks:
            if m['page'] == i:
                x0, t, x1, b = m['bbox']
                d.rectangle([int((x0 - 1) * scale), int((t - 1) * scale),
                             int((x1 + 1) * scale), int((b + 1) * scale)], fill='white')
        out.append(im)
    return out


def tight(im, m=5):
    """Crop to the ink only (drops cell padding/borders)."""
    from PIL import ImageOps
    bb = ImageOps.invert(im.convert('L')).point(lambda p: 255 if p > 60 else 0).getbbox()
    if not bb:
        return im
    return im.crop((max(0, bb[0] - m), max(0, bb[1] - m), min(im.width, bb[2] + m), min(im.height, bb[3] + m)))


def general_with_student(src, masks, rec, scale=2.5):
    """Whole document (stamps, tables, scale...) where the ONLY student row left is this student's,
    placed right under the table header. Everything is pixels, so no other student's text survives."""
    out = []
    for i in range(len(src.pdf)):
        im = src.pdf[i].render(scale=scale).to_pil().convert('RGB')
        row = None
        if rec['page'] == i:
            x0, t, x1, b = rec['bbox']
            row = im.crop((int((x0 - 1) * scale), int((t - 0.5) * scale), int((x1 + 1) * scale), int((b + 0.5) * scale)))
        d = ImageDraw.Draw(im)
        for m in masks:
            if m['page'] == i:
                mx0, mt, mx1, mb = m['bbox']
                d.rectangle([int((mx0 - 1) * scale), int((mt - 1) * scale), int((mx1 + 1) * scale), int((mb + 1) * scale)], fill='white')
                if row is not None:
                    im.paste(row, (int((mx0 - 1) * scale), int((mt - 0.5) * scale)))
                    row = None
        out.append(im)
    return out
