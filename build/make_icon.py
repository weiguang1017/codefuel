"""Generate the app icon for macOS (.icns) and Windows (.ico) from one drawing."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent
BASE = 1024


def rounded(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def draw_icon(size=BASE):
    scale = size / BASE
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Backdrop: near-black green, subtle inner highlight like a terminal window.
    rounded(draw, (0, 0, size - 1, size - 1), int(210 * scale), (23, 33, 32, 255))
    rounded(draw, (int(56 * scale), int(56 * scale), size - int(56 * scale), size - int(56 * scale)),
            int(160 * scale), (29, 42, 39, 255))

    # Code brackets + energy bolt: a daily fuel supply for developers.
    def points(coords):
        return [(int(x * scale), int(y * scale)) for x, y in coords]

    width = max(1, int(54 * scale))
    for bracket in (((330, 332), (174, 512), (330, 692)),
                    ((694, 332), (850, 512), (694, 692))):
        draw.line(points(bracket), fill=(150, 216, 160, 255), width=width, joint='curve')
        for x, y in points(bracket):
            r = width // 2
            draw.ellipse((x - r, y - r, x + r, y + r), fill=(150, 216, 160, 255))
    draw.polygon(points(((552, 230), (394, 540), (501, 540), (467, 794),
                         (637, 458), (530, 458))), fill=(238, 250, 240, 255))
    return image


def main():
    master = draw_icon()
    master.save(OUT / 'icon.png')

    iconset = OUT / 'icon.iconset'
    if not iconset.exists():
        iconset.mkdir(parents=True)
    for size in (16, 32, 128, 256, 512):
        master.resize((size, size), Image.LANCZOS).save(iconset / f'icon_{size}x{size}.png')
        master.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f'icon_{size}x{size}@2x.png')

    master.resize((256, 256), Image.LANCZOS).save(OUT / 'icon.ico',
                                                  sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print('icon.png / icon.ico ready; iconset ready for iconutil')


if __name__ == '__main__':
    main()
