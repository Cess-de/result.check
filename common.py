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

    @functools.lru_cache(maxsize=6)
    def page_img(self, i, scale=S):
        return self.pdf[i].render(scale=scale).to_pil().convert('RGB')

    def clip(self, i, bbox, pad=0.5, scale=S):
        im = self.page_img(i, scale)
        x0, t, x1, b = bbox
        return im.crop((max(0, int((x0 - pad) * scale)), max(0, int((t - pad) * scale)),
                        int((x1 + pad) * scale), int((b + pad) * scale)))

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
