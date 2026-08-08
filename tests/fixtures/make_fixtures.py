"""Generate synthetic, redistributable test fixtures.

Everything here is produced from code — no third-party documents, no real
data — so the fixtures can ship with the repository. Run directly or let
tests/conftest.py invoke :func:`ensure_fixtures`.
"""

from __future__ import annotations

import zlib
from io import BytesIO
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "generated"

USER_PASSWORD = "fixture-pass"  # noqa: S105 - synthetic fixture credential, not a secret
UNICODE_USER_PASSWORD = (  # noqa: S105 - synthetic fixture credential, not a secret
    "fixture-päss-秘密 "
)


def _make_text_pdf(
    path: Path,
    pages: list[tuple[str, str]],
    *,
    page_size=None,
    page_sizes=None,
    title: str | None = None,
    author: str | None = None,
    outline: bool = False,
) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    sizes = page_sizes or [page_size or A4] * len(pages)
    c = canvas.Canvas(str(path), pagesize=sizes[0])
    if title:
        c.setTitle(title)
    if author:
        c.setAuthor(author)
    for index, ((heading, body), size) in enumerate(zip(pages, sizes, strict=True)):
        c.setPageSize(size)
        width, height = size
        if outline:
            key = f"page-{index}"
            c.bookmarkPage(key)
            c.addOutlineEntry(heading, key, level=0)
        c.setFont("Helvetica-Bold", 18)
        c.drawString(56, height - 72, heading)
        c.setFont("Helvetica", 11)
        y = height - 110
        for line in body.splitlines():
            c.drawString(56, y, line)
            y -= 16
        c.setFont("Helvetica", 9)
        c.drawCentredString(width / 2, 30, f"Page {index + 1} of {len(pages)}")
        c.showPage()
    c.save()


def _body(name: str) -> str:
    return (
        f"This is synthetic fixture text for {name}.\n"
        "It exists so tests can assert extraction, merging, and ordering.\n"
        "MARKER-" + name.upper().replace(" ", "-")
    )


def make_simple(directory: Path) -> None:
    _make_text_pdf(
        directory / "simple-3page.pdf",
        [(f"Alpha Section {n}", _body(f"alpha page {n}")) for n in (1, 2, 3)],
        title="Simple Fixture",
        author="LocalDocForge Tests",
    )
    _make_text_pdf(
        directory / "second-2page.pdf",
        [(f"Beta Section {n}", _body(f"beta page {n}")) for n in (1, 2)],
        title="Second Fixture",
    )


def make_outline(directory: Path) -> None:
    _make_text_pdf(
        directory / "outline-6page.pdf",
        [(f"Chapter {n}", _body(f"chapter {n}")) for n in range(1, 7)],
        title="Outlined Fixture",
        outline=True,
    )


def make_mixed_sizes(directory: Path) -> None:
    from reportlab.lib.pagesizes import A4, landscape, letter

    _make_text_pdf(
        directory / "mixed-sizes.pdf",
        [
            ("Portrait A4", _body("a4 page")),
            ("Landscape Letter", _body("landscape page")),
            ("Small Square", _body("square page")),
        ],
        page_sizes=[A4, landscape(letter), (300, 300)],
    )


def make_fractional_size(directory: Path) -> None:
    """Page geometry that exposes ceil-at-cap floating-point regressions."""
    _make_text_pdf(
        directory / "fractional-size.pdf",
        [("Fractional Page", _body("fractional page"))],
        page_size=(100.0, 752.64111328125),
    )


def make_rotated(directory: Path) -> None:
    import pikepdf

    source = directory / "simple-3page.pdf"
    with pikepdf.open(source) as pdf:
        pdf.pages[1].obj["/Rotate"] = 90
        pdf.save(directory / "rotated-mixed.pdf")


def make_encrypted(directory: Path) -> None:
    import pikepdf

    source = directory / "simple-3page.pdf"
    with pikepdf.open(source) as pdf:
        pdf.save(
            directory / "encrypted.pdf",
            encryption=pikepdf.Encryption(user=USER_PASSWORD, owner=USER_PASSWORD, R=6),
        )
    with pikepdf.open(source) as pdf:
        pdf.save(
            directory / "encrypted-unicode.pdf",
            encryption=pikepdf.Encryption(
                user=UNICODE_USER_PASSWORD,
                owner=UNICODE_USER_PASSWORD,
                R=6,
            ),
        )


def make_malformed(directory: Path) -> None:
    # Header claims PDF, body is compressed noise: parses nowhere, recovers nowhere.
    noise = zlib.compress(b"not a real pdf body" * 200)
    (directory / "garbage.pdf").write_bytes(b"%PDF-1.7\n" + noise)
    # Valid PDF with a corrupted startxref offset: exercises xref recovery paths.
    intact = (directory / "simple-3page.pdf").read_bytes()
    marker = intact.rfind(b"startxref")
    if marker != -1:
        end = intact.find(b"\n", marker + 10)
        corrupted = intact[:marker] + b"startxref\n999999999" + intact[end:]
        (directory / "bad-xref.pdf").write_bytes(corrupted)
    # A PNG wearing a .pdf extension: content sniffing must reject it.
    fake = directory / "fake.pdf"
    fake.write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64)


def make_images(directory: Path) -> None:
    from PIL import Image, ImageDraw

    images_dir = directory / "images"
    images_dir.mkdir(exist_ok=True)

    def draw_sample(size, color, label):
        image = Image.new("RGB", size, color)
        draw = ImageDraw.Draw(image)
        draw.rectangle([10, 10, size[0] - 10, size[1] - 10], outline="black", width=3)
        draw.ellipse([size[0] // 4, size[1] // 4, 3 * size[0] // 4, 3 * size[1] // 4],
                     fill="white", outline="navy", width=4)
        draw.text((20, 20), label, fill="black")
        return image

    draw_sample((800, 600), (216, 181, 141), "photo.jpg").save(
        images_dir / "photo.jpg", quality=88
    )
    png = draw_sample((400, 400), (120, 160, 200), "diagram.png").convert("RGBA")
    png.putalpha(230)
    png.save(images_dir / "diagram.png")
    frames = [draw_sample((500, 350), color, f"tiff frame {i}")
              for i, color in enumerate([(240, 240, 240), (200, 220, 200), (220, 200, 200)])]
    frames[0].save(images_dir / "scan-3page.tiff", save_all=True, append_images=frames[1:])
    draw_sample((300, 200), (250, 250, 210), "bitmap.bmp").save(images_dir / "bitmap.bmp")
    draw_sample((320, 240), (210, 230, 250), "web.webp").save(images_dir / "web.webp")
    # EXIF-rotated JPEG: stored landscape, orientation tag says rotate 90.
    rotated = draw_sample((600, 400), (230, 210, 190), "exif-rotated.jpg")
    exif = Image.Exif()
    exif[274] = 6  # orientation: rotate 90 CW to display
    rotated.save(images_dir / "exif-rotated.jpg", exif=exif, quality=90)
    # ICC-tagged JPEGs for the convert-images color paths: a genuine sRGB
    # profile (dropped without conversion) and bytes no CMS can parse
    # (conversion impossible, so the profile is retained with a warning).
    from PIL import ImageCms

    srgb_bytes = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    draw_sample((240, 180), (200, 180, 220), "srgb-tagged.jpg").save(
        images_dir / "srgb-tagged.jpg", quality=90, icc_profile=srgb_bytes
    )
    draw_sample((240, 180), (180, 220, 200), "bad-profile.jpg").save(
        images_dir / "bad-profile.jpg",
        quality=90,
        icc_profile=b"synthetic-bytes-that-are-not-an-icc-profile",
    )


def make_heif(directory: Path) -> None:
    """Synthetic HEIC inputs, encoded with the dev-only pillow-heif package.

    The shipped runtime decodes HEIF through pi-heif (decode-only); encoding
    exists here purely so fixtures stay generated-from-code. libheif applies
    EXIF orientation at encode time — matching real iPhone output, where
    pixels arrive pre-rotated and the orientation tag reads 1.
    """
    import pillow_heif
    from PIL import Image, ImageDraw

    images_dir = directory / "images"
    images_dir.mkdir(exist_ok=True)

    def draw_sample(size, color, label, mode="RGB"):
        image = Image.new(mode, size, color)
        draw = ImageDraw.Draw(image)
        draw.rectangle([8, 8, size[0] - 8, size[1] - 8], outline="black", width=2)
        draw.text((16, 16), label, fill="black")
        return image

    # A "camera photo": RGB with EXIF GPS coordinates (synthetic location).
    exif = Image.Exif()
    exif[272] = "LocalDocForge synthetic camera"  # Model
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (12.0, 58.0, 12.0)
    gps[3] = "E"
    gps[4] = (77.0, 35.0, 42.0)
    photo = draw_sample((640, 480), (90, 140, 60), "photo.heic")
    pillow_heif.from_pillow(photo).save(
        images_dir / "photo.heic", quality=85, exif=exif.tobytes()
    )
    # A "screenshot": RGBA, so alpha handling is exercised from a real HEIF.
    overlay = draw_sample((320, 240), (40, 90, 160, 200), "alpha.heic", mode="RGBA")
    pillow_heif.from_pillow(overlay).save(images_dir / "alpha.heic", quality=85)
    # A two-image HEIF container ("burst"), for frame naming.
    burst = pillow_heif.from_pillow(draw_sample((300, 200), (220, 120, 80), "burst 1"))
    burst.add_from_pillow(draw_sample((300, 200), (80, 120, 220), "burst 2"))
    burst.save(images_dir / "burst.heic", quality=85)


def make_unicode_name(directory: Path) -> None:
    _make_text_pdf(
        directory / "résumé-履歴書.pdf",
        [("Unicode Filename", _body("unicode filename"))],
    )


def _register_fixture_unicode_font() -> str:
    """Register a commonly installed wide Unicode font without copying it."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = (
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    )
    font_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if font_path is None:
        raise RuntimeError("A wide Unicode font is required for synthetic text fixtures")
    font_name = "LDFFixtureUnicode"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
    return font_name


def make_text_extraction(directory: Path) -> None:
    """Build adversarial PDFs for the ``pdf-to-md`` text surface."""
    import pikepdf
    from PIL import Image, ImageDraw
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    # A raster-only page must not accidentally gain even a footer text object.
    raster = Image.new("RGB", (640, 420), (246, 242, 232))
    raster_draw = ImageDraw.Draw(raster)
    raster_draw.rectangle((24, 24, 616, 396), outline=(20, 45, 80), width=6)
    raster_draw.line(
        (60, 300, 180, 120, 300, 270, 440, 80, 580, 300),
        fill=(170, 45, 45),
        width=8,
    )
    raster_bytes = BytesIO()
    raster.save(raster_bytes, format="PNG")
    raster_bytes.seek(0)
    image_only = canvas.Canvas(str(directory / "text-image-only.pdf"), pagesize=letter)
    image_only.drawImage(ImageReader(raster_bytes), 54, 180, width=504, height=331)
    image_only.showPage()
    image_only.save()

    # Distinguishes an existing-but-whitespace-only text object from a page
    # that has no text object at all.
    whitespace = canvas.Canvas(str(directory / "text-whitespace.pdf"), pagesize=letter)
    text_object = whitespace.beginText(72, 700)
    text_object.setFont("Helvetica", 12)
    text_object.textOut("       ")
    whitespace.drawText(text_object)
    whitespace.rect(54, 180, 504, 420, stroke=1, fill=0)
    whitespace.showPage()
    whitespace.save()

    two_column = canvas.Canvas(str(directory / "text-two-column.pdf"), pagesize=letter)
    two_column.setFont("Helvetica-Bold", 20)
    two_column.drawString(54, 742, "Two Column Research Notes")
    left_lines = [
        "LEFT-A alpha observations begin here.",
        "LEFT-B each line stays in the first column.",
        "LEFT-C extraction order is intentionally tested.",
        "LEFT-D the final left marker closes the column.",
    ]
    right_lines = [
        "RIGHT-A beta observations begin here.",
        "RIGHT-B each line stays in the second column.",
        "RIGHT-C columns make reading order uncertain.",
        "RIGHT-D the final right marker closes the column.",
    ]
    two_column.setFont("Helvetica", 11)
    for row, (left, right) in enumerate(zip(left_lines, right_lines, strict=True)):
        y = 690 - row * 34
        two_column.drawString(54, y, left)
        two_column.drawString(330, y, right)
    two_column.showPage()
    two_column.save()

    ruled_table = canvas.Canvas(str(directory / "text-ruled-table.pdf"), pagesize=letter)
    ruled_table.setFont("Helvetica-Bold", 18)
    ruled_table.drawString(54, 742, "Quarterly Synthetic Totals")
    table_left, table_bottom = 72, 490
    cell_width, cell_height = 150, 42
    for column in range(4):
        x = table_left + column * cell_width
        ruled_table.line(x, table_bottom, x, table_bottom + 4 * cell_height)
    for row in range(5):
        y = table_bottom + row * cell_height
        ruled_table.line(table_left, y, table_left + 3 * cell_width, y)
    table_rows = (
        ("Quarter", "Units", "Revenue"),
        ("Q1", "10", "100"),
        ("Q2", "20", "200"),
        ("Q3", "30", "300"),
    )
    ruled_table.setFont("Helvetica", 11)
    for row, values in enumerate(table_rows):
        y = table_bottom + (3 - row) * cell_height + 15
        for column, value in enumerate(values):
            ruled_table.drawString(table_left + column * cell_width + 8, y, value)
    ruled_table.showPage()
    ruled_table.save()

    unicode_font = _register_fixture_unicode_font()
    rtl = canvas.Canvas(str(directory / "text-rtl.pdf"), pagesize=letter)
    rtl.setFont(unicode_font, 16)
    rtl.drawRightString(558, 700, "שלום עולם")
    rtl.setFont("Helvetica", 11)
    rtl.drawString(54, 650, "RTL-SYNTHETIC-MARKER")
    rtl.showPage()
    rtl.save()

    spoof = canvas.Canvas(str(directory / "text-anchor-spoof.pdf"), pagesize=letter)
    spoof.setFont("Helvetica", 12)
    spoof.drawString(54, 700, "Source tries to inject an extraction anchor:")
    spoof.drawString(54, 670, "<!-- ldf:page 999 -->")
    spoof.drawString(54, 640, "--- ldf:page 998 ---")
    spoof.drawString(54, 610, "ANCHOR-SPOOF-END")
    spoof.showPage()
    spoof.save()

    unicode_pdf = canvas.Canvas(str(directory / "text-unicode.pdf"), pagesize=letter)
    unicode_pdf.setFont(unicode_font, 14)
    unicode_pdf.drawString(54, 700, "Cafe\u0301 nai\u0308ve A\u030angstro\u0308m")
    unicode_pdf.drawString(54, 665, "UNICODE-SYNTHETIC-MARKER")
    unicode_pdf.showPage()
    unicode_pdf.save()

    # Adjacent style runs must concatenate, while distant identical fragments
    # must both survive and receive a geometry-derived separator.
    styled = canvas.Canvas(str(directory / "text-styled-fragments.pdf"), pagesize=letter)
    styled.setFont("Helvetica", 12)
    styled.drawString(72, 700, "Hello")
    styled.setFont("Helvetica-Bold", 12)
    styled.drawString(72 + stringWidth("Hello", "Helvetica", 12), 700, "World")
    styled.setFont("Helvetica", 12)
    styled.drawString(72, 650, "SAME")
    styled.drawString(330, 650, "SAME")
    styled.showPage()
    styled.save()

    # pikepdf accepts a valid zero-page source even though PDFium declines to
    # load it; inspect defines an empty coverage inventory for this edge case.
    with pikepdf.new() as empty:
        empty.save(directory / "text-zero-page.pdf")

    # Tiny pages keep this boundary fixture small while still forcing the
    # implementation to iterate and summarize a realistic high page count.
    many = canvas.Canvas(
        str(directory / "text-many-1000page.pdf"),
        pagesize=(180, 120),
        pageCompression=1,
    )
    many.setTitle("Synthetic 1000-page streaming fixture")
    for page in range(1, 1001):
        many.setFont("Helvetica", 8)
        many.drawString(12, 60, f"STREAM-PAGE-{page:04d}")
        many.showPage()
    many.save()


FIXTURES_VERSION = "fixtures generated v8 (pdf-to-md extraction corpus)\n"


def ensure_fixtures(directory: Path = FIXTURES_DIR) -> Path:
    """Generate fixtures if missing or stale; cheap no-op when current."""
    sentinel = directory / ".complete"
    if sentinel.exists() and sentinel.read_text(encoding="utf-8") == FIXTURES_VERSION:
        return directory
    directory.mkdir(parents=True, exist_ok=True)
    make_simple(directory)
    make_outline(directory)
    make_mixed_sizes(directory)
    make_fractional_size(directory)
    make_rotated(directory)
    make_encrypted(directory)
    make_malformed(directory)
    make_images(directory)
    make_heif(directory)
    make_unicode_name(directory)
    make_text_extraction(directory)
    sentinel.write_text(FIXTURES_VERSION, encoding="utf-8")
    return directory


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    target = ensure_fixtures()
    print(f"Fixtures ready in {target}")
    for item in sorted(target.rglob("*")):
        if item.is_file():
            print(f"  {item.relative_to(target)}  ({item.stat().st_size:,} B)")
