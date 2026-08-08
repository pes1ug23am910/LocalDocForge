"""Generate synthetic, redistributable test fixtures.

Everything here is produced from code — no third-party documents, no real
data — so the fixtures can ship with the repository. Run directly or let
tests/conftest.py invoke :func:`ensure_fixtures`.
"""

from __future__ import annotations

import zlib
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


FIXTURES_VERSION = "fixtures generated v4 (fractional page geometry)\n"


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
