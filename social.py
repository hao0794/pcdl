"""
社群平台解析：IG、Threads、X、TikTok、抖音

extract(link) 回傳媒體清單，格式跟 Plus Chat 的一樣，多了兩個欄位：
  {"type": "photo" | "video",
   "url": 直接下載網址（手機可以直接抓，不經過伺服器、不吃流量）,
   "server": None | "http" | "ytdl"   # 不是 None 的話要透過伺服器下載
   "src": 原始貼文網址（server="ytdl" 時用）,
   "headers": 伺服器下載時要帶的標頭}

登入用的 cookies：在 Render 的 Secret Files 放一個 cookies.txt（Netscape 格式），
路徑預設 /etc/secrets/cookies.txt，可用 COOKIES_FILE 環境變數改。
沒有 cookies 時 X、TikTok、抖音照樣能用；IG、Threads 多半需要。
"""
import glob
import http.cookiejar
import json
import os
import re
import shutil
import threading
import time
import urllib.request
from urllib.parse import urlparse

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")


class SocialError(Exception):
    """給使用者看的錯誤訊息"""
    def __init__(self, message, code=502):
        super().__init__(message)
        self.code = code


# ---------- 平台判斷 ----------
def _host_is(host, *domains):
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def platform_of(link):
    host = urlparse(link).hostname
    if _host_is(host, "instagram.com", "instagr.am"):
        return "instagram"
    if _host_is(host, "threads.net", "threads.com"):
        return "threads"
    if _host_is(host, "x.com", "twitter.com"):
        return "x"
    if _host_is(host, "tiktok.com"):
        return "tiktok"
    if _host_is(host, "douyin.com", "iesdouyin.com"):
        return "douyin"
    return None


# ---------- cookies ----------
# Render 的 Secret Files 是唯讀的，而 yt-dlp / gallery-dl 用完會想寫回 cookies，
# 所以先複製一份到可寫的位置再給它們用。
# 另外順便整理格式：貼到 Render 網頁時 Tab 可能變成空格、開頭多了空行，
# 這裡統一轉回標準的 Netscape 格式，避免讀不到。
_SECRET = os.environ.get("COOKIES_FILE", "/etc/secrets/cookies.txt")
_CANDIDATES = [_SECRET, "/etc/secrets/cookies.txt", "/app/cookies.txt", "/etc/secrets/cookies"]
COOKIES = None
COOKIE_STATUS = {"source": None, "lines": 0, "bad": 0}


def _normalize_cookies(raw):
    out, bad = ["# Netscape HTTP Cookie File"], 0
    for line in raw.lstrip("﻿").splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            parts = line.split(None, 6)
        if len(parts) == 6:        # 值是空的
            parts.append("")
        if len(parts) != 7:
            bad += 1
            continue
        out.append("\t".join(p.strip() for p in parts))
    return "\n".join(out) + "\n", len(out) - 1, bad


for _p in _CANDIDATES:
    if os.path.isfile(_p):
        with open(_p, encoding="utf-8", errors="replace") as _f:
            _text, _n, _bad = _normalize_cookies(_f.read())
        COOKIES = "/tmp/pcdl_cookies.txt"
        with open(COOKIES, "w", encoding="utf-8") as _f:
            _f.write(_text)
        COOKIE_STATUS.update(source=_p, lines=_n, bad=_bad)
        break


def cookie_report():
    """給 /cookies 檢查用：只列網域和名稱，不顯示內容"""
    if not COOKIES:
        found = [p for p in ("/etc/secrets",) if os.path.isdir(p)]
        listing = os.listdir("/etc/secrets") if found else []
        return ("找不到 cookies.txt\n"
                f"/etc/secrets 裡的檔案：{listing or '（沒有這個資料夾或是空的）'}\n"
                "請到 Render → Environment → Secret Files 新增，檔名要剛好是 cookies.txt")
    jar = _cookie_jar()
    by_domain = {}
    for c in jar:
        by_domain.setdefault(c.domain.lstrip("."), set()).add(c.name)
    lines = [f"讀到 {COOKIE_STATUS['source']}：{COOKIE_STATUS['lines']} 筆，"
             f"格式錯誤略過 {COOKIE_STATUS['bad']} 筆"]
    for d in ("instagram.com", "threads.com", "threads.net"):
        names = set().union(*[v for k, v in by_domain.items() if k.endswith(d)] or [set()])
        if not names:
            lines.append(f"{d}：沒有")
        else:
            login = "有 ✅" if "sessionid" in names else "沒有 ❌（可能匯出時沒登入）"
            lines.append(f"{d}：{len(names)} 個，登入用的 sessionid {login}")
    others = sorted(k for k in by_domain if not any(k.endswith(d) for d in
                                                    ("instagram.com", "threads.com", "threads.net")))
    if others:
        lines.append("其他網域：" + "、".join(others))
    return "\n".join(lines)


def _cookie_jar():
    jar = http.cookiejar.MozillaCookieJar()
    if COOKIES:
        try:
            jar.load(COOKIES, ignore_discard=True, ignore_expires=True)
        except Exception:
            pass
    return jar


def has_cookies(domain):
    return any(c.domain.lstrip(".").endswith(domain) for c in _cookie_jar())


def _opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookie_jar()))


def _fetch(url, ua=DESKTOP_UA, extra=None):
    """回傳 (最後網址, 內容文字)，會跟著轉址"""
    headers = {"User-Agent": ua, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"}
    headers.update(extra or {})
    with _opener().open(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


# ---------- gallery-dl（IG、X、TikTok） ----------
_gdl_lock = threading.Lock()
_gdl_ready = False


def _gdl_setup():
    global _gdl_ready
    from gallery_dl import config
    config.clear()
    if COOKIES:
        config.set(("extractor",), "cookies", COOKIES)
    config.set(("extractor",), "cookies-update", False)
    # 預設遇到限流會等很久（IG 每個請求間隔 6–12 秒、429 時每次等 60 秒重試 4 次），
    # 捷徑和瀏覽器會一直轉圈。改成快速失敗，直接回錯誤訊息。
    config.set(("extractor",), "retries", 1)
    config.set(("extractor",), "timeout", 20)
    config.set(("extractor",), "sleep-request", 0)
    config.set(("extractor",), "sleep-429", 3)
    config.set(("extractor", "instagram"), "sleep-request", 0.5)
    config.set(("extractor", "instagram"), "videos", "merged")  # 單一 mp4（含聲音），手機能直接抓
    config.set(("extractor", "tiktok"), "videos", "ytdl")       # TikTok 影片交給 yt-dlp
    config.set(("extractor", "tiktok"), "audio", False)
    config.set(("extractor", "twitter"), "videos", True)
    config.set(("extractor", "twitter"), "quoted", False)
    config.set(("extractor", "twitter"), "retweets", False)
    _gdl_ready = True


VIDEO_EXT = {"mp4", "mov", "webm", "m4v", "m3u8"}


def _gallery_dl(link):
    from gallery_dl import job
    with _gdl_lock:
        if not _gdl_ready:
            _gdl_setup()
    try:
        dj = job.DataJob(link, file=None, resolve=True)
    except Exception as e:   # 找不到對應的擷取器
        raise SocialError(f"不支援這種連結：{e}", 400)
    dj.run()
    items = []
    for msg in dj.data:
        if msg[0] == -1:
            raise SocialError(f"{msg[1].get('error')}: {msg[1].get('message')}")
        if msg[0] != 3:          # 只要 Message.Url
            continue
        url, kw = msg[1], msg[2]
        if url.startswith("ytdl:"):
            items.append({"type": "video", "url": None, "server": "ytdl", "src": url[5:]})
            continue
        ext = str(kw.get("extension", "")).lower()
        kind = str(kw.get("type", "")).lower()
        if ext in VIDEO_EXT or kind in ("video", "animated_gif"):
            items.append({"type": "video", "url": url, "server": None})
        elif ext in ("jpg", "jpeg", "png", "webp", "heic", "gif") or kind in ("photo", "image"):
            items.append({"type": "photo", "url": url, "server": None})
    return items


# ---------- yt-dlp（X 影片備援） ----------
def _ytdl_opts(**more):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "ffmpeg_location": FFMPEG, "socket_timeout": 30}
    if COOKIES:
        opts["cookiefile"] = COOKIES
    opts.update(more)
    return opts


def _ytdl_videos(link):
    """只抓影片：回傳每支影片最佳的單檔 mp4 直連"""
    import yt_dlp
    with yt_dlp.YoutubeDL(_ytdl_opts(noplaylist=False)) as ydl:
        info = ydl.extract_info(link, download=False)
    entries = info.get("entries") or [info]
    items = []
    for e in entries:
        if not e:
            continue
        fmts = [f for f in e.get("formats") or []
                if f.get("ext") == "mp4" and str(f.get("protocol", "")).startswith("http")
                and f.get("vcodec") != "none" and f.get("acodec") != "none"]
        if fmts:
            best = max(fmts, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
            items.append({"type": "video", "url": best["url"], "server": None})
        else:
            items.append({"type": "video", "url": None, "server": "ytdl", "src": e.get("webpage_url") or link})
    return items


# ---------- Threads ----------
def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _best_image(media):
    cands = ((media.get("image_versions2") or {}).get("candidates")) or []
    if not cands:
        return None
    return max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0)).get("url")


def _media_from_post(post):
    """Threads / IG 共用的貼文結構 → 媒體清單"""
    out = []
    for m in post.get("carousel_media") or [post]:
        vids = m.get("video_versions") or []
        if vids:
            out.append({"type": "video", "url": vids[0]["url"], "server": None})
        elif (img := _best_image(m)):
            out.append({"type": "photo", "url": img, "server": None})
    return out


def _threads(link):
    final, html = _fetch(link)
    m = re.search(r"/post/([\w-]+)", final) or re.search(r"/(?:t|p)/([\w-]+)", final)
    if not m:
        # 分享短網址（/share/xxx）不是用 HTTP 轉址，真正的網址藏在頁面裡
        real = None
        for pat in (r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"',
                    r'<meta[^>]+property="og:url"[^>]+content="([^"]+)"',
                    r'(https://www\.threads\.(?:com|net)/@[\w.]+/post/[\w-]+)',
                    r'(?:href|url|content)=["\'](?:https://www\.threads\.(?:com|net))?(/@[\w.]+/post/[\w-]+)'):
            mm = re.search(pat, html)
            if mm and "/post/" in mm.group(1):
                real = mm.group(1).replace("\\/", "/")
                break
        if real is None:
            # 有些頁面把網址寫在 JSON 裡（斜線被跳脫）
            mm = re.search(r'\\?/@([\w.]+)\\?/post\\?/([\w-]+)', html)
            if mm:
                real = f"/@{mm.group(1)}/post/{mm.group(2)}"
        if real:
            if real.startswith("/"):
                real = "https://www.threads.com" + real
            final, html = _fetch(real)
            m = re.search(r"/post/([\w-]+)", final) or re.search(r"/post/([\w-]+)", real)
    if not m:
        if "/share/" in link and ("login" in final or "/share/" in final):
            raise SocialError("Threads 分享短網址解不開，請改用貼文右上角 ⋯ → 複製連結", 400)
        raise SocialError("看不懂這個 Threads 連結，請用貼文的「複製連結」", 400)
    code = m.group(1)
    for blob in re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S):
        if code not in blob:
            continue
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        for d in _walk(data):
            if d.get("code") == code and any(k in d for k in ("image_versions2", "carousel_media", "video_versions")):
                items = _media_from_post(d)
                if items:
                    return items
                raise SocialError("這則 Threads 貼文沒有照片或影片", 404)
    if "login" in final or not has_cookies("threads.com") and not has_cookies("threads.net"):
        raise SocialError("Threads 要登入才看得到，請在伺服器放小號的 cookies", 403)
    raise SocialError("在 Threads 頁面裡找不到這則貼文（cookies 可能過期了）")


# ---------- 抖音 ----------
def _douyin_pick(url_list):
    url_list = url_list or []
    for u in url_list:          # 盡量挑 jpeg，iPhone 相簿相容性最好
        if ".jpeg" in u or ".jpg" in u:
            return u
    return url_list[0] if url_list else None


def _douyin(link):
    final, _ = _fetch(link, ua=IPHONE_UA)
    m = re.search(r"(?:/(?:video|note|slides)/|modal_id=|aweme_id=)(\d{8,})", final)
    if not m:
        raise SocialError("看不懂這個抖音連結，請用 App 的「分享 → 複製連結」", 400)
    aweme_id = m.group(1)
    page_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    _, html = _fetch(page_url, ua=IPHONE_UA)
    mm = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not mm:
        raise SocialError("抖音頁面格式改了，暫時抓不到")
    item = None
    for d in _walk(json.loads(mm.group(1))):
        if isinstance(d.get("item_list"), list) and d["item_list"]:
            item = d["item_list"][0]
            break
    if not item:
        raise SocialError("找不到這支抖音（可能已刪除或不公開）", 404)

    images = item.get("images") or []
    if images:
        out = []
        for img in images:
            vid = ((img.get("video") or {}).get("play_addr") or {}).get("uri")  # 動態圖文
            if vid:
                out.append(_douyin_video(vid))
            elif (u := _douyin_pick(img.get("url_list"))):
                out.append({"type": "photo", "url": u, "server": None})
        return out

    uri = ((item.get("video") or {}).get("play_addr") or {}).get("uri")
    if not uri:
        raise SocialError("這支抖音沒有可下載的影片", 404)
    return [_douyin_video(uri)]


def _douyin_video(uri):
    if uri.startswith("http"):
        url = uri.replace("playwm", "play")
    else:
        url = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0"
    return {"type": "video", "url": url, "server": "http",
            "headers": {"User-Agent": IPHONE_UA, "Referer": "https://www.douyin.com/"}}


# ---------- 對外介面 ----------
_cache = {}
CACHE_SEC = 600


def extract(link):
    """連結 → 媒體清單。結果快取 10 分鐘：捷徑會連續呼叫好幾次，避免重複打 IG 被限制"""
    hit = _cache.get(link)
    if hit and time.time() - hit[0] < CACHE_SEC:
        return hit[1]
    plat = platform_of(link)
    if plat == "threads":
        items = _threads(link)
    elif plat == "douyin":
        items = _douyin(link)
    elif plat in ("instagram", "x", "tiktok"):
        try:
            items = _gallery_dl(link)
        except SocialError as e:
            if plat == "x":            # X 有時候 gallery-dl 不登入抓不到，改用 yt-dlp（只有影片）
                try:
                    items = _ytdl_videos(link)
                except Exception:
                    raise e
            elif plat == "instagram" and not has_cookies("instagram.com"):
                raise SocialError("IG 要登入才抓得到，請在伺服器放小號的 cookies", 403)
            else:
                raise
    else:
        raise SocialError("不支援這個網站", 400)
    if not items:
        raise SocialError("這則貼文裡沒有照片或影片", 404)
    for k in [k for k, v in _cache.items() if time.time() - v[0] >= CACHE_SEC]:
        _cache.pop(k, None)
    _cache[link] = (time.time(), items)
    return items


def download(item, out_dir):
    """伺服器端下載單一項目，回傳檔案路徑"""
    if item.get("server") == "ytdl":
        import yt_dlp
        opts = _ytdl_opts(outtmpl=os.path.join(out_dir, "dl.%(ext)s"),
                          format="bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
                          merge_output_format="mp4")
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([item["src"]])
        files = [f for f in glob.glob(os.path.join(out_dir, "dl.*")) if not f.endswith(".part")]
        if not files:
            raise RuntimeError("yt-dlp 沒有產生檔案")
        return max(files, key=os.path.getsize)

    ext = "mp4" if item["type"] == "video" else "jpg"
    path = os.path.join(out_dir, "dl." + ext)
    headers = item.get("headers") or {"User-Agent": DESKTOP_UA}
    with _opener().open(urllib.request.Request(item["url"], headers=headers), timeout=60) as r, \
            open(path, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 16)
    return path
