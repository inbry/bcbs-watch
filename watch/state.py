"""記事の永続状態（FR-3）。

`state/items.json` の読み書きをこのモジュールに閉じ込める（FR-3.6）。将来
SQLite などに差し替える場合も、他モジュールはこのモジュールが提供する関数
だけを使えばよく、影響を受けない。

**保持件数（max_items）と表示件数（display_limit）は別の設定である（FR-3.5）。**
このモジュールが受け取るのは max_items だけであり、display_limit は一切
知らない・受け取らない。表示件数の適用は出力側（段階D）の責務であり、両者を
混同すると、表示のために弾かれた記事なのか保持上限で state から落ちた記事
なのか区別がつかなくなる。特に後者は次回取得時に「新規＝first_seen 未設定」
と誤判定され、NEW が誤って復活する（FR-3.5 が防ごうとしている事故）。

標準ライブラリの json のみを使用する（D-5）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .fetch import RawItem

_MIN_DATETIME = datetime.min.replace(tzinfo=timezone.utc)

# enrich（段階C）が書き込むフィールドの初期値。新規記事はこの状態で作られ、
# enriched が True になるまで「未翻訳」として扱われる（§9）。
def _default_enrich_fields() -> dict:
    return {
        "enriched": False,  # True になったら FR-2.13 により再翻訳しない
        "title_ja": "",
        "summary_ja": "",
        "score": None,
        "doc_type": "",
        "tags": [],
        "deadline": "",
    }


def load_state(path: Path) -> dict[str, dict]:
    """`state/items.json` を読み込む。ファイルが無ければ空の状態を返す。

    状態はチャンネルをまたいで1ファイルにまとめ、各レコードの `channel`
    フィールドで区別する（FR-3.3）。
    """
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("items", {})


def save_state(path: Path, records: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": records}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)


def _raw_fields(item: RawItem) -> dict:
    return {
        "key": item.key,
        "channel": item.channel,
        "source_id": item.source_id,
        "source_label": item.source_label,
        "language": item.language,
        "title": item.title,
        "summary": item.summary,
        "link": item.link,
        "published": item.published.isoformat() if item.published else None,
    }


def merge_raw_items(
    records: dict[str, dict], raw_items: list[RawItem], now: datetime
) -> dict[str, dict]:
    """新規取得した記事を state にマージする。

    既存記事の `first_seen` と enrich 済みフィールド（段階C）は絶対に
    上書きしない（FR-3.2）。取得元から来る生データ（タイトル・要約・公開日
    など）だけを毎回最新の値で更新する。
    """
    now_iso = now.isoformat()
    for item in raw_items:
        existing = records.get(item.key)
        raw_fields = _raw_fields(item)
        if existing is None:
            records[item.key] = {
                **raw_fields,
                "first_seen": now_iso,
                **_default_enrich_fields(),
            }
        else:
            defaults = _default_enrich_fields()
            enrich_fields = {name: existing.get(name, default) for name, default in defaults.items()}
            records[item.key] = {
                **raw_fields,
                "first_seen": existing["first_seen"],
                **enrich_fields,
            }
    return records


def prune_channel(records: dict[str, dict], channel_id: str, max_items: int) -> dict[str, dict]:
    """1チャンネル分の保持件数上限を適用する（FR-3.4）。

    超過分は公開日の古いものから捨てる。他チャンネルの件数には影響しない
    （FR-3.3：チャンネルをまたいで1ファイルだが、刈り込みはチャンネルごと）。
    """
    channel_keys = [key for key, record in records.items() if record["channel"] == channel_id]
    if len(channel_keys) <= max_items:
        return records

    def _published_key(key: str) -> datetime:
        published = records[key]["published"]
        return datetime.fromisoformat(published) if published else _MIN_DATETIME

    channel_keys.sort(key=_published_key, reverse=True)
    for key in channel_keys[max_items:]:
        del records[key]
    return records


def items_for_channel(records: dict[str, dict], channel_id: str) -> list[dict]:
    return [record for record in records.values() if record["channel"] == channel_id]
