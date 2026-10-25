"""Post staged episodes to TikTok from a headless browser with a saved login (no TikTok API, no audit).

    python -m atc_br.post_tiktok --login              # once, on the Mac: log in (QR from the phone) → tiktok_state.json
    python -m atc_br.post_tiktok tiktoks/staged/X ... # post these episode folders

In the cloud the login comes from the TIKTOK_STATE secret (the JSON from --login, base64).
Flow = what worked by hand: Studio upload page → file input → dismiss content-check prompt → caption → Post.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "tiktok_state.json"
UPLOAD = "https://www.tiktok.com/tiktokstudio/upload?from=webapp"
MAX_MB = 50
GAP_MIN = float(os.environ.get("POST_GAP_MIN", 20))   # minutes between posts in one run
log = lambda m: print(f"{datetime.now():%H:%M:%S} · {m}", flush=True)


def state_file():
    if os.environ.get("TIKTOK_STATE"):
        raw = os.environ["TIKTOK_STATE"].strip()
        STATE.write_text(raw if raw.startswith("{") else base64.b64decode(raw).decode())
    if not STATE.exists():
        raise SystemExit("no TikTok login: run --login on the Mac, then store tiktok_state.json as the TIKTOK_STATE secret")
    return STATE


def login():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False)
        ctx = b.new_context(viewport={"width": 1280, "height": 900}, locale="pt-BR")
        page = ctx.new_page()
        page.goto("https://www.tiktok.com/login/qrcode")
        log("log in in the window (scan the QR with the TikTok app). Waiting for the Studio page…")
        for _ in range(600):
            if any(c["name"] == "sessionid" for c in ctx.cookies()):
                break
            time.sleep(1)
        page.goto(UPLOAD)
        page.wait_for_timeout(4000)
        ctx.storage_state(path=str(STATE))
        b.close()
    log(f"saved {STATE} — base64 for the secret:\n{base64.b64encode(STATE.read_bytes()).decode()[:60]}…")


def caption(spec):
    return f"{spec['caption'].strip()}\n\n{' '.join(spec['hashtags'])}"


def small_copy(mp4):
    """Keep uploads light: re-encode anything over MAX_MB (same picture, CRF 21)."""
    if mp4.stat().st_size <= MAX_MB * 1e6:
        return mp4
    out = mp4.with_name("upload.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp4), "-c:v", "libx264", "-crf", "21", "-preset", "medium",
                    "-c:a", "copy", "-movflags", "+faststart", str(out)], check=True)
    return out


def _click_if(page, sel, timeout=1500):
    try:
        page.locator(sel).first.click(timeout=timeout)
        return True
    except Exception:
        return False


DISMISS = re.compile(r"^(Entendi|Ok|OK|Got it|Fechar|Close|Agora não|Not now|Pular|Skip|Concluir|Done)$")   # never a bare "Cancelar": that one cancels the upload


def dismiss(page):
    """Close the tour / tip / content-check dialogs TikTok puts on top (they block clicks), without touching the upload."""
    for _ in range(4):
        hit = False
        for sel, name in (("text=/cancelar o carregamento|cancel (the )?upload/i", re.compile(r"^(Não|No)$")),     # undo a stray cancel
                          ("text=/verifica[çc][õo]es autom|automatic content check/i", re.compile(r"^(Cancelar|Cancel)$"))):
            if page.locator(sel).count():
                for b in page.get_by_role("button", name=name).all():
                    try:
                        if b.is_visible():
                            b.click(timeout=1500)
                            hit = True
                    except Exception:
                        pass
        for b in page.get_by_role("button", name=DISMISS).all():
            try:
                if b.is_visible():
                    b.click(timeout=1500)
                    hit = True
            except Exception:
                pass
        if not hit:
            break
        page.wait_for_timeout(600)


def post_one(page, ep: Path, shots: Path):
    spec = json.loads((ep / "episode.json").read_text())
    mp4 = small_copy(ep / "video.mp4")
    page.goto(UPLOAD, wait_until="domcontentloaded")
    page.wait_for_selector("input[type=file]", state="attached", timeout=60000)
    page.set_input_files("input[type=file]", str(mp4))
    log(f"  uploading {mp4.name} ({mp4.stat().st_size / 1e6:.1f} MB)")
    editor = page.locator("div[contenteditable='true']").first
    editor.wait_for(timeout=120000)
    for _ in range(300):              # upload finished = "Enviado" / "Uploaded" under the file name
        dismiss(page)
        if page.locator("text=/^\\s*(Enviado|Uploaded)/").count():
            break
        time.sleep(1)
    page.wait_for_timeout(1500)
    dismiss(page)
    editor.evaluate("e => e.focus()")   # focus directly: a leftover overlay can't block it
    page.keyboard.press("Meta+A" if os.uname().sysname == "Darwin" else "Control+A")
    page.keyboard.press("Delete")
    for line in caption(spec).split("\n"):
        page.keyboard.type(line, delay=8)
        page.wait_for_timeout(400)
        page.keyboard.press("Shift+Enter")
    page.mouse.click(5, 5)                # close the hashtag suggestion list
    dismiss(page)
    btn = page.locator("button[data-e2e='post_video_button']")
    if not btn.count():
        btn = page.get_by_role("button", name=re.compile(r"^(Publicar|Post)$"))
    btn = btn.first
    page.screenshot(path=str(shots / f"{ep.name}-before.png"))
    btn.scroll_into_view_if_needed()
    btn.evaluate("b => b.click()")
    _click_if(page, "button:has-text('Publicar agora'), button:has-text('Post now')", 5000)
    for _ in range(90):
        if "/tiktokstudio/content" in page.url or page.locator("text=/publicado|posted|Gerenciar publica|Manage your posts/i").count():
            break
        time.sleep(1)
    page.screenshot(path=str(shots / f"{ep.name}-after.png"))
    ok = "/tiktokstudio/content" in page.url or page.locator("text=/publicado|posted/i").count() > 0
    log(f"  {'posted' if ok else 'NOT CONFIRMED'}: {spec['title']}")
    return ok, spec


def post_all(eps, state_path: Path):
    from playwright.sync_api import sync_playwright
    posted = json.loads(state_path.read_text()) if state_path.exists() else []
    shots = ROOT / "episodes" / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(storage_state=str(state_file()), viewport={"width": 1280, "height": 900}, locale="pt-BR",
                            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                                       "Chrome/131.0.0.0 Safari/537.36")
        page = ctx.new_page()
        for i, ep in enumerate(map(Path, eps)):
            if i:
                time.sleep(GAP_MIN * 60)
            log(f"post {ep.name}")
            try:
                ok, spec = post_one(page, ep, shots)
            except Exception as e:
                page.screenshot(path=str(shots / f"{ep.name}-error.png"))
                log(f"  failed: {e}")
                continue
            if ok:
                spec["status"], spec["posted"] = "posted", f"{datetime.now():%Y-%m-%d} TikTok @radioaereo"
                (ep / "episode.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2))
                posted.append({"title": spec["title"], "episode": ep.name, "at": datetime.now().isoformat(timespec="minutes")})
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps(posted, ensure_ascii=False, indent=1))
        ctx.storage_state(path=str(STATE))   # refreshed cookies; the workflow can save them back
        b.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("episodes", nargs="*")
    a = ap.parse_args()
    if a.login:
        login()
    else:
        post_all(a.episodes, ROOT / "episodes" / "posted.json")
