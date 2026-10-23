"""
Plus Chat 影片下載伺服器（個人用）

GET /list  → 選單文字（全部下載／影片1／照片2…）
GET /urls  → 選到的項目轉成下載網址
GET /item  → 單一項目（影片合併成 mp4）
  每個請求都要帶 key=<金鑰>&url=<Plus Chat 貼文連結>

畫質：優先用 DASH 最高位元率；失敗時退回 HLS。
只換容器、不重新編碼，速度快、畫質不損失。
"""
import calendar
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urljoin, urlparse

from flask import Flask, Response, abort, request

app = Flask(__name__)

ACCESS_KEY = os.environ.get("ACCESS_KEY", "")
PAGE_HOSTS = set(os.environ.get("PAGE_HOSTS", "artist.mnetplus.world").split(","))
MEDIA_PREFIX = os.environ.get("MEDIA_PREFIX", "https://media.static.bstage.in/")
IMAGE_PREFIX = os.environ.get("IMAGE_PREFIX", "https://image.static.bstage.in/")
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"}
NS = "{urn:mpeg:dash:schema:mpd:2011}"


# ---------- 共用 ----------
def get(url, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def fetch_all(urls):
    """平行下載多個片段，保持順序"""
    with ThreadPoolExecutor(max_workers=6) as ex:
        return list(ex.map(lambda u: get(u, binary=True), urls))


def write_concat(path, urls):
    with open(path, "wb") as f:
        for chunk in fetch_all(urls):
            f.write(chunk)


# ---------- 流量煞車 ----------
# 每次下載影片前查 Render 本月已用的對外流量，超過上限就拒絕，避免產生超額費用。
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "")      # Render 會自動提供
LIMIT_MB = float(os.environ.get("BANDWIDTH_LIMIT_MB", "4500"))  # 額度 5 GB，留 0.5 GB 緩衝
_UNIT_MB = {"b": 1 / 1e6, "bytes": 1 / 1e6, "kb": 1 / 1e3, "mb": 1, "gb": 1e3}
_usage_cache = {"t": 0.0, "mb": None}


def _sum_values(node, unit="mb"):
    """Render 回傳的 JSON 裡把所有 value 加總，並依 unit 換算成 MB"""
    total = 0.0
    if isinstance(node, list):
        for x in node:
            total += _sum_values(x, unit)
    elif isinstance(node, dict):
        # 依來源拆開的資料只算 total，避免重複計算
        src = (node.get("labels") or {}).get("trafficSource") if isinstance(node.get("labels"), dict) else None
        if src and src != "total":
            return 0.0
        unit = str(node.get("unit", unit)).lower()
        for k, v in node.items():
            if k == "value" and isinstance(v, (int, float)):
                total += v * _UNIT_MB.get(unit, 1)
            elif isinstance(v, (list, dict)):
                total += _sum_values(v, unit)
    return total


def month_usage_mb():
    """本月（UTC）已用流量，單位 MB；查不到時回傳 None。結果快取 5 分鐘。"""
    if not (RENDER_API_KEY and SERVICE_ID):
        _usage_cache["errors"] = ["缺少 RENDER_SERVICE_ID 環境變數" if RENDER_API_KEY else "沒有設定 RENDER_API_KEY"]
        return None
    now = time.time()
    if _usage_cache["mb"] is not None and now - _usage_cache["t"] < 300:
        return _usage_cache["mb"]
    t = time.gmtime(now)
    start = int(calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0)))
    iso = lambda s: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(s))
    # Render 文件對參數格式寫得不完整，依序嘗試幾種常見寫法，用第一個成功的
    attempts = [
        ("bandwidth", {"resource": SERVICE_ID, "startTime": start, "endTime": int(now)}),
        ("bandwidth", {"resource": SERVICE_ID, "startTime": iso(start), "endTime": iso(now)}),
        ("bandwidth", {"resource": SERVICE_ID, "startTime": start, "endTime": int(now),
                       "resolutionSeconds": 3600}),
        ("bandwidth-sources", {"resource": SERVICE_ID, "startTime": start, "endTime": int(now)}),
    ]
    errors = []
    for path, params in attempts:
        url = f"https://api.render.com/v1/metrics/{path}?" + urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {RENDER_API_KEY}",
                                                   "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read()
            mb = _sum_values(json.loads(body))
        except urllib.error.HTTPError as e:
            errors.append(f"{path} {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")
            if e.code in (401, 403):
                break        # 金鑰本身有問題，換格式也沒用
            continue
        except Exception as e:
            errors.append(f"{path}: {e!r}"[:250])
            continue
        _usage_cache.update(t=now, mb=mb, raw=body[:400].decode("utf-8", "replace"),
                            via=f"{path} {list(params)}", errors=[])
        return mb
    _usage_cache.update(errors=errors)
    return None


def bandwidth_ok():
    """沒設定 API 金鑰時不檢查；設定了但查詢失敗時，為了安全一律擋下"""
    if not RENDER_API_KEY:
        return True, None
    mb = month_usage_mb()
    if mb is None:
        return False, "查不到本月流量，為避免超額先暫停影片下載"
    if mb >= LIMIT_MB:
        return False, f"本月流量已用 {mb / 1000:.2f} GB，達到上限，影片下載下個月恢復（照片仍可下載）"
    return True, mb


def ffmpeg(*args):
    r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", *args],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-500:])


def _video_item(v):
    if isinstance(v, dict) and str(v.get("hlsPath", "")).startswith(MEDIA_PREFIX):
        dash = v.get("dashPath")
        return {"type": "video", "hls": v["hlsPath"],
                "dash": dash if str(dash or "").startswith(MEDIA_PREFIX) else None}
    return None


def parse_post(page_html):
    """回傳 (是否付費, [媒體...])；媒體依「影片在前、照片在後」排列"""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page_html, re.S)
    post = None
    if m:
        try:
            post = json.loads(m.group(1))["props"]["pageProps"].get("post")
        except (ValueError, KeyError, TypeError):
            post = None

    items = []
    if post:
        for v in [post.get("video")] + list(post.get("videos") or []):
            it = _video_item(v)
            if it and it not in items:
                items.append(it)
        for img in post.get("images") or []:
            url = img if isinstance(img, str) else (img or {}).get("url") or (img or {}).get("path")
            if url and url.startswith(IMAGE_PREFIX) and {"type": "photo", "url": url} not in items:
                items.append({"type": "photo", "url": url})
        return bool(post.get("paid")), items

    # 備援：網頁結構改變時，至少用正規表示式把影片找出來
    prefix = re.escape(MEDIA_PREFIX)
    for mm in re.finditer(r'"hlsPath":"(' + prefix + r'[^"]+)"(?:,"dashPath":"(' + prefix + r'[^"]+)")?',
                          page_html):
        it = {"type": "video", "hls": mm.group(1), "dash": mm.group(2)}
        if it not in items:
            items.append(it)
    return False, items


def label(item, n):
    return ("影片" if item["type"] == "video" else "照片") + str(n)


# ---------- DASH ----------
def _tmpl_attrs(*nodes):
    """SegmentTemplate 屬性可能分散在 AdaptationSet 與 Representation，合併起來"""
    attrs = {}
    for n in nodes:
        if n is not None:
            t = n.find(NS + "SegmentTemplate")
            if t is not None:
                attrs.update(t.attrib)
    return attrs


def _fill(tmpl, rep_id, number=None):
    s = tmpl.replace("$RepresentationID$", rep_id)
    if number is not None:
        s = re.sub(r"\$Number%0(\d+)d\$", lambda m: str(number).zfill(int(m.group(1))), s)
        s = s.replace("$Number$", str(number))
    return s


def _duration_sec(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", iso or "")
    if not m:
        raise ValueError("無法解析影片長度")
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def _track_urls(mpd_url, adapt, total_sec):
    reps = adapt.findall(NS + "Representation")
    rep = max(reps, key=lambda r: int(r.get("bandwidth", 0)))
    a = _tmpl_attrs(adapt, rep)
    if "media" not in a or "duration" not in a:
        raise ValueError("不支援的 DASH 格式")
    seg_sec = int(a["duration"]) / int(a.get("timescale", 1))
    count = math.ceil(total_sec / seg_sec - 1e-6)
    start = int(a.get("startNumber", 1))
    rid = rep.get("id", "")
    init = urljoin(mpd_url, _fill(a["initialization"], rid))
    segs = [urljoin(mpd_url, _fill(a["media"], rid, start + i)) for i in range(count)]
    return [init] + segs


def download_dash(mpd_url, out_mp4, work):
    root = ET.fromstring(get(mpd_url))
    total = _duration_sec(root.get("mediaPresentationDuration"))
    period = root.find(NS + "Period")
    tracks = {}
    for adapt in period.findall(NS + "AdaptationSet"):
        kind = (adapt.get("mimeType") or adapt.get("contentType") or "").split("/")[0]
        if kind in ("video", "audio") and kind not in tracks:
            tracks[kind] = _track_urls(mpd_url, adapt, total)
    if "video" not in tracks:
        raise ValueError("DASH 裡沒有影像軌")
    inputs = []
    for kind, urls in tracks.items():
        p = os.path.join(work, kind + ".mp4")
        write_concat(p, urls)
        inputs += ["-i", p]
    ffmpeg(*inputs, "-c", "copy", "-movflags", "+faststart", out_mp4)


# ---------- HLS（備援） ----------
def download_hls(master_url, out_mp4, work):
    master = get(master_url)
    best, best_bw, pending = None, -1, None
    for line in master.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            pending = int(m.group(1)) if m else 0
        elif line and not line.startswith("#") and pending is not None:
            if pending > best_bw:
                best, best_bw = urljoin(master_url, line), pending
            pending = None
    playlist_url = best or master_url
    playlist = get(playlist_url) if best else master
    if "#EXT-X-KEY" in playlist and "METHOD=NONE" not in playlist:
        raise ValueError("影片有加密，無法下載")
    segs = [urljoin(playlist_url, l.strip()) for l in playlist.splitlines()
            if l.strip() and not l.startswith("#")]
    ts = os.path.join(work, "all.ts")
    write_concat(ts, segs)
    ffmpeg("-i", ts, "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out_mp4)


# ---------- 路由 ----------
ALL_LABEL = "全部下載"


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(429)
@app.errorhandler(502)
def plain_error(e):
    return Response(f"錯誤：{e.description}", status=e.code, mimetype="text/plain; charset=utf-8")


@app.get("/")
def health():
    return "ok"


@app.get("/usage")
def usage():
    """查看本月流量（需要金鑰）"""
    if not ACCESS_KEY or request.args.get("key") != ACCESS_KEY:
        abort(403, "金鑰錯誤")
    if not RENDER_API_KEY:
        return text("尚未設定 RENDER_API_KEY，流量煞車沒有啟用")
    _usage_cache["mb"] = None
    mb = month_usage_mb()
    if mb is None:
        detail = "\n".join(_usage_cache.get("errors") or ["（沒有錯誤訊息）"])
        return text(f"錯誤：查詢 Render 流量失敗\n服務 ID：{SERVICE_ID or '（空白）'}\n\n{detail}")
    return text(f"本月已用 {mb:.1f} MB／上限 {LIMIT_MB:.0f} MB\n\n"
                f"查詢方式：{_usage_cache.get('via')}\n原始回應開頭：{_usage_cache.get('raw')}")


def load_post():
    """檢查金鑰與連結，回傳 (貼文連結, 媒體清單)"""
    if not ACCESS_KEY or request.args.get("key") != ACCESS_KEY:
        abort(403, "金鑰錯誤")
    # \u5f9e\u6574\u6bb5\u6587\u5b57\u88e1\u6293\u51fa\u7b2c\u4e00\u500b\u7db2\u5740\uff08\u8907\u88fd\u5230\u300c\u5206\u4eab\u6587\u5b57\uff0b\u9023\u7d50\u300d\u4e5f\u80fd\u7528\uff09
    raw = request.args.get("url") or ""
    found = re.search(r"https?://[^\s\"'\u201c\u201d<>]+", raw)
    link = found.group(0) if found else raw.strip()
    parsed = urlparse(link)
    if parsed.scheme not in ("https", "http") or parsed.hostname not in PAGE_HOSTS:
        abort(400, "請提供 Plus Chat 貼文連結")
    paid, items = parse_post(get(link))
    if paid and not items:
        abort(403, "這是付費會員貼文，只支援公開貼文")
    if not items:
        abort(404, "這則貼文裡沒有照片或影片")
    return link, items


def text(body, status=200):
    return Response(body, status=status, mimetype="text/plain; charset=utf-8")


@app.get("/list")
def list_items():
    """給捷徑的選單：每行一個選項；超過一個時第一行是「全部下載」"""
    _, items = load_post()
    has_video = any(it["type"] == "video" for it in items)
    ok, msg = bandwidth_ok() if has_video else (True, None)
    if not ok:
        photos = [(n, it) for n, it in enumerate(items, 1) if it["type"] == "photo"]
        if not photos:
            abort(429, msg)
        # 流量滿了：只列出照片（照片不經過伺服器、不吃流量）
        labels = [label(it, n) for n, it in photos]
    else:
        labels = [label(it, n) for n, it in enumerate(items, 1)]
    return text("\n".join(([ALL_LABEL] if len(labels) > 1 else []) + labels))


@app.get("/urls")
def urls():
    """把選到的選項換成實際下載網址（每行一個）。照片直接給原圖網址，不經過伺服器"""
    link, items = load_post()
    pick = request.args.get("pick", "")
    if not pick.strip() or ALL_LABEL in pick or "全部" in pick:
        idxs = list(range(1, len(items) + 1))
    else:
        idxs = sorted({int(n) for n in re.findall(r"\d+", pick) if 1 <= int(n) <= len(items)})
    if any(items[i - 1]["type"] == "video" for i in idxs) and not bandwidth_ok()[0]:
        idxs = [i for i in idxs if items[i - 1]["type"] == "photo"]
    if not idxs:
        abort(400, "沒有選到任何可下載的項目")
    base = request.host_url.rstrip("/")
    if request.headers.get("X-Forwarded-Proto") == "https":
        base = base.replace("http://", "https://", 1)
    out = []
    for i in idxs:
        it = items[i - 1]
        if it["type"] == "photo":
            out.append(it["url"])
        else:
            out.append(f"{base}/item?{urlencode({'key': ACCESS_KEY, 'url': link, 'i': i})}")
    return text("\n".join(out))


@app.get("/item")
def item():
    """下載單一項目：影片合併成 mp4；照片也能從這裡拿"""
    link, items = load_post()
    try:
        idx = int(request.args.get("i", 1))
    except ValueError:
        idx = 1
    if not 1 <= idx <= len(items):
        abort(404, f"這則貼文只有 {len(items)} 個項目")
    it = items[idx - 1]
    post_id = urlparse(link).path.rstrip("/").split("/")[-1][-8:]
    ok, msg = bandwidth_ok()
    if not ok:
        abort(429, msg)

    if it["type"] == "photo":
        data = get(it["url"], binary=True)
        return Response(data, mimetype="image/jpeg", headers={
            "Content-Disposition": f'attachment; filename="pluschat_{post_id}_{idx}.jpg"'})

    work = tempfile.mkdtemp()
    out = os.path.join(work, "out.mp4")
    try:
        try:
            if not it["dash"]:
                raise ValueError("no dash")
            download_dash(it["dash"], out, work)
        except Exception:
            download_hls(it["hls"], out, work)
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        abort(502, f"下載失敗：{e}")

    size = os.path.getsize(out)

    def stream():
        try:
            with open(out, "rb") as f:
                while chunk := f.read(1 << 16):
                    yield chunk
        finally:
            shutil.rmtree(work, ignore_errors=True)

    return Response(stream(), mimetype="video/mp4", headers={
        "Content-Length": str(size),
        "Content-Disposition": f'attachment; filename="pluschat_{post_id}_{idx}.mp4"',
    })
