"""翻訳・要約・スコア付け（FR-2）。

同一チャンネルの全記事を1回の API 呼び出しにまとめる（FR-2.15）。
Anthropic API へのアクセスは標準ライブラリの urllib のみで行う（D-5）。
API キーは環境変数 ANTHROPIC_API_KEY からのみ読む。コードにも設定ファイル
にも書かない（NFR-4）。

**検証用の模擬応答を差し込める設計。** `enrich_channel` は `call_model`
引数（既定は `call_anthropic`）を差し替えられるようにしてあるだけで、
それ以外のロジック（バッチ化・JSON パース・分類のフォールバック・
language による分岐など）は本物のAPIでも模擬応答でも共通である。
`watch.mock.mock_call_model` が `call_anthropic` と同じシグネチャの
模擬実装（--mock オプション用）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date

from .config import Channel

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 4096


@dataclass
class PendingItem:
    """enrich 対象1件の入力（enrich 前の生データ）。"""

    key: str
    language: str
    title: str
    summary: str


@dataclass
class EnrichResult:
    """enrich 1件分の出力。"""

    key: str
    title_ja: str
    summary_ja: str
    score: int
    doc_type: str
    tags: list[str] = field(default_factory=list)
    deadline: str = ""


# チャンネルの関心プロファイル・分類の選択肢・用語集は、すべてチャンネル設定
# （config.toml の [[channel]]）から埋め込む。コード側にこれらの値そのもの
# （選択肢の文字列や採点基準）を持たない（FR-0.4, FR-0.5）。
SYSTEM_PROMPT_TEMPLATE = """あなたは規制動向トリアージサイトの編集者です。読者プロファイル:

{profile}

出力は必ず JSON 配列のみを返すこと。前後に説明文やコードフェンスを付けない
こと。配列の各要素は入力の記事1件に対応し、次のキーを持つこと:

- "key": 入力と同じ記事キー（そのまま返す）
- "title_ja": 見出しの日本語訳。逐語訳ではなく、日本の金融規制実務で通じる
  自然な日本語にすること。入力の "language" が "ja" の記事は翻訳せず、
  入力の "title" をそのまま返すこと（言い回しも変えないこと）
- "summary_ja": 日本語の要約を2〜3文で。「何が出たか」に加えて
  「実務上どこが効くか」を必ず含めること。**根拠は入力として与えられた
  見出しと公式サマリーの内容だけに限ること。そこに書かれていない数値・
  条項・日付・固有名詞などの事実を、自分の知識から補って書き加えては
  いけない。**入力に無い情報は、たとえ一般的によく知られていると思っても
  書かないこと。また、**原文の逐語的な言い換え（文の並びや表現をなぞる
  だけの訳）にせず、要約者自身の言葉で書くこと。**
- "score": 0〜100の整数。上記プロファイルとの関連度で判断すること
- "doc_type": 次のいずれか1つの文字列: {doc_types}
- "tags": 短いタグを最大3つ（配列）。日本語を基本とする
- "deadline": コメント提出期限が本文に明記されている場合のみ "YYYY-MM-DD"
  形式で。明記がなければ空文字列 "" とすること（推測しないこと）

訳語は次の用語集に従い、実行のたびにブレないこと:
{glossary}

FRTB / SA-CCR / LCR / NSFR / CVA / G-SIB のような定着した略語は英語のまま
残すこと。無理に和訳しないこと。
"""


def build_system_prompt(channel: Channel) -> str:
    glossary_lines = "\n".join(f'- "{en}" → {ja}' for en, ja in channel.glossary.items())
    return SYSTEM_PROMPT_TEMPLATE.format(
        profile=channel.profile.strip(),
        doc_types=", ".join(channel.doc_types),
        glossary=glossary_lines or "(なし)",
    )


def build_user_prompt(items: list[PendingItem]) -> str:
    payload = [
        {
            "key": item.key,
            "language": item.language,
            "title": item.title,
            "summary": item.summary,
        }
        for item in items
    ]
    return (
        "以下の記事それぞれについて、指定した JSON 配列形式で返してください。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _strip_code_fence(text: str) -> str:
    # §9: API が JSON 以外（コードフェンス付きなど）を返したら、剥がして再パースを試みる
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return match.group(1) if match else text


def _parse_response_text(text: str) -> list[dict]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    stripped = _strip_code_fence(text)
    return json.loads(stripped)  # ここでも失敗すれば例外が呼び出し元に伝播する


def call_anthropic(model: str, system_prompt: str, user_prompt: str) -> str:
    """実際の Anthropic API を呼ぶ。標準ライブラリの urllib のみを使用する。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    body = json.dumps(
        {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read())
    return "".join(block.get("text", "") for block in payload.get("content", []))


def _valid_deadline(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        date.fromisoformat(value)
    except ValueError:
        return ""  # §9: 期限の抽出が不正な形式なら空として扱う
    return value


def _normalize(raw: dict, item_by_key: dict[str, PendingItem], channel: Channel) -> EnrichResult:
    key = raw.get("key", "")
    item = item_by_key.get(key)

    title_ja = raw.get("title_ja") or (item.title if item else "")
    if item is not None and item.language == "ja":
        # FR-2.3: 日本語記事は翻訳しない。モデルが何を返しても原文を優先する
        # （同じ文字列が見出しと原題で2度並ぶ事故を防ぐ）。
        title_ja = item.title

    doc_type = raw.get("doc_type")
    if doc_type not in channel.doc_types:
        # FR-2.10: 選択肢にない値は「その他」相当に丸める
        doc_type = "other" if "other" in channel.doc_types else channel.doc_types[0]

    try:
        score = max(0, min(100, int(raw.get("score"))))
    except (TypeError, ValueError):
        score = 0

    tags = raw.get("tags")
    if not isinstance(tags, list):
        tags = []
    tags = [str(t) for t in tags][:3]

    return EnrichResult(
        key=key,
        title_ja=str(title_ja),
        summary_ja=str(raw.get("summary_ja", "")),
        score=score,
        doc_type=doc_type,
        tags=tags,
        deadline=_valid_deadline(raw.get("deadline")),
    )


def enrich_channel(
    channel: Channel,
    model: str,
    items: list[PendingItem],
    call_model=call_anthropic,
) -> dict[str, EnrichResult]:
    """1チャンネル分の新規記事をまとめて1回で enrich する（FR-2.15）。

    API 呼び出しの失敗、または JSON 以外の応答でコードフェンス除去後も
    パースに失敗した場合は空の dict を返す。呼び出し側はそれを「このバッチ
    全件が未翻訳のまま」として扱うこと（§9：原題とリンクのみで掲載し、
    次回実行で再試行する）。
    """
    if not items:
        return {}

    system_prompt = build_system_prompt(channel)
    user_prompt = build_user_prompt(items)

    try:
        text = call_model(model, system_prompt, user_prompt)
        raw_results = _parse_response_text(text)
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError, OSError) as exc:
        logger.warning("channel %s: enrich call failed (%s)", channel.id, exc)
        return {}

    item_by_key = {item.key: item for item in items}
    results: dict[str, EnrichResult] = {}
    for raw in raw_results:
        if not isinstance(raw, dict) or raw.get("key") not in item_by_key:
            continue
        result = _normalize(raw, item_by_key, channel)
        results[result.key] = result
    return results
