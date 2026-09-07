"""RSS 1.0 (RDF) / RSS 2.0 / Atom の取得・解析（FR-1）。

いずれの形式も同じコードパスで扱う（FR-1.4）。取得方式の追加は
``FETCHERS`` に関数を1つ登録するだけでよい（FR-1.7）。

標準ライブラリの urllib / xml.etree / email.utils / hashlib のみを使用する（D-5）。
"""

from __future__ import annotations

import hashlib
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from .config import Source

logger = logging.getLogger(__name__)


@dataclass
class RawItem:
    """フィードから取得した、翻訳・要約前の生の記事。"""

    key: str  # URL の SHA-1 先頭16桁（FR-1.6）
    channel: str
    source_id: str
    source_label: str
    language: str
    title: str
    summary: str
    link: str
    published: datetime | None


def _localname(tag: str) -> str:
    """名前空間プレフィックスを取り除いたタグ名を返す。

    RSS 1.0 / RSS 2.0 / Atom で名前空間が異なるため、要素名の一致判定は
    常にこのローカル名で行う（FR-1.4）。
    """
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _text(elem: ET.Element | None) -> str:
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


def _find_child(parent: ET.Element, name: str) -> ET.Element | None:
    for child in parent:
        if _localname(child.tag) == name:
            return child
    return None


def _find_all_children(parent: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in parent if _localname(child.tag) == name]


def _parse_date(text: str) -> datetime | None:
    text = text.strip()
    if not text:
        return None

    dt: datetime | None = None
    # ISO 8601（RSS 1.0 の dc:date、Atom の updated/published。例 2026-06-02T00:00:00Z）
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # RFC 2822（RSS 2.0 の pubDate）
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None

    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _make_key(link: str) -> str:
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]


def _matches_keywords(source: Source, title: str, summary: str, link: str) -> bool:
    """タイトル・公式サマリー・URL のいずれかにキーワードを含むか（大文字小文字非区別 OR）。"""
    if not source.include:
        return True
    haystack = f"{title} {summary} {link}".lower()
    return any(keyword.lower() in haystack for keyword in source.include)


def _extract_rss1(root: ET.Element) -> list[tuple[str, str, str, datetime | None]]:
    """RSS 1.0 (RDF)。item は http://purl.org/rss/1.0/ 名前空間、日付は dc:date。"""
    results = []
    for item in _find_all_children(root, "item"):
        title = _text(_find_child(item, "title"))
        link = _text(_find_child(item, "link"))
        summary = _text(_find_child(item, "description"))
        published = _parse_date(_text(_find_child(item, "date")))
        results.append((title, link, summary, published))
    return results


def _extract_rss2(root: ET.Element) -> list[tuple[str, str, str, datetime | None]]:
    channel = _find_child(root, "channel")
    if channel is None:
        return []
    results = []
    for item in _find_all_children(channel, "item"):
        title = _text(_find_child(item, "title"))
        link = _text(_find_child(item, "link"))
        summary = _text(_find_child(item, "description"))
        published = _parse_date(_text(_find_child(item, "pubDate")))
        results.append((title, link, summary, published))
    return results


def _extract_atom(root: ET.Element) -> list[tuple[str, str, str, datetime | None]]:
    results = []
    for entry in _find_all_children(root, "entry"):
        title = _text(_find_child(entry, "title"))
        link_elem = _find_child(entry, "link")
        link = link_elem.get("href", "") if link_elem is not None else ""
        summary_elem = _find_child(entry, "summary") or _find_child(entry, "content")
        summary = _text(summary_elem)
        date_elem = _find_child(entry, "updated") or _find_child(entry, "published")
        published = _parse_date(_text(date_elem)) if date_elem is not None else None
        results.append((title, link, summary, published))
    return results


def _parse_feed(content: bytes, source: Source) -> list[RawItem]:
    root = ET.fromstring(content)
    root_name = _localname(root.tag)

    if root_name == "RDF":
        raw = _extract_rss1(root)
    elif root_name == "rss":
        raw = _extract_rss2(root)
    elif root_name == "feed":
        raw = _extract_atom(root)
    else:
        logger.warning("source %s: unrecognized feed root <%s>", source.id, root.tag)
        raw = []

    items: list[RawItem] = []
    for title, link, summary, published in raw:
        if not link:
            continue
        if not _matches_keywords(source, title, summary, link):
            continue
        items.append(
            RawItem(
                key=_make_key(link),
                channel=source.channel,
                source_id=source.id,
                source_label=source.label,
                language=source.language,
                title=title,
                summary=summary,
                link=link,
                published=published,
            )
        )
    return items


def fetch_rss(source: Source) -> list[RawItem]:
    """RSS 1.0 / RSS 2.0 / Atom のいずれかを取得・解析する。"""
    request = urllib.request.Request(source.url, headers={"User-Agent": "bcbs-watch/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()
    return _parse_feed(content, source)


# FR-1.7: 取得方式（kind）ごとの実装をここに登録するだけで追加できる。
# main 側や config 側のコードを変更する必要はない。
FETCHERS = {
    "rss": fetch_rss,
}


def fetch_source(source: Source) -> list[RawItem]:
    """1つの情報源を取得する。失敗しても処理全体を止めない（FR-1.5）。"""
    fetcher = FETCHERS.get(source.kind)
    if fetcher is None:
        logger.warning("source %s: unknown kind %r, skipping", source.id, source.kind)
        return []
    try:
        return fetcher(source)
    except (urllib.error.URLError, ET.ParseError, OSError, ValueError) as exc:
        logger.warning("source %s: fetch failed (%s), skipping", source.id, exc)
        return []
