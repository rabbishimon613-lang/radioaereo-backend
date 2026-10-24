"""Shared safe-zone table. Margins are in pixels against a 1080x1920 frame."""

W, H = 1080, 1920

SURFACES = {
    'tiktok':     dict(t=130, b=484, l=44, r=140, col=(91, 141, 239),  name='TIKTOK — ORGANIC'),
    'tiktok_ad':  dict(t=130, b=534, l=44, r=140, col=(64, 190, 220),  name='TIKTOK — PAID'),
    'instagram':  dict(t=108, b=320, l=60, r=120, col=(255, 144, 64),  name='INSTAGRAM REELS'),
    'stories':    dict(t=250, b=250, l=60, r=60,  col=(226, 96, 160),  name='INSTAGRAM STORIES'),
    'shorts':     dict(t=100, b=380, l=40, r=140, col=(220, 72, 72),   name='YOUTUBE SHORTS'),
    'universal':  dict(t=260, b=260, l=90, r=90,  col=(212, 201, 168), name='UNIVERSAL'),
}

def scaled(surface, w, h):
    """Margins scaled to an arbitrary frame size, preserving proportion."""
    s = SURFACES[surface]
    return dict(t=round(s['t'] * h / H), b=round(s['b'] * h / H),
                l=round(s['l'] * w / W), r=round(s['r'] * w / W),
                col=s['col'], name=s['name'])

def usable(surface):
    s = SURFACES[surface]
    return (W - s['l'] - s['r'], H - s['t'] - s['b'])
