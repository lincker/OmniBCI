"""Build Command10's small, portable label PNGs using Pillow's bundled font."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from omnibci.infrastructure.plugins.paradigms.command10SSVEP.targets import TARGETS


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / 'omnibci/infrastructure/plugins/paradigms/command10SSVEP/resource'
SCALE = 4


def text_image(text, size, font_size):
    image = Image.new('RGBA', tuple(value * SCALE for value in size))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=font_size * SCALE)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    position = ((image.width - right + left) / 2 - left,
                (image.height - bottom + top) / 2 - top)
    draw.text(position, text, font=font, fill='white')
    return image.resize(size, Image.Resampling.LANCZOS)


def main():
    DESTINATION.mkdir(parents=True, exist_ok=True)
    arrow = Image.new('RGBA', (128 * SCALE, 128 * SCALE))
    points = ((64, 24), (96, 56), (76, 56), (76, 104), (52, 104), (52, 56), (32, 56))
    ImageDraw.Draw(arrow).polygon([(x * SCALE, y * SCALE) for x, y in points], fill='white')
    arrow = arrow.resize((128, 128), Image.Resampling.LANCZOS)
    directions = {
        'Up': arrow,
        'Down': arrow.transpose(Image.Transpose.ROTATE_180),
        'Left': arrow.transpose(Image.Transpose.ROTATE_90),
        'Right': arrow.transpose(Image.Transpose.ROTATE_270),
    }
    for target in TARGETS:
        image = directions.get(target.label)
        if image is None:
            image = text_image(target.label, (128, 128), 96)
        image.save(DESTINATION / f'target-{target.target_id:02}.png')
    text_image('Feedback', (256, 64), 40).save(DESTINATION / 'feedback.png')
    print(f'Built {len(TARGETS) + 1} label textures in {DESTINATION}')


if __name__ == '__main__':
    main()
