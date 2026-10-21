"""namevids.me + oakroot upload."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import random
import re
import string
import struct
import tempfile
import time
import uuid
from typing import Any

import httpx

from telegram_links import normalize_telegram_url, telegram_url

SITE = "https://namevids.me"
UPLOAD_API = "https://add4.oakroot.top/api/upload.php"
SHARE_API = "https://add4.oakroot.top/api/share.php"
DRAFT_FID_RE = re.compile(r'data-id="(\d+)"', re.I)
PROFILE_POST_RE = re.compile(
    r'id="(\d+)"[^>]*data-post="([^"]+)"[^>]*data-raw="\[([^\]]+)\]"',
    re.I | re.S,
)
REDACT_CONTENT_API = f"{SITE}/api/redact_content.php"
DEFAULT_WEBAPP_URL = "https://peach-web-5t2p.onrender.com"
DEFAULT_NAMEVIDS_CAPTION_LINK = telegram_url("jesovixxx")
CAPTION_MAX_LEN = 200
TITLE_MAX_LEN = 50

_CYRILLIC_TO_LATIN = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}
_TITLE_ALLOWED_RE = re.compile(r"[^a-zA-Z0-9 \-_'.,!?]")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_CHROME_UA_PROFILES = (
    DEFAULT_USER_AGENT,
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
)


def parse_account_metadata(meta: Any) -> dict[str, Any]:
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str) and meta.strip():
        return json.loads(meta)
    return {}


def _random5(rng: random.Random) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(rng.choice(alphabet) for _ in range(5))


def _fa_entry(rng: random.Random, value: str, elapsed_ms: int) -> str:
    payload = base64.b64encode(f"{value}|{elapsed_ms}".encode()).decode()
    return f"{_random5(rng)}|{payload}"


def generate_fa(*, seed: str | None = None) -> str:
    """Anti-bot `fa` JSON for namevids login. Same seed → same payload (per account)."""
    rng = random.Random(seed) if seed else random
    elapsed = rng.randint(100, 5000)
    return json.dumps(
        {
            "s": _fa_entry(rng, str(rng.randint(0, 500)), elapsed),
            "t": _fa_entry(rng, f"{rng.randint(0, 1000)},{rng.randint(0, 1000)}", elapsed),
            "c": _fa_entry(rng, f"{rng.randint(0, 1000)},{rng.randint(0, 1000)}", elapsed),
            "m": _fa_entry(rng, f"{rng.randint(0, 1000)},{rng.randint(0, 1000)}", elapsed),
        },
        separators=(",", ":"),
    )


def resolve_user_agent(metadata: dict[str, Any], *, login_name: str = "") -> str:
    explicit = (metadata.get("user_agent") or os.environ.get("NAMEVIDS_USER_AGENT") or "").strip()
    if explicit:
        return explicit
    if login_name:
        idx = int(hashlib.sha256(login_name.encode()).hexdigest(), 16) % len(_CHROME_UA_PROFILES)
        return _CHROME_UA_PROFILES[idx]
    return DEFAULT_USER_AGENT


def resolve_fa(metadata: dict[str, Any], *, login_name: str = "") -> str:
    explicit = (metadata.get("fa") or os.environ.get("NAMEVIDS_FA") or "").strip()
    if explicit:
        return explicit
    seed = (metadata.get("fa_seed") or login_name or "").strip() or None
    return generate_fa(seed=seed)


def login(
    login_name: str,
    password: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> httpx.Client:
    meta = metadata or {}
    user_agent = resolve_user_agent(meta, login_name=login_name)
    headers = {"User-Agent": user_agent}
    accept_language = str(meta.get("accept_language") or "").strip()
    if accept_language:
        headers["Accept-Language"] = accept_language
    client = httpx.Client(
        headers=headers,
        timeout=60.0,
        follow_redirects=True,
    )
    client.get(f"{SITE}/login")
    res = client.post(
        f"{SITE}/api/login.php",
        data={
            "login": login_name,
            "password": password,
            "type": "authorization",
            "fa": resolve_fa(meta, login_name=login_name),
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": SITE,
            "Referer": f"{SITE}/login",
        },
    )
    res.raise_for_status()
    payload = res.json()
    if payload.get("type") != "auth-successful":
        raise RuntimeError(f"namevids auth failed: {payload}")
    return client


def fetch_api_key(client: httpx.Client) -> str:
    res = client.get(f"{SITE}/share", headers={"Referer": f"{SITE}/share"})
    res.raise_for_status()
    match = re.search(r"apiKey:\s*'([^']+)'", res.text)
    if not match:
        raise RuntimeError("namevids apiKey not found")
    return match.group(1)


def _share_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": SITE,
        "Referer": f"{SITE}/share",
        "X-Requested-With": "XMLHttpRequest",
    }


def list_draft_fids(client: httpx.Client) -> list[str]:
    res = client.get(f"{SITE}/share", headers={"Referer": f"{SITE}/share"})
    res.raise_for_status()
    return list(dict.fromkeys(DRAFT_FID_RE.findall(res.text)))


def clear_pending_drafts(
    client: httpx.Client,
    api_key: str,
    *,
    pause_sec: float = 0.35,
) -> int:
    """Delete orphan uploads from oakroot temp table (fixes 'Up to 20 files per post')."""
    fids = list_draft_fids(client)
    if not fids:
        return 0
    deleted = 0
    for fid in fids:
        res = client.post(
            SHARE_API,
            data={"delete": "true", "fid": fid, "key": api_key},
            headers=_share_headers(),
            timeout=60.0,
        )
        if res.text.strip() == "1":
            deleted += 1
        time.sleep(pause_sec)
    print(f"namevids: cleared {deleted}/{len(fids)} pending drafts")
    return deleted


def delete_draft_fid(client: httpx.Client, api_key: str, file_id: str) -> None:
    """Remove an unpublished oakroot upload (share page draft). Not for live profile posts."""
    if not file_id:
        return
    res = client.post(
        SHARE_API,
        data={"delete": "true", "fid": file_id, "key": api_key},
        headers=_share_headers(),
        timeout=60.0,
    )
    if res.text.strip() == "1":
        print(f"namevids: deleted orphan draft fid={file_id}")


def profile_url(login_name: str) -> str:
    handle = login_name.strip().lstrip("@")
    return f"{SITE}/@{handle}"


def list_profile_posts(client: httpx.Client, login_name: str) -> list[dict[str, str]]:
    """Published posts on /@login — post id differs from oakroot upload fid in our DB."""
    res = client.get(profile_url(login_name), headers={"Referer": f"{SITE}/share"})
    res.raise_for_status()
    posts: list[dict[str, str]] = []
    for match in PROFILE_POST_RE.finditer(res.text):
        post_id, slug, raw = match.group(1), match.group(2), match.group(3)
        raw_decoded = html.unescape(raw)
        quoted = re.findall(r'"([^"]*)"', raw_decoded)
        title = quoted[-1] if quoted else ""
        posts.append({"post_id": post_id, "slug": slug, "title": title})
    return posts


def find_profile_post(
    client: httpx.Client,
    login_name: str,
    *,
    title_contains: str = "",
    post_id: str = "",
) -> dict[str, str] | None:
    needle = title_contains.strip().lower()
    for post in list_profile_posts(client, login_name):
        if post_id and post["post_id"] == post_id:
            return post
        if needle and needle in post["title"].lower():
            return post
    return None


def delete_published_post(
    client: httpx.Client,
    post_id: str,
    *,
    referer: str | None = None,
) -> dict[str, Any]:
    """Delete a live post from the account profile (footer.min.js → redact_content.php)."""
    if not post_id:
        raise ValueError("post_id required")
    page = referer or f"{SITE}/"
    res = client.post(
        REDACT_CONTENT_API,
        data=f"delete=true&fileid={post_id}",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": SITE,
            "Referer": page,
        },
        timeout=60.0,
    )
    res.raise_for_status()
    if not res.text.strip():
        raise RuntimeError(f"namevids delete empty response for post_id={post_id}")
    data = res.json()
    if not isinstance(data, dict) or not data.get("type"):
        raise RuntimeError(f"namevids delete failed for post_id={post_id}: {data}")
    print(f"namevids: deleted published post_id={post_id}")
    return data


def delete_published_post_by_title(
    client: httpx.Client,
    login_name: str,
    title_contains: str,
) -> dict[str, Any]:
    post = find_profile_post(client, login_name, title_contains=title_contains)
    if not post:
        raise RuntimeError(f"namevids post not found on profile: {title_contains!r}")
    referer = f"{SITE}/{post['slug']}"
    return delete_published_post(client, post["post_id"], referer=referer)


def prepare_namevids_session(
    login_name: str,
    password: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> tuple[httpx.Client, str]:
    """Login, fetch apiKey, clear stuck drafts before a batch upload run."""
    client = login(login_name, password, metadata=metadata)
    api_key = fetch_api_key(client)
    clear_pending_drafts(client, api_key)
    return client, api_key


def flush_pending_drafts(client: httpx.Client, api_key: str) -> bool:
    """Legacy: publish temp-table batch. Prefer clear_pending_drafts — share hits rate limits."""
    cleared = clear_pending_drafts(client, api_key)
    if cleared:
        return True
    res = client.post(
        SHARE_API,
        data={"share": "true", "key": api_key},
        headers=_share_headers(),
        timeout=120.0,
    )
    res.raise_for_status()
    data = res.json() if res.text.strip() else {}
    ok = bool(data.get("success"))
    err = str(data.get("error") or "")
    if not ok and "too fast" in err.lower():
        print("namevids: share rate-limited, skip flush")
    else:
        print(f"namevids: flush pending drafts -> success={ok}")
    return ok


def _uniqueize_mp4_copy(file_path: str) -> str:
    """Copy mp4 with a unique trailing free atom so oakroot treats re-upload as new."""
    payload = uuid.uuid4().bytes + uuid.uuid4().bytes
    atom_size = 8 + len(payload)
    atom = struct.pack(">I", atom_size) + b"free" + payload
    fd, out_path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    with open(file_path, "rb") as src, open(out_path, "wb") as dst:
        dst.write(src.read())
        dst.write(atom)
    return out_path


def _post_upload(
    client: httpx.Client,
    api_key: str,
    file_path: str,
    filename: str,
) -> dict[str, Any]:
    with open(file_path, "rb") as handle:
        res = client.post(
            UPLOAD_API,
            data={"key": api_key},
            files={"file": (filename, handle, "video/mp4")},
            headers={"Origin": SITE, "Referer": f"{SITE}/share"},
            timeout=600.0,
        )
    if not res.text.strip():
        raise RuntimeError("namevids upload empty response (200)")
    data = res.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"namevids upload invalid response: {data!r}")
    return data


def _resolve_upload_id(
    client: httpx.Client,
    api_key: str,
    file_path: str,
    filename: str,
    *,
    allow_draft_flush: bool = True,
) -> str:
    data = _post_upload(client, api_key, file_path, filename)
    if data.get("success") and data.get("id"):
        return str(data["id"])

    err = str(data.get("error") or "")
    if allow_draft_flush and "20 files" in err.lower():
        clear_pending_drafts(client, api_key)
        data = _post_upload(client, api_key, file_path, filename)
        if data.get("success") and data.get("id"):
            return str(data["id"])

    if data.get("fileExistId"):
        exist_id = str(data["fileExistId"])
        print(
            f"namevids: duplicate clip (fileExistId={exist_id}) — "
            "re-upload with unique metadata for temp table"
        )
        unique_path = _uniqueize_mp4_copy(file_path)
        try:
            unique_name = f"{uuid.uuid4().hex[:12]}.mp4"
            retry = _post_upload(client, api_key, unique_path, unique_name)
            if retry.get("success") and retry.get("id"):
                return str(retry["id"])
            retry_err = str(retry.get("error") or "")
            if allow_draft_flush and "20 files" in retry_err.lower():
                clear_pending_drafts(client, api_key)
                retry = _post_upload(client, api_key, unique_path, unique_name)
                if retry.get("success") and retry.get("id"):
                    return str(retry["id"])
            if retry.get("fileExistId"):
                raise RuntimeError(
                    "namevids upload duplicate after unique metadata — "
                    f"fileExistId={retry['fileExistId']}"
                )
            raise RuntimeError(f"namevids upload failed after duplicate retry: {retry}")
        finally:
            try:
                os.unlink(unique_path)
            except OSError:
                pass

    raise RuntimeError(f"namevids upload failed: {data}")


def upload_stream(client: httpx.Client, api_key: str, file_path: str, filename: str) -> str:
    return _resolve_upload_id(client, api_key, file_path, filename)


def _transliterate_cyrillic(text: str) -> str:
    out: list[str] = []
    for ch in text:
        mapped = _CYRILLIC_TO_LATIN.get(ch) or _CYRILLIC_TO_LATIN.get(ch.lower())
        if mapped is not None:
            out.append(mapped)
        else:
            out.append(ch)
    return "".join(out)


_AZERI_TO_LATIN = {
    "ə": "e",
    "Ə": "E",
    "ş": "s",
    "Ş": "S",
    "ö": "o",
    "Ö": "O",
    "ü": "u",
    "Ü": "U",
    "ğ": "g",
    "Ğ": "G",
    "ı": "i",
    "İ": "I",
    "ç": "c",
    "Ç": "C",
}


def _fold_azeri(text: str) -> str:
    return "".join(_AZERI_TO_LATIN.get(ch, ch) for ch in text)


def _latinize_title_text(text: str) -> str:
    folded = _fold_azeri(_transliterate_cyrillic(text))
    folded = _TITLE_ALLOWED_RE.sub(" ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def sanitize_namevids_title(
    *,
    title_ru: str = "",
    title_en: str = "",
    video_id: str = "",
) -> str:
    """namevids rejects Cyrillic titles — prefer English, else transliterate."""
    candidates: list[str] = []
    for raw in (title_en, title_ru):
        text = (raw or "").strip()
        if not text:
            continue
        if " - " in text:
            text = text.split(" - ", 1)[0].strip()
        latin = _latinize_title_text(text)
        if latin and latin not in candidates:
            candidates.append(latin)

    vid = str(video_id or "").strip()
    if vid and vid not in candidates:
        candidates.append(vid)

    for candidate in candidates:
        if candidate:
            return candidate[:TITLE_MAX_LEN]
    return "video"


def _title_publish_candidates(primary: str, *, video_id: str = "") -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in (primary, video_id):
        title = (raw or "").strip()[:TITLE_MAX_LEN]
        if title and title not in seen:
            seen.add(title)
            out.append(title)
    return out or ["video"]


def publish(
    client: httpx.Client,
    api_key: str,
    file_id: str,
    title: str,
    caption: str,
    *,
    fallback_caption: str | None = None,
    fallback_title: str | None = None,
) -> None:
    """Set title/caption and share. Retries share when oakroot temp table is not ready."""
    titles = _title_publish_candidates(title, video_id=fallback_title or "")
    captions = [caption[:CAPTION_MAX_LEN]]
    if fallback_caption:
        fallback = fallback_caption[:CAPTION_MAX_LEN]
        if fallback not in captions:
            captions.append(fallback)

    last_exc: RuntimeError | None = None
    for title_idx, publish_title in enumerate(titles):
        for cap_idx, cap in enumerate(captions):
            try:
                _publish_once(client, api_key, file_id, publish_title, cap)
                return
            except RuntimeError as exc:
                last_exc = exc
                err = str(exc).lower()
                if cap_idx + 1 < len(captions) and "description_invalid" in err:
                    print("namevids: description_invalid, retry minimal caption")
                    continue
                if title_idx + 1 < len(titles) and "title_invalid" in err:
                    print(f"namevids: title_invalid, retry title={titles[title_idx + 1]!r}")
                    break
                raise
    if last_exc:
        raise last_exc


def _publish_once(
    client: httpx.Client,
    api_key: str,
    file_id: str,
    title: str,
    caption: str,
) -> None:
    timeout = float(os.environ.get("NAMEVIDS_PUBLISH_TIMEOUT", "120"))
    initial_delay = float(os.environ.get("NAMEVIDS_PUBLISH_INITIAL_DELAY", "3"))
    retry_delays = [
        float(value)
        for value in os.environ.get("NAMEVIDS_PUBLISH_RETRY_DELAYS", "5,10,15").split(",")
        if value.strip()
    ] or [5.0, 10.0, 15.0]
    rate_limit_wait = float(os.environ.get("NAMEVIDS_RATE_LIMIT_WAIT", "45"))

    time.sleep(initial_delay)

    edit = client.post(
        SHARE_API,
        data={
            "title": title,
            "caption": caption,
            "fileId": file_id,
            "key": api_key,
        },
        headers=_share_headers(),
        timeout=timeout,
    )
    edit.raise_for_status()
    if edit.text.strip():
        edit_data = edit.json()
        if edit_data.get("success") is False:
            raise RuntimeError(f"namevids edit failed: {edit_data}")

    last_error: RuntimeError | None = None
    for attempt, delay in enumerate([0.0, *retry_delays]):
        if delay > 0:
            time.sleep(delay)
        pub = client.post(
            SHARE_API,
            data={"share": "true", "key": api_key},
            headers=_share_headers(),
            timeout=timeout,
        )
        pub.raise_for_status()
        data = pub.json()
        if data.get("success"):
            return
        err = str(data.get("error") or data)
        last_error = RuntimeError(f"namevids publish failed: {data}")
        err_lower = err.lower()
        if "too fast" in err_lower:
            if attempt < min(3, len(retry_delays) + 1):
                wait = rate_limit_wait * (attempt + 1)
                print(f"namevids: rate limited, wait {wait:.0f}s before retry {attempt + 1}")
                time.sleep(wait)
                continue
            raise last_error
        if "temporary table" not in err_lower:
            raise last_error
        if attempt < len(retry_delays):
            print(f"namevids: temp table not ready, retry {attempt + 1}/{len(retry_delays)}")

    if last_error:
        raise last_error


def build_traffic_link(settings: dict[str, str], video_id: str) -> str:
    """Cloudflare /go?id= → t.me bot → inline watch button."""
    go_base = (settings.get("traffic.go_base_url") or "").strip().rstrip("/")
    if not go_base:
        raise RuntimeError(
            "traffic.go_base_url is empty — set TRAFFIC_GO_BASE_URL in .env "
            "and run: python scripts/seed_traffic_go_url.py"
        )
    return f"{go_base}?id={video_id}"


def build_web_video_link(settings: dict[str, str], video_id: str) -> str:
    """Public SPA watch page (bot / WebApp — not namevids caption)."""
    import os

    base = (
        (settings.get("webapp.base_url") or "").strip()
        or os.environ.get("TELEGRAM_WEBAPP_URL", "").strip()
        or DEFAULT_WEBAPP_URL
    ).rstrip("/")
    return f"{base}/watch/{video_id}"


def build_namevids_caption_link(settings: dict[str, str], _video_id: str = "") -> str:
    """Telegram channel (or custom URL) in namevids caption — not per-video Render watch."""
    import os

    link = (
        (settings.get("namevids.caption_link") or "").strip()
        or os.environ.get("NAMEVIDS_CAPTION_LINK", "").strip()
        or DEFAULT_NAMEVIDS_CAPTION_LINK
    )
    return normalize_telegram_url(link.rstrip("/"))


def build_caption(_settings: dict[str, str], video: dict[str, Any], link: str) -> str:
    """namevids caption: full video header, site link, English description."""
    desc = (video.get("description_en") or video.get("description_ru") or "").strip()
    desc = re.sub(r"\s+", " ", desc)

    # namevids often renders the URL as a continuous “pill”/anchor.
    # Add a blank line after the URL so the description starts clearly separated.
    prefix = f"full video\n{link}\n\n"
    room = CAPTION_MAX_LEN - len(prefix)
    if room < 0:
        return prefix[:CAPTION_MAX_LEN]
    caption = prefix + desc[:room]
    return caption.rstrip()[:CAPTION_MAX_LEN]
