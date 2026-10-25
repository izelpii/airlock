#!/usr/bin/env python3
"""
Small image edits from the command line: crop a region, shift the hue, or make
an image black and white.

    tools/image_tool.py crop top01.png --size 800x100 --at 0,40 -o banner.png
    tools/image_tool.py crop photo.jpg --size 800x100 --center
    tools/image_tool.py crop photo.jpg --size 800x100 --random     # anywhere it fits
    tools/image_tool.py hue  logo.png --degrees 120          # red -> green
    tools/image_tool.py bw   logo.png                         # greyscale
    tools/image_tool.py bw   logo.png --threshold 128         # pure black / white

Without -o the result is written next to the input as <name>-<operation>.<ext>,
never over the input itself.

Transparency is kept when the output format can hold it (PNG, WebP). A photo's
EXIF orientation is applied first, so crop coordinates are those of the image
as you see it, not as the camera stored it.

Needs Pillow (pip install Pillow).
"""

import argparse
import os
import random
import sys
import textwrap

try:
    from PIL import Image, ImageOps
except ImportError:
    # The usual cause is a different python3 on PATH than the one Pillow was
    # installed into (Homebrew's or the system one instead of pyenv's), so name
    # the interpreter that actually ran.
    import sysconfig
    _managed = os.path.exists(os.path.join(sysconfig.get_path('stdlib'),
                                           'EXTERNALLY-MANAGED'))
    # Homebrew's Python refuses pip installs into itself (PEP 668): a venv then.
    _install = (f'python3 -m venv ~/.venvs/image-tool && '
                f'~/.venvs/image-tool/bin/pip install Pillow\n'
                f'  then run:           ~/.venvs/image-tool/bin/python tools/image_tool.py ...'
                if _managed else f'{sys.executable} -m pip install Pillow')
    sys.exit(f'error: Pillow is not installed for {sys.executable} '
             f'(Python {sys.version.split()[0]}).\n'
             f'  Install it:         {_install}\n'
             f'  or run the tool with a Python that has it, e.g. '
             f'~/.pyenv/shims/python3 tools/image_tool.py ...')

# Formats that cannot store an alpha channel: the result is flattened onto white.
_NO_ALPHA = {'.jpg', '.jpeg', '.bmp'}


def _open(path):
    img = Image.open(path)
    img = ImageOps.exif_transpose(img) or img
    return img


def _split_alpha(img):
    """(colour image, alpha band or None)."""
    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
        rgba = img.convert('RGBA')
        return rgba.convert('RGB'), rgba.getchannel('A')
    return img.convert('RGB'), None


def _save(img, out_path):
    ext = os.path.splitext(out_path)[1].lower()
    if ext in _NO_ALPHA and img.mode in ('RGBA', 'LA'):
        background = Image.new('RGB', img.size, (255, 255, 255))
        background.paste(img, mask=img.getchannel('A'))
        img = background
    kwargs = {'quality': 95} if ext in ('.jpg', '.jpeg', '.webp') else {}
    img.save(out_path, **kwargs)
    print(f'{out_path}  ({img.width}x{img.height})')


def _default_out(path, suffix):
    stem, ext = os.path.splitext(path)
    return f'{stem}-{suffix}{ext}'


def _pair(text, sep, what):
    try:
        a, b = (int(v) for v in text.lower().split(sep))
    except ValueError:
        raise SystemExit(f'error: {what} must look like 800{sep}100, got {text!r}')
    return a, b


# ── Operations ────────────────────────────────────────────────────────────────

def crop(img, width, height, x=0, y=0, center=False, rand=None):
    """
    The width x height region at (x, y), centred on the image, or - given a
    random.Random as `rand` - at a random place where the whole region fits.

    Returns (image, x, y): where the region was taken from.
    """
    if width <= 0 or height <= 0:
        raise SystemExit('error: the size must be positive')
    if width > img.width or height > img.height:
        raise SystemExit(f'error: {width}x{height} is larger than the image '
                         f'({img.width}x{img.height})')
    if center:
        x, y = (img.width - width) // 2, (img.height - height) // 2
    elif rand is not None:
        # Every top-left corner that keeps the region inside the image is
        # equally likely, so the result always has the requested size.
        x = rand.randint(0, img.width - width)
        y = rand.randint(0, img.height - height)
    if x < 0 or y < 0 or x + width > img.width or y + height > img.height:
        raise SystemExit(f'error: {width}x{height} at {x},{y} runs off the image '
                         f'({img.width}x{img.height})')
    return img.crop((x, y, x + width, y + height)), x, y


def shift_hue(img, degrees):
    """Rotate every colour around the colour wheel; greys and alpha are untouched."""
    rgb, alpha = _split_alpha(img)
    h, s, v = rgb.convert('HSV').split()
    offset = round((degrees % 360) * 256 / 360)
    h = h.point(lambda value: (value + offset) % 256)
    out = Image.merge('HSV', (h, s, v)).convert('RGB')
    if alpha is not None:
        out.putalpha(alpha)
    return out


def black_and_white(img, threshold=None):
    """Greyscale, or with a threshold, pure black and white."""
    rgb, alpha = _split_alpha(img)
    grey = rgb.convert('L')
    if threshold is not None:
        grey = grey.point(lambda value: 255 if value >= threshold else 0)
    if alpha is not None:
        grey = Image.merge('LA', (grey, alpha))
    return grey


# ── Command line ──────────────────────────────────────────────────────────────

_EXAMPLES = """\
examples:
  crop a region 800 wide and 100 tall, starting 40 px from the top:
    tools/image_tool.py crop top01.png --size 800x100 --at 0,40 -o banner.png

  the same size, taken from the middle of the image:
    tools/image_tool.py crop photo.jpg --size 800x100 --center

  the same size, from a random place in the image (repeat for more pieces;
  --seed makes a pick repeatable):
    tools/image_tool.py crop photo.jpg --size 800x100 --random
    tools/image_tool.py crop photo.jpg --size 800x100 --random --seed 7

  shift every colour 120 degrees around the colour wheel (red -> green):
    tools/image_tool.py hue logo.png --degrees 120

  greyscale, or pure black and white:
    tools/image_tool.py bw logo.png
    tools/image_tool.py bw logo.png --threshold 128

notes:
  * Without -o the result goes next to the input as <name>-<operation>.<ext>
    (e.g. top01-crop800x100.png). The input is never overwritten.
  * The output format follows the -o extension: .png/.webp keep transparency,
    .jpg flattens it onto white.
  * A photo's EXIF rotation is applied first, so crop coordinates are those of
    the image as you see it.

run 'tools/image_tool.py <command> -h' for a command's own options.
"""


def _command(sub, name, summary, description, examples):
    # Raw formatting keeps the examples' layout, so the description is wrapped
    # here by hand.
    return sub.add_parser(
        name, help=summary, description=textwrap.fill(description, 79),
        epilog='examples:\n' + '\n'.join(f'  {e}' for e in examples),
        formatter_class=argparse.RawDescriptionHelpFormatter)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='tools/image_tool.py',
        description='Small image edits: crop a region, shift the hue, or make an '
                    'image black and white.',
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', metavar='COMMAND',
                                title='commands')

    p = _command(sub, 'crop', 'extract a region of the image (e.g. 800x100)',
                 'Extract a WIDTHxHEIGHT region and save it as a new image. The '
                 'region is placed by its top-left corner (--at), centred '
                 '(--center), or at a random place where it fits entirely '
                 '(--random), so the result always has the requested size. It '
                 'must fit inside the image.',
                 ['tools/image_tool.py crop top01.png --size 800x100 --at 0,40',
                  'tools/image_tool.py crop photo.jpg --size 800x100 --center -o strip.jpg',
                  'tools/image_tool.py crop photo.jpg --size 800x100 --random',
                  'for i in 1 2 3; do tools/image_tool.py crop photo.jpg --size 800x100 --random; done',
                  'tools/image_tool.py crop photo.jpg --size 800x100 --random --seed 7   # repeatable'])
    p.add_argument('image', help='the image to read')
    p.add_argument('--size', required=True, metavar='WxH',
                   help='size of the region in pixels, e.g. 800x100')
    where = p.add_mutually_exclusive_group()
    where.add_argument('--at', default='0,0', metavar='X,Y',
                       help='top-left corner of the region (default 0,0)')
    where.add_argument('--center', action='store_true',
                       help='take the region from the middle of the image')
    where.add_argument('--random', action='store_true',
                       help='take the region from a random place where it fits')
    p.add_argument('--seed', type=int, metavar='N',
                   help='with --random: the same seed picks the same place every time')
    p.add_argument('-o', '--output', metavar='FILE',
                   help='where to save (default <name>-crop<W>x<H>.<ext>; with '
                        '--random, <name>-crop<W>x<H>-at<X>,<Y>.<ext>)')

    p = _command(sub, 'hue', 'rotate the colours around the colour wheel',
                 'Shift the hue of every pixel by DEGREES. Saturation, brightness '
                 'and transparency are kept, so greys, black and white do not '
                 'change.',
                 ['tools/image_tool.py hue logo.png --degrees 120    # red -> green',
                  'tools/image_tool.py hue logo.png --degrees 240    # red -> blue',
                  'tools/image_tool.py hue logo.png --degrees 180    # opposite colours'])
    p.add_argument('image', help='the image to read')
    p.add_argument('--degrees', type=float, required=True, metavar='DEGREES',
                   help='how far to rotate, 0-360 (negative values rotate the other way)')
    p.add_argument('-o', '--output', metavar='FILE',
                   help='where to save (default <name>-hue<DEGREES>.<ext>)')

    p = _command(sub, 'bw', 'make the image black and white',
                 'Convert to greyscale. With --threshold, every pixel becomes '
                 'pure black or pure white instead (exact only in PNG: JPEG '
                 'compression adds near-black noise). Transparency is kept.',
                 ['tools/image_tool.py bw logo.png',
                  'tools/image_tool.py bw scan.png --threshold 128 -o scan-bw.png'])
    p.add_argument('image', help='the image to read')
    p.add_argument('--threshold', type=int, choices=range(0, 256), metavar='0-255',
                   help='pixels at least this bright become white, the rest black')
    p.add_argument('-o', '--output', metavar='FILE',
                   help='where to save (default <name>-bw.<ext>)')

    if not (argv if argv is not None else sys.argv[1:]):
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if not os.path.isfile(args.image):
        parser.error(f'{args.image} is not a file')
    img = _open(args.image)

    if args.command == 'crop':
        width, height = _pair(args.size, 'x', '--size')
        x, y = _pair(args.at, ',', '--at')
        if args.seed is not None and not args.random:
            parser.error('--seed only applies to --random')
        rand = random.Random(args.seed) if args.random else None
        result, x, y = crop(img, width, height, x, y, center=args.center, rand=rand)
        suffix = f'crop{width}x{height}'
        if args.random:
            # Where it came from, in the name: repeated runs give separate
            # pieces instead of overwriting one.
            print(f'random region: {width}x{height} at {x},{y}')
            suffix += f'-at{x},{y}'
    elif args.command == 'hue':
        result = shift_hue(img, args.degrees)
        suffix = f'hue{args.degrees:g}'
    else:
        result = black_and_white(img, args.threshold)
        suffix = 'bw' if args.threshold is None else f'bw{args.threshold}'

    out = args.output or _default_out(args.image, suffix)
    if os.path.abspath(out) == os.path.abspath(args.image):
        parser.error('the output would overwrite the input; choose another -o')
    _save(result, out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
