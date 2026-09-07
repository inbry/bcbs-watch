"""HTML / RSS 出力（FR-4）。

単一ファイル・CSS はインライン・外部リソース依存なし・JavaScript なしという
制約を満たす。NEW 判定（FR-4.10）はページ生成時刻を基準に、生成時に確定させる
（JavaScript にも localStorage にも依存しない。FR-4.11）。

標準ライブラリの html / datetime / email.utils / xml.etree のみを使用する
（D-5）。タイムゾーンは zoneinfo を使わず固定オフセットで扱う。Windows の
開発環境には IANA タイムゾーンDB（tzdata）が同梱されておらず、zoneinfo が
`ZoneInfoNotFoundError` を送出するため、pip install なしでは動かせない
（D-5, NFR-1）。Asia/Tokyo は夏時間がなく年間を通じて UTC+9 で確定するので、
固定オフセットで十分かつ確実に正しい。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape
from xml.etree import ElementTree as ET

from .config import Channel, Config

_MIN_DATETIME = datetime.min.replace(tzinfo=timezone.utc)

# site.timezone → 固定UTCオフセット。v1 では Asia/Tokyo のみサポートする（D-6）。
_FIXED_OFFSETS = {
    "Asia/Tokyo": timedelta(hours=9),
}


def resolve_timezone(name: str) -> timezone:
    offset = _FIXED_OFFSETS.get(name)
    if offset is None:
        offset = timedelta(0)  # 未知のタイムゾーンは UTC にフォールバックする
    return timezone(offset)


_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #fff; --fg: #1a1a1a; --muted: #6b6b6b; --border: #ddd;
  --badge-bg: #f0f0f0; --hero-bg: #fff4e5; --hero-border: #e0a640;
  --new-bg: #e5f0ff; --new-fg: #1a4d8f; --link: #1a4d8f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181c; --fg: #e8e8e8; --muted: #9a9a9a; --border: #3a3d42;
    --badge-bg: #262a30; --hero-bg: #3a2f14; --hero-border: #c98a2c;
    --new-bg: #16324f; --new-fg: #a9cdfb; --link: #7db2f8;
  }
}
* { box-sizing: border-box; }
body {
  max-width: 40rem; margin: 0 auto; padding: 1rem;
  font-family: system-ui, -apple-system, sans-serif;
  background: var(--bg); color: var(--fg);
}
a { color: var(--link); }
:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
nav.channels { font-size: 0.85rem; margin-bottom: 0.75rem; }
nav.channels a { margin-right: 0.75rem; }
h1 { font-size: 1.2rem; margin-bottom: 0.5rem; }
h2.month { font-size: 0.85rem; color: var(--muted); margin: 1.5rem 0 0.25rem; font-weight: 600; }
.hero {
  border: 1px solid var(--hero-border); background: var(--hero-bg);
  border-radius: 0.5rem; padding: 0.75rem 1rem; margin-bottom: 1rem;
}
.hero .days { font-size: 1.6rem; font-weight: 700; }
.hero .label { font-size: 0.8rem; color: var(--muted); }
.hero h2 { font-size: 1rem; margin: 0.25rem 0 0; }
article { padding: 0.75rem 0; border-bottom: 1px solid var(--border); }
h3 { font-size: 1rem; margin: 0 0 0.15rem; }
.meta { font-size: 0.8rem; color: var(--muted); }
.original { font-size: 0.8rem; color: var(--muted); }
.badge {
  display: inline-block; font-size: 0.75rem; border: 1px solid var(--border);
  background: var(--badge-bg); border-radius: 0.25rem; padding: 0 0.35rem; margin-right: 0.25rem;
}
.new-badge {
  display: inline-block; font-size: 0.7rem; font-weight: 600; color: var(--new-fg);
  background: var(--new-bg); border-radius: 0.25rem; padding: 0 0.3rem; margin-right: 0.35rem;
}
.folded { padding: 0.3rem 0; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
.folded a { color: var(--fg); }
.pending { color: var(--muted); font-style: italic; }
footer { margin-top: 2rem; font-size: 0.75rem; color: var(--muted); }
"""


def _parse_published(record: dict) -> datetime:
    published = record.get("published")
    return datetime.fromisoformat(published) if published else _MIN_DATETIME


def _parse_first_seen(record: dict) -> datetime:
    return datetime.fromisoformat(record["first_seen"])


def _is_new(record: dict, generated_at: datetime, new_hours: int) -> bool:
    # FR-4.10: 判定はページ生成時刻（generated_at）を基準に、生成時に確定させる。
    return (generated_at - _parse_first_seen(record)) <= timedelta(hours=new_hours)


def select_display_records(channel: Channel, records: list[dict], generated_at: datetime) -> list[dict]:
    """表示件数の上限（FR-4.8）と、NEW／高スコアの上限対象外扱い（FR-4.9）を適用する。"""
    ordered = sorted(records, key=_parse_published, reverse=True)
    top = ordered[: channel.display_limit]
    top_keys = {r["key"] for r in top}

    exempt = [
        r
        for r in ordered
        if r["key"] not in top_keys
        and (
            _is_new(r, generated_at, channel.display_new_hours)
            or (r.get("score") or 0) >= channel.display_always_score
        )
    ]

    combined = top + exempt
    combined.sort(key=_parse_published, reverse=True)
    return combined


def _find_hero(channel: Channel, records: list[dict], today: date) -> dict | None:
    """`hero = "deadline"` のとき、締切が最も近い未経過の案件を1件選ぶ（FR-4.6）。

    表示件数の上限にかかわらず、そのチャンネルの全記事から選ぶ。
    """
    if channel.hero != "deadline":
        return None

    candidates: list[tuple[date, dict]] = []
    for record in records:
        deadline = record.get("deadline")
        if not deadline:
            continue
        try:
            deadline_date = date.fromisoformat(deadline)
        except ValueError:
            continue
        if deadline_date >= today:  # 未経過のみ
            candidates.append((deadline_date, record))

    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def _display_heading(record: dict) -> str:
    # FR-2.3: language="ja" の記事は見出しを翻訳せず、原題の併記も出さない。
    return record["title"] if record["language"] == "ja" else record["title_ja"]


def _render_hero(channel: Channel, hero: dict, today: date) -> str:
    deadline_date = date.fromisoformat(hero["deadline"])
    remaining = (deadline_date - today).days
    heading = _display_heading(hero)
    return (
        '<div class="hero">'
        f'<div class="days">残り{remaining}日</div>'
        f'<div class="label">コメント提出期限 {escape(hero["deadline"])}</div>'
        f'<h2><a href="{escape(hero["link"])}">{escape(heading)}</a></h2>'
        "</div>"
    )


def _render_pending(record: dict, new_badge: str) -> str:
    # §9: API呼び出し失敗・enrich未処理の記事は原題とリンクのみを掲載する。
    # 英語の公式サマリーはここでは出さない（C-2）。
    # NEW は enrich 済みかどうかに関係なく first_seen だけで決まる（FR-4.10）ので、
    # 未翻訳の記事にも同様に付ける。
    published = record["published"][:10] if record["published"] else "?"
    return (
        "<article>"
        f'<h3>{new_badge}<a href="{escape(record["link"])}">{escape(record["title"])}</a></h3>'
        f'<div class="meta">{escape(record["source_label"])} / {escape(published)}'
        ' <span class="pending">(未翻訳)</span></div>'
        "</article>"
    )


def _render_folded(channel: Channel, record: dict, new_badge: str) -> str:
    # FR-4.5: スコア50未満の記事は1行に畳んで表示し、視線を奪わないこと。
    published = record["published"][:10] if record["published"] else "?"
    heading = _display_heading(record)
    return (
        '<div class="folded">'
        f"{new_badge}"
        f'<a href="{escape(record["link"])}">{escape(heading)}</a>'
        f' <span class="meta">{escape(record["source_label"])} / {escape(published)}</span>'
        "</div>"
    )


def _render_article(channel: Channel, record: dict, new_badge: str) -> str:
    heading = _display_heading(record)
    original = (
        f'<div class="original">{escape(record["title"])}</div>'
        if record["language"] == "en"
        else ""
    )
    doc_label = channel.doc_type_labels.get(record["doc_type"], record["doc_type"])
    published = record["published"][:10] if record["published"] else "?"
    tags = "".join(f'<span class="badge">{escape(t)}</span>' for t in record.get("tags", []))
    deadline_html = (
        f'<div class="meta">締切: {escape(record["deadline"])}</div>' if record.get("deadline") else ""
    )

    return (
        "<article>"
        f'<h3>{new_badge}<a href="{escape(record["link"])}">{escape(heading)}</a></h3>'
        f"{original}"
        f'<p>{escape(record["summary_ja"])}</p>'
        f'<div class="meta">{escape(record["source_label"])} / {escape(published)} '
        f'<span class="badge">{escape(doc_label)}</span> '
        f"スコア {record['score']} {tags}</div>"
        f"{deadline_html}"
        "</article>"
    )


def _month_label(dt: datetime) -> str:
    return f"{dt.year}年{dt.month}月"


def _render_list(channel: Channel, records: list[dict], generated_at: datetime, new_hours: int) -> str:
    rows = []
    last_month: str | None = None
    for record in records:
        published = _parse_published(record)
        if published is not _MIN_DATETIME:
            month = _month_label(published)
            if month != last_month:
                rows.append(f'<h2 class="month">{escape(month)}</h2>')
                last_month = month

        is_new = _is_new(record, generated_at, new_hours)
        # NEW は文字で示し、色だけで区別しない（§7）。hero より目立たせない見た目にする。
        # first_seen だけで決まり、enrich 済みかどうかには依存しない（FR-4.10）。
        new_badge = '<span class="new-badge">NEW</span>' if is_new else ""

        if not record.get("enriched"):
            rows.append(_render_pending(record, new_badge))
            continue

        if (record.get("score") or 0) < 50:
            rows.append(_render_folded(channel, record, new_badge))
        else:
            rows.append(_render_article(channel, record, new_badge))

    return "\n".join(rows) if rows else "<p>該当する記事はありません。</p>"


def _render_nav(channel: Channel, config: Config) -> str:
    # FR-4.7: チャンネルが2つ以上あるときのみ、ページ間のリンクを出す。
    channels = config.enabled_channels()
    if len(channels) < 2:
        return ""
    links = "".join(
        f'<a href="{escape(c.output)}">{escape(c.label)}</a>' for c in channels if c.id != channel.id
    )
    return f'<nav class="channels">{links}</nav>'


def render_channel_html(
    channel: Channel,
    records: list[dict],
    config: Config,
    generated_at: datetime,
) -> str:
    """1チャンネル分の記事一覧を単一ファイルの HTML にする。"""
    tz = resolve_timezone(config.site.timezone)
    today = generated_at.astimezone(tz).date()

    hero = _find_hero(channel, records, today)
    hero_html = _render_hero(channel, hero, today) if hero else ""

    display_records = select_display_records(channel, records, generated_at)
    list_html = _render_list(channel, display_records, generated_at, channel.display_new_hours)

    nav_html = _render_nav(channel, config)
    generated_local = generated_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")

    return (
        "<!doctype html>\n"
        '<html lang="ja">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="apple-mobile-web-app-capable" content="yes">\n'
        f"<title>{escape(channel.label)}</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{nav_html}\n"
        f"<h1>{escape(channel.label)}</h1>\n"
        f"{hero_html}\n"
        f"{list_html}\n"
        "<footer>"
        "要約は自動生成であり、判断は原文で確認すること。"
        f"最終更新: {escape(generated_local)}"
        "</footer>\n"
        "</body>\n"
        "</html>\n"
    )


def render_feed_xml(channel: Channel, records: list[dict], feed_limit: int) -> str:
    """RSS 2.0 フィードを生成する（FR-4.2）。件数は display_limit と独立（FR-4.12）。"""
    ordered = sorted(records, key=_parse_published, reverse=True)[:feed_limit]

    rss = ET.Element("rss", version="2.0")
    channel_el = ET.SubElement(rss, "channel")
    ET.SubElement(channel_el, "title").text = channel.label
    ET.SubElement(channel_el, "description").text = (
        "要約は自動生成であり、判断は原文で確認すること。"
    )

    for record in ordered:
        item_el = ET.SubElement(channel_el, "item")
        heading = _display_heading(record) if record.get("enriched") else record["title"]
        ET.SubElement(item_el, "title").text = heading
        ET.SubElement(item_el, "link").text = record["link"]
        ET.SubElement(item_el, "guid").text = record["link"]
        if record.get("enriched"):
            # C-2: 英語の公式サマリーは転載しない。日本語要約のみを載せる。
            ET.SubElement(item_el, "description").text = record["summary_ja"]
        published = _parse_published(record)
        if published is not _MIN_DATETIME:
            ET.SubElement(item_el, "pubDate").text = format_datetime(published)

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True).decode("utf-8")
