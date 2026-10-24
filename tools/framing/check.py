#!/usr/bin/env python3
"""Burn safe-zone bands onto a video or still so violations are obvious.

    python3 check.py INPUT.mp4 OUT.png [--surface universal] [--frames 6]
    python3 check.py FRAME.png  OUT.png [--surface tiktok]

Video input renders a contact sheet sampled across the runtime.
"""
import sys, os, argparse, subprocess, tempfile, glob
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zones import SURFACES, scaled

FONTS = ["/System/Library/Fonts/Menlo.ttc",
         "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]

def font(sz):
    for p in FONTS:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, sz, index=1)
            except Exception:
                try: return ImageFont.truetype(p, sz)
                except Exception: pass
    return ImageFont.load_default()

def annotate(im, surfaces):
    im = im.convert('RGBA')
    w, h = im.size
    ov = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for key in surfaces:
        s = scaled(key, w, h)
        fill = s['col'] + (58,)
        d.rectangle((0, 0, w, s['t']), fill=fill)
        d.rectangle((0, h - s['b'], w, h), fill=fill)
        d.rectangle((0, s['t'], s['l'], h - s['b']), fill=fill)
        d.rectangle((w - s['r'], s['t'], w, h - s['b']), fill=fill)
        d.rectangle((s['l'], s['t'], w - s['r'], h - s['b']),
                    outline=s['col'] + (215,), width=max(2, w // 400))
        d.text((s['l'] + 6, s['t'] + 5), s['name'],
               font=font(max(11, w // 62)), fill=s['col'] + (255,))
    return Image.alpha_composite(im, ov).convert('RGB')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src'); ap.add_argument('out')
    ap.add_argument('--surface', default='universal',
                    help='comma-separated: ' + ','.join(SURFACES) + ', or "all"')
    ap.add_argument('--frames', type=int, default=6)
    a = ap.parse_args()

    surfaces = list(SURFACES) if a.surface == 'all' else [s.strip() for s in a.surface.split(',')]
    for s in surfaces:
        if s not in SURFACES:
            sys.exit(f'unknown surface {s!r}; choose from {", ".join(SURFACES)}')

    ext = os.path.splitext(a.src)[1].lower()
    if ext in ('.png', '.jpg', '.jpeg', '.webp'):
        annotate(Image.open(a.src), surfaces).save(a.out)
        print('wrote', a.out); return

    with tempfile.TemporaryDirectory() as td:
        dur = float(subprocess.check_output(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', a.src]).decode().strip())
        shots = []
        for i in range(a.frames):
            t = dur * (i + 0.5) / a.frames
            p = os.path.join(td, f's_{i:02d}.png')
            subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(t), '-i', a.src,
                            '-frames:v', '1', p, '-y'], check=True)
            if os.path.exists(p): shots.append(annotate(Image.open(p), surfaces))
        if not shots: sys.exit('no frames extracted')
        tw = shots[0].width // 2
        th = int(tw * shots[0].height / shots[0].width)
        sheet = Image.new('RGB', (tw * len(shots), th), (0, 0, 0))
        for i, s in enumerate(shots):
            sheet.paste(s.resize((tw, th), Image.LANCZOS), (i * tw, 0))
        d = ImageDraw.Draw(sheet)
        for i in range(len(shots)):
            d.text((i * tw + 8, th - 26), f'{dur*(i+0.5)/len(shots):.2f}s',
                   font=font(max(13, tw // 26)), fill=(255, 255, 255))
        sheet.save(a.out)
        print('wrote', a.out, f'({len(shots)} frames across {dur:.1f}s)')

if __name__ == '__main__':
    main()
