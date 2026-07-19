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


def make_unicode_name(directory: Path) -> None:
    _make_text_pdf(
        directory / "résumé-履歴書.pdf",
        [("Unicode Filename", _body("unicode filename"))],
    )


def ensure_fixtures(directory: Path = FIXTURES_DIR) -> Path:
    """Generate fixtures if missing; cheap no-op when present."""
    sentinel = directory / ".complete"
    if sentinel.exists():
        return directory
    directory.mkdir(parents=True, exist_ok=True)
    make_simple(directory)
    make_outline(directory)
    make_mixed_sizes(directory)
    make_rotated(directory)
    make_encrypted(directory)
    make_malformed(directory)
    make_images(directory)
    make_unicode_name(directory)
    sentinel.write_text("fixtures generated\n", encoding="utf-8")
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
