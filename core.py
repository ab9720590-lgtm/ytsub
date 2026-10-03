"""YTSub core: transcript fetch, translate, subtitle writers, video/playlist download.

No UI code here, so it can be used from the CLI (cli.py) and the Android app (main.py).
"""
from __future__ import annotations

import html
import json
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

Log = Callable[[str], None]
Progress = Callable[[float], None]


def _noop(*_a, **_k):
    pass


class Cancelled(Exception):
    """Raised when the user cancels the running job."""


# ------------------------------------------------------------------ settings

@dataclass
class Settings:
    out_dir: str = ""
    download_video: bool = False
    get_subs: bool = True
    translate: bool = True
    bilingual: bool = False
    audio_only: bool = False
    auto_start: bool = True
    strip_noise: bool = False
    quality: int = 720
    target_lang: str = "ar"
    sub_format: str = "srt"          # srt | vtt | txt
    src_langs: list = field(default_factory=lambda: ["ar", "en"])
    workers: int = 4
    retries: int = 3

    @classmethod
    def load(cls, path) -> "Settings":
        s = cls()
        try:
            data = json.loads(Path(path).read_text("utf-8"))
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except Exception:
            pass
        return s

    def save(self, path) -> None:
        atomic_write(path, json.dumps(asdict(self), ensure_ascii=False, indent=2))


# ------------------------------------------------------------------ helpers

URL_RE = re.compile(r"https?://[^\s]+")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
TAG_RE = re.compile(r"<[^>]+>")
NOISE_RE = re.compile(r"^\s*[\[(][^\])]{1,30}[\])]\s*$")  # [Music] (applause)
BAD_FS_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def extract_url(text: str) -> Optional[str]:
    """First URL inside arbitrary shared text ("Watch this https://youtu.be/xxx")."""
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,;)]}") if m else None


def get_video_id(text: str) -> Optional[str]:
    text = (text or "").strip()
    if ID_RE.match(text):
        return text
    url = extract_url(text)
    if not url:
        return None
    p = urlparse(url)
    host = p.netloc.lower()
    parts = [x for x in p.path.split("/") if x]
    cand = None
    if host == "youtu.be" and parts:
        cand = parts[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        if len(parts) > 1 and parts[0] in ("shorts", "embed", "live", "v"):
            cand = parts[1]
        else:
            cand = parse_qs(p.query).get("v", [None])[0]
    return cand if cand and ID_RE.match(cand) else None


def get_playlist_id(text: str) -> Optional[str]:
    url = extract_url(text)
    if not url:
        return None
    pid = parse_qs(urlparse(url).query).get("list", [None])[0]
    return pid if pid and PLAYLIST_ID_RE.match(pid) else None


def sanitize(name: str, limit: int = 120) -> str:
    name = BAD_FS_CHARS.sub("_", name or "").strip(" ._")
    return (name[:limit].rstrip(" ._")) or "untitled"


def atomic_write(path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    tmp.replace(path)


def with_retry(fn, retries=3, delay=1.5, cancel: Optional[threading.Event] = None):
    for attempt in range(1, retries + 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        try:
            return fn()
        except Cancelled:
            raise
        except Exception:
            if attempt == retries:
                raise
            time.sleep(delay * attempt)


def is_arabic_text(text: str) -> bool:
    letters = re.findall(r"[^\W\d_]", text or "")
    if not letters:
        return True  # nothing to translate (music notes, numbers...)
    arabic = re.findall(r"[\u0600-\u06FF]", text)
    return len(arabic) / len(letters) > 0.5


# ------------------------------------------------------------------ cues

@dataclass
class Cue:
    start: float
    end: float
    text: str
    orig: str = ""


def format_time(seconds: float, comma: bool = True) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}{',' if comma else '.'}{ms:03}"


def clean_text(text: str, strip_noise: bool = False) -> str:
    text = html.unescape(TAG_RE.sub("", text or ""))
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if strip_noise and NOISE_RE.match(text):
        return ""
    return text


def fetch_cues(video_id: str, langs, retries=3, strip_noise=False,
               cancel=None, log: Log = _noop):
    """Returns (cues, language_code, is_generated)."""
    from youtube_transcript_api import YouTubeTranscriptApi

    def _get():
        api = YouTubeTranscriptApi()
        tlist = api.list(video_id)
        tr = None
        try:
            tr = tlist.find_manually_created_transcript(langs)
        except Exception:
            try:
                tr = tlist.find_generated_transcript(langs)
            except Exception:
                tr = next(iter(tlist), None)   # any language at all
        if tr is None:
            raise RuntimeError("لا توجد ترجمة لهذا الفيديو")
        return tr, tr.fetch()

    tr, data = with_retry(_get, retries, cancel=cancel)
    snippets = list(data)
    cues: list[Cue] = []
    for i, sn in enumerate(snippets):
        text = clean_text(sn.text, strip_noise)
        if not text:
            continue
        start = float(sn.start)
        end = start + float(sn.duration)
        if i + 1 < len(snippets):
            nxt = float(snippets[i + 1].start)
            if nxt > start:
                end = min(end, nxt)          # auto-captions overlap: clamp
        if end <= start + 0.2:
            end = start + 1.0
        cues.append(Cue(start, end, text, text))
    if not cues:
        raise RuntimeError("الترجمة فارغة")
    log(f"لغة الترجمة: {tr.language_code}" + (" (تلقائية)" if tr.is_generated else ""))
    return cues, tr.language_code, tr.is_generated


# ------------------------------------------------------------------ translate

def translate_cues(cues, target="ar", workers=4, retries=3,
                   progress: Progress = _noop, cancel=None, log: Log = _noop):
    """Batch + parallel translation. Returns new cues, keeps `orig`. """
    from deep_translator import GoogleTranslator

    texts = [c.text for c in cues]
    if target == "ar":
        todo = [i for i, t in enumerate(texts) if not is_arabic_text(t)]
    else:
        todo = [i for i, t in enumerate(texts) if t]

    chunks, cur, size = [], [], 0
    for i in todo:
        n = len(texts[i]) + 1
        if cur and (size + n > 4000 or len(cur) >= 40):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += n
    if cur:
        chunks.append(cur)

    result = list(texts)
    failed: list[int] = []

    def work(chunk):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        tr = GoogleTranslator(source="auto", target=target)
        out = None
        try:
            joined = "\n".join(texts[i] for i in chunk)
            out = with_retry(lambda: tr.translate(joined), retries, cancel=cancel).split("\n")
        except Cancelled:
            raise
        except Exception:
            out = None
        if out is None or len(out) != len(chunk):      # fallback: one by one
            out = []
            for i in chunk:
                try:
                    out.append(with_retry(lambda i=i: tr.translate(texts[i]), retries, cancel=cancel))
                except Cancelled:
                    raise
                except Exception:
                    out.append(texts[i])
                    failed.append(i)
        for i, o in zip(chunk, out):
            result[i] = (o or texts[i]).strip()

    if chunks:
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = [ex.submit(work, ch) for ch in chunks]
            try:
                for f in as_completed(futs):
                    f.result()
                    done += 1
                    progress(done / len(chunks))
            except Cancelled:
                for f in futs:
                    f.cancel()
                raise
    if failed:
        log(f"تعذّرت ترجمة {len(failed)} سطر (تُركت كما هي)")
    return [Cue(c.start, c.end, result[i], c.orig) for i, c in enumerate(cues)]


# ------------------------------------------------------------------ writers

def render(cues, fmt="srt", bilingual=False) -> str:
    def body(c: Cue) -> str:
        if bilingual and c.orig and c.orig != c.text:
            return f"{c.orig}\n{c.text}"
        return c.text

    if fmt == "txt":
        return "\n".join(body(c).replace("\n", " | ") for c in cues) + "\n"
    if fmt == "vtt":
        blocks = [
            f"{format_time(c.start, False)} --> {format_time(c.end, False)}\n{body(c)}"
            for c in cues
        ]
        return "WEBVTT\n\n" + "\n\n".join(blocks) + "\n"
    blocks = [
        f"{i}\n{format_time(c.start)} --> {format_time(c.end)}\n{body(c)}"
        for i, c in enumerate(cues, 1)
    ]
    return "\n\n".join(blocks) + "\n"


# ------------------------------------------------------------------ yt-dlp

def get_title(video_id: str) -> str:
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "noplaylist": True, "socket_timeout": 15}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            return info.get("title") or video_id
    except Exception:
        return video_id


def download_media(video_id: str, out_dir: Path, base: str, s: Settings,
                   progress: Progress = _noop, cancel=None, log: Log = _noop) -> str:
    import yt_dlp

    have_ffmpeg = shutil.which("ffmpeg") is not None
    q = int(s.quality)
    if s.audio_only:
        fmt = "bestaudio[ext=m4a]/bestaudio/best"
    elif have_ffmpeg:
        # ffmpeg available (e.g. Termux: pkg install ffmpeg): merge best video+audio
        fmt = f"bestvideo[height<={q}]+bestaudio/best[height<={q}]/best"
    else:
        # progressive mp4 only: no ffmpeg needed (APK build)
        fmt = f"best[height<={q}][ext=mp4]/best[height<={q}]/best"

    def hook(d):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress(d.get("downloaded_bytes", 0) / total)
        elif d.get("status") == "finished":
            progress(1.0)

    opts = {
        "format": fmt,
        "outtmpl": str(out_dir / (base.replace("%", "%%") + ".%(ext)s")),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": int(s.retries),
        "fragment_retries": int(s.retries),
        "socket_timeout": 20,
        "continuedl": True,
        "restrictfilenames": False,
        "progress_hooks": [hook],
    }
    if have_ffmpeg and not s.audio_only:
        opts["merge_output_format"] = "mp4"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            rd = info.get("requested_downloads") or []
            if rd and rd[0].get("filepath"):
                return rd[0]["filepath"]
            return ydl.prepare_filename(info)
    except Exception:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        raise


def list_playlist(url_or_id: str):
    import yt_dlp
    pid = get_playlist_id(url_or_id) or url_or_id.strip()
    if not PLAYLIST_ID_RE.match(pid):
        raise ValueError("رابط قائمة التشغيل غير صحيح")
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
            "skip_download": True, "socket_timeout": 20}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/playlist?list={pid}", download=False)
    items = [(e["id"], e.get("title") or e["id"])
             for e in (info.get("entries") or []) if e and e.get("id")]
    return info.get("title") or "playlist", items


# ------------------------------------------------------------------ pipelines

def process_video(source: str, s: Settings, out_dir, log: Log = _noop,
                  progress: Progress = _noop, cancel=None, prefix: str = "") -> dict:
    vid = get_video_id(source)
    if not vid:
        raise ValueError("رابط الفيديو غير صحيح")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    title = get_title(vid)
    base = f"{prefix}{sanitize(title)} [{vid}]"
    log(f"الفيديو: {title}")
    result = {"id": vid, "title": title, "files": [], "errors": []}

    steps = int(s.get_subs) + int(s.download_video)
    done = 0

    def sub_progress(p):
        progress((done + p) / max(1, steps))

    if s.get_subs:
        try:
            log("جلب الترجمة...")
            cues, lang, _gen = fetch_cues(vid, s.src_langs, s.retries, s.strip_noise, cancel, log)
            fmt = s.sub_format
            p = out / f"{base}.{lang}.{fmt}"
            atomic_write(p, render(cues, fmt), "utf-8-sig")
            result["files"].append(str(p))
            if s.translate and lang.split("-")[0] != s.target_lang:
                log(f"ترجمة إلى {s.target_lang}...")
                tr = translate_cues(cues, s.target_lang, s.workers, s.retries,
                                    lambda x: sub_progress(x), cancel, log)
                tag = f"{s.target_lang}.bilingual" if s.bilingual else s.target_lang
                p2 = out / f"{base}.{tag}.{fmt}"
                atomic_write(p2, render(tr, fmt, s.bilingual), "utf-8-sig")
                result["files"].append(str(p2))
            elif s.translate:
                log("الترجمة بلغتك بالفعل")
        except Cancelled:
            raise
        except Exception as e:
            result["errors"].append(f"الترجمة: {e}")
            log(f"خطأ في الترجمة: {e}")
        done += 1
        progress(done / max(1, steps))

    if s.download_video:
        try:
            log("تنزيل الصوت..." if s.audio_only else f"تنزيل الفيديو ({s.quality}p)...")
            f = download_media(vid, out, base, s, sub_progress, cancel, log)
            result["files"].append(f)
        except Cancelled:
            raise
        except Exception as e:
            result["errors"].append(f"التنزيل: {e}")
            log(f"خطأ في التنزيل: {e}")
        done += 1
        progress(done / max(1, steps))

    return result


def run_playlist(url: str, s: Settings, out_dir, start: int = 1, end: Optional[int] = None,
                 log: Log = _noop, progress: Progress = _noop, cancel=None) -> dict:
    title, items = list_playlist(url)
    start = max(1, start)
    selected = items[start - 1:end]
    log(f"القائمة: {title} ({len(items)} فيديو، سيتم {len(selected)})")
    folder = Path(out_dir) / sanitize(title)
    result = {"title": title, "files": [], "errors": []}
    total = max(1, len(selected))
    for k, (vid, t) in enumerate(selected):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        n = start + k
        log(f"[{k + 1}/{len(selected)}] {t}")
        try:
            r = process_video(vid, s, folder, log,
                              lambda p, k=k: progress((k + p) / total), cancel, f"{n:03d} - ")
            result["files"] += r["files"]
            result["errors"] += r["errors"]
        except Cancelled:
            raise
        except Exception as e:      # one bad video must not stop the playlist
            result["errors"].append(f"{t}: {e}")
            log(f"تخطي: {e}")
        progress((k + 1) / total)
    return result


# ------------------------------------------------------------------ history

def add_history(path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_history(path, limit: int = 50) -> list:
    try:
        lines = Path(path).read_text("utf-8").splitlines()
    except Exception:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out[::-1]


def clear_history(path) -> None:
    try:
        Path(path).unlink()
    except Exception:
        pass
