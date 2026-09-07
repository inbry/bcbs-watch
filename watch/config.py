"""設定ファイル（config.toml）の読み込み。

分類の選択肢・関心プロファイル・用語集など、チャンネルごとに変わりうるものは
すべてここで TOML から読み込んだ値として保持する。コード側はそれらを
「単なる文字列・リスト・辞書」として扱うだけで、選択肢そのものをコードに
書かない（FR-0.4, FR-0.5）。

標準ライブラリの tomllib のみを使用する（D-5）。
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Site:
    timezone: str = "Asia/Tokyo"


@dataclass
class Model:
    name: str = "claude-sonnet-5"
    max_enrich_per_run: int = 15


@dataclass
class Channel:
    id: str
    label: str
    output: str
    feed: str
    hero: str = "none"  # "deadline" | "none"（FR-0.6）
    enrich_enabled: bool = False  # 翻訳・要約・スコア付け（enrich）を行うか。既定は無効
    max_items: int = 120  # 保持件数（FR-3.4）。表示件数（display_limit）とは別設定（FR-3.5）
    display_limit: int = 20  # 表示件数（FR-4.8）
    display_new_hours: int = 72  # NEW を出す時間（FR-4.10）
    display_always_score: int = 80  # この点以上は表示件数の上限を無視（FR-4.9b）
    feed_limit: int = 50  # RSSフィードの件数。display_limit とは別設定（FR-4.12）
    doc_types: list[str] = field(default_factory=list)  # FR-0.4: 選択肢はここにだけ存在する
    doc_type_labels: dict[str, str] = field(default_factory=dict)
    profile: str = ""  # 関心プロファイル（§5）。ページには出力しない（C-1）
    glossary: dict[str, str] = field(default_factory=dict)


@dataclass
class Source:
    id: str
    channel: str
    label: str
    kind: str
    url: str
    language: str = "en"
    include: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class Config:
    site: Site
    model: Model
    channels: dict[str, Channel]
    sources: list[Source]

    def enabled_channels(self) -> list[Channel]:
        return list(self.channels.values())

    def sources_for(self, channel_id: str) -> list[Source]:
        return [s for s in self.sources if s.channel == channel_id and s.enabled]


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    site = Site(**data.get("site", {}))
    model = Model(**data.get("model", {}))

    channels: dict[str, Channel] = {}
    for raw in data.get("channel", []):
        raw = dict(raw)
        doc_type_labels = raw.pop("doc_type_labels", {})
        glossary = raw.pop("glossary", {})
        channel = Channel(doc_type_labels=doc_type_labels, glossary=glossary, **raw)
        channels[channel.id] = channel

    sources: list[Source] = []
    for raw in data.get("source", []):
        source = Source(**raw)
        if source.enabled and source.channel not in channels:
            # §9: 情報源が未定義のチャンネルを指す場合は無効化して警告し、処理は続ける
            logger.warning(
                "source %s: channel %r is not defined, disabling this source",
                source.id,
                source.channel,
            )
            source.enabled = False
        sources.append(source)

    return Config(site=site, model=model, channels=channels, sources=sources)
