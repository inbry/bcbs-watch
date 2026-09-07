"""CLI エントリポイント。

実行例:
  python -m watch.main --dry-run   # 収集と描画のみ。API は一切呼ばない（NFR-2）
  python -m watch.main --mock      # 模擬記事・模擬応答で enrich まで含めて通しで検証する
  python -m watch.main             # 通常実行

v1 は方針として翻訳・要約・スコア付け（enrich）を行わず、Anthropic API を
一切呼ばない。チャンネル設定の `enrich_enabled`（既定 false）で制御する。
enrich_enabled=false のチャンネルは、--mock を付けない限り enrich 関連の
コード（enrich.py 経由の実 API 呼び出し）に一切到達しない。`--mock` は
`enrich_enabled` の値に関わらず模擬応答での検証を行える（本切り替えを
数週間後に有効化する前の確認用）。

成果物はすべて実行時のカレントディレクトリからの相対パスで書き出す
（絶対パスを使わない。CLAUDE.md の制約）。そのため常にリポジトリルートで
実行すること。
"""

from __future__ import annotations

import argparse
import functools
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .enrich import PendingItem, call_anthropic, enrich_channel
from .fetch import fetch_source
from .mock import build_mock_raw_items, mock_call_model
from .render import render_channel_html, render_feed_xml
from .state import items_for_channel, load_state, merge_raw_items, prune_channel, save_state

logger = logging.getLogger(__name__)

CONFIG_PATH = "config/sources.toml"  # リポジトリルートからの相対パス（要件書§6）
STATE_PATH = "state/items.json"  # リポジトリルートからの相対パス（FR-3.1）
_MIN_DATETIME = datetime.min.replace(tzinfo=timezone.utc)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="watch.main")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Claude API を呼ばず、収集と描画だけ行う（NFR-2）。--mock より優先される。",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="実データ・実APIの代わりに模擬記事・模擬応答を使い、enrichまで通しで検証する。",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="既訳を破棄して再生成する（FR-2.17）。",
    )
    parser.add_argument(
        "--channel",
        action="append",
        help="処理対象をこのチャンネル id に限定する（複数指定可）。--refresh と併用できる。",
    )
    return parser


def _parse_iso(value: str | None) -> datetime:
    if not value:
        return _MIN_DATETIME
    return datetime.fromisoformat(value)


def _select_pending(records: dict, channel_id: str, cap: int) -> list[PendingItem]:
    """未 enrich の記事を first_seen の古い順に選び、1回あたりの上限で切る（FR-2.14）。"""
    pending_records = [r for r in items_for_channel(records, channel_id) if not r.get("enriched")]
    pending_records.sort(key=lambda r: _parse_iso(r["first_seen"]))
    pending_records = pending_records[:cap]
    return [
        PendingItem(key=r["key"], language=r["language"], title=r["title"], summary=r["summary"])
        for r in pending_records
    ]


def _apply_refresh(records: dict, channel_id: str, target_channels: list[str] | None) -> None:
    """既訳を破棄する（FR-2.17）。--channel 指定があればそのチャンネルだけに限定する。"""
    if target_channels is not None and channel_id not in target_channels:
        return
    for record in items_for_channel(records, channel_id):
        record["enriched"] = False


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_arg_parser().parse_args(argv)

    config = load_config(Path(CONFIG_PATH))
    records = load_state(Path(STATE_PATH))
    now = datetime.now(timezone.utc)

    channels = config.enabled_channels()
    if args.channel:
        channels = [c for c in channels if c.id in args.channel]

    for channel in channels:
        if args.mock:
            raw_items = build_mock_raw_items(channel)
        else:
            raw_items = []
            for source in config.sources_for(channel.id):
                fetched = fetch_source(source)
                logger.info("source %s: %d item(s) after filtering", source.id, len(fetched))
                raw_items.extend(fetched)

        # first_seen と enrich 済みフィールドは merge 時点で保護される（FR-3.2）。
        records = merge_raw_items(records, raw_items, now)
        # 保持件数の上限は max_items のみで決まる。display_limit はここに渡さない（FR-3.5）。
        records = prune_channel(records, channel.id, channel.max_items)

        if args.refresh:
            _apply_refresh(records, channel.id, args.channel)

        run_enrich = not args.dry_run and (channel.enrich_enabled or args.mock)

        if not run_enrich:
            if not args.dry_run and not channel.enrich_enabled:
                logger.info(
                    "channel %s: enrich_enabled=false, skipping API call entirely", channel.id
                )
        else:
            pending = _select_pending(records, channel.id, config.model.max_enrich_per_run)
            if not pending:
                # §9: 新規記事ゼロ → API を呼ばず、ページの更新時刻だけ更新する。
                logger.info("channel %s: no pending items, skipping API call", channel.id)
            else:
                call_model = (
                    functools.partial(mock_call_model, channel=channel)
                    if args.mock
                    else call_anthropic
                )
                results = enrich_channel(
                    channel, config.model.name, pending, call_model=call_model
                )
                for key, result in results.items():
                    records[key].update(
                        {
                            "enriched": True,
                            "title_ja": result.title_ja,
                            "summary_ja": result.summary_ja,
                            "score": result.score,
                            "doc_type": result.doc_type,
                            "tags": result.tags,
                            "deadline": result.deadline,
                        }
                    )
                failed = len(pending) - len(results)
                if failed:
                    logger.warning(
                        "channel %s: %d item(s) could not be enriched, will retry next run",
                        channel.id,
                        failed,
                    )
                logger.info(
                    "channel %s: enriched %d/%d pending item(s)",
                    channel.id,
                    len(results),
                    len(pending),
                )

        channel_records = items_for_channel(records, channel.id)
        channel_records.sort(key=lambda r: _parse_iso(r["published"]), reverse=True)

        html = render_channel_html(channel, channel_records, config, now)
        out_path = Path("public") / channel.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")

        feed = render_feed_xml(channel, channel_records, channel.feed_limit)
        feed_path = Path("public") / channel.feed
        feed_path.write_text(feed, encoding="utf-8")

        logger.info(
            "channel %s: %d item(s) -> %s, %s",
            channel.id,
            len(channel_records),
            out_path,
            feed_path,
        )

    save_state(Path(STATE_PATH), records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
