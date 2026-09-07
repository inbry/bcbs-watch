"""検証用の模擬データ・模擬応答（`--mock`）。

実際の BIS フィード取得や Anthropic API 呼び出しを行わずに、翻訳・要約・
スコア付けが済んだ記事が並ぶ状態を再現するためのモジュール。

- `build_mock_raw_items` は `fetch_source` の代わりに使う模擬記事（RawItem）を生成する
- `mock_call_model` は `enrich.call_anthropic` と同じシグネチャの模擬応答関数で、
  `enrich_channel(..., call_model=...)` にそのまま差し込める

呼び出し側（`enrich_channel`）のロジックは変わらない。差し替えているのは
「実際にネットワークへ出て行く関数」だけである。

模擬データは要件書の各項目を検証できるよう、以下を1セットに含める：
- `language = "en"` / `"ja"` の両方（FR-2.1〜2.3）
- スコアが 0〜49 / 50〜79 / 80〜100 の各帯（FR-4.5, FR-4.9b）
- 分類が選択肢にない値を返すケース（FR-2.10 のフォールバック検証）
- 締切ありのケース／なしのケース（FR-2.11）
- 件数は30件（表示件数の初期値20件を超える数。FR-4.8/4.9 の検証に使う）

標準ライブラリの json のみを使用する（D-5）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .config import Channel
from .fetch import RawItem

MOCK_ITEM_COUNT = 30

_TOPICS = [
    "market risk capital requirements",
    "liquidity coverage ratio disclosure",
    "operational resilience principles",
    "credit valuation adjustment framework",
    "leverage ratio treatment of derivatives",
    "cryptoasset exposure standard",
    "stress testing principles",
    "third-party risk management guidance",
    "climate-related financial risk disclosure",
    "counterparty credit risk methodology",
]


def build_mock_raw_items(channel: Channel) -> list[RawItem]:
    """`channel` 向けの模擬記事を生成する。実際のRSS取得の代わりに使う。

    URL・タイトルは実データと混同しないよう "mock" だとひと目でわかる形にする。
    """
    now = datetime.now(timezone.utc)
    items = []
    for i in range(MOCK_ITEM_COUNT):
        topic = _TOPICS[i % len(_TOPICS)]
        language = "ja" if i % 5 == 0 else "en"  # 30件中6件を日本語情報源扱いにする
        published = now - timedelta(days=i)  # 新しい順に日付をばらけさせる

        if language == "en":
            title = f"Basel Committee mock notice {i:02d}: {topic}"
            summary = (
                f"[MOCK] The Basel Committee issued a mock notice on {topic} "
                f"for verification purposes (item {i:02d})."
            )
        else:
            title = f"（模擬）バーゼル委員会関連メモ {i:02d}：{topic}"
            summary = f"（模擬データ）検証用のメモ（{i:02d}件目、{topic}）。"

        items.append(
            RawItem(
                key=f"mock-{channel.id}-{i:02d}",
                channel=channel.id,
                source_id="mock",
                source_label="MOCK",
                language=language,
                title=title,
                summary=summary,
                link=f"https://example.invalid/mock/{channel.id}/{i:02d}",
                published=published,
            )
        )
    return items


def _extract_payload(user_prompt: str) -> list[dict]:
    # enrich.build_user_prompt は末尾に JSON 配列を1つだけ埋め込む。
    start = user_prompt.index("[")
    return json.loads(user_prompt[start:])


def mock_call_model(model: str, system_prompt: str, user_prompt: str, channel: Channel) -> str:
    """`enrich.call_anthropic` と同じ3引数シグネチャの模擬応答関数。

    `functools.partial(mock_call_model, channel=channel)` で束縛してから
    `enrich_channel(..., call_model=...)` に渡す。ネットワークは一切使わない。
    分類の選択肢はハードコードせず、`channel.doc_types`（設定）から作る。
    選択肢にない値を返すケースの検証のためだけに "unknown_doc_type" という
    ダミー値を1つ混ぜる（FR-2.10 のフォールバックが正しく効くかの確認用）。
    """
    payload = _extract_payload(user_prompt)
    doc_type_cycle = list(channel.doc_types) + ["unknown_doc_type"]
    now = datetime.now(timezone.utc)

    results = []
    for i, entry in enumerate(payload):
        language = entry.get("language", "en")
        title = entry.get("title", "")

        if language == "ja":
            title_ja = title  # FR-2.3: 日本語記事は翻訳しない
        else:
            title_ja = f"（模擬訳）{title}"

        deadline = ""
        if i % 4 == 0:
            deadline = (now + timedelta(days=7 + i)).date().isoformat()

        results.append(
            {
                "key": entry.get("key"),
                "title_ja": title_ja,
                "summary_ja": f"（模擬要約）{entry.get('summary', '')[:40]}",
                "score": (i * 13 + 7) % 101,  # 0〜100 に分散させる
                "doc_type": doc_type_cycle[i % len(doc_type_cycle)],
                "tags": ["模擬", f"tag{i % 3}"],
                "deadline": deadline,
            }
        )
    return json.dumps(results, ensure_ascii=False)
