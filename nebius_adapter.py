#!/usr/bin/env python3
"""
Nebius Token Factory アダプタ interface (demo6 WatchDigest / Nebius ハッカソン)

nemotron_brief.py 本体はこの interface だけに依存する。実装は2つ:

  - MockNebiusAdapter        : ネットワークなしの検証用実装。プロンプトに
                               埋め込まれた alert ペイロード JSON を読み、
                               その数値だけで定型ブリーフィングを組み立てて
                               返す (実 LLM の出力のオフライン代替)。
                               パイプライン全体 + 数値検証器をオフラインで
                               一気通貫検証するために使う。
  - TokenFactoryNebiusAdapter: 実 Nebius Token Factory 用実装。
                               OpenAI 互換 Chat Completions API を標準ライブラリ
                               (urllib) で叩く。認証情報は環境変数からのみ読む:
                                 NEBIUS_API_KEY   (必須。コード/ログに書かない)
                                 NEBIUS_BASE_URL  (既定 https://api.tokenfactory.nebius.com/v1)
                                 NEBIUS_MODEL     (既定 nvidia/Nemotron-3.5-Super-VL)
                               実API検証済み (2026-10-28): スモークテスト HTTP 200、
                               WatchDigest 実API実行で数値検証 PASS (39 件一致)。
                               実測と注意点は README の "Verified live run" を参照。

切替: 環境変数 WATCHDIGEST_NEBIUS_ADAPTER=mock|tokenfactory (既定 mock)。

API 形状 (2026-10-25 に Nebius 公式ドキュメント/ブログで確認):
  POST {base}/chat/completions
  Authorization: Bearer <NEBIUS_API_KEY>
  Body: {"model": ..., "messages": [{"role": ..., "content": ...}], ...}
  応答: choices[0].message.content / usage
  出典: https://dev.nebius.com/cookbook/beam-reflection-token-factory
        https://nebius.com/blog/posts/three-new-open-weight-models-from-us-companies-give-builders-more-choice
"""

from __future__ import annotations

import json
import os
import urllib.request

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
DEFAULT_MODEL = "nvidia/Nemotron-3.5-Super-VL"


class NebiusAdapter:
    """本体が依存する interface。実装は chat_complete を備えること。"""

    is_mock: bool = False
    name: str = "base"
    model: str = ""

    def chat_complete(self, messages: list[dict], *, max_tokens: int = 1200,
                      temperature: float = 0.3) -> str:
        """messages は OpenAI 形式 [{"role": ..., "content": ...}]。
        応答テキスト (choices[0].message.content) を返す。"""
        raise NotImplementedError


class MockNebiusAdapter(NebiusAdapter):
    """検証用モック。実接続なし。

    実 LLM は「プロンプト中の alert JSON だけを見て文章化する」役割なので、
    モックも同じ規律を守る: ユーザーメッセージ末尾の JSON ペイロードを読み、
    そこに存在する数値だけを使ってブリーフィングを組み立てる。
    ペイロードに無い数値は一切書かない (検証器で落ちる文を作らないため)。
    """

    is_mock = True
    name = "mock"
    model = "mock-nemotron (offline canned)"

    def chat_complete(self, messages, *, max_tokens=1200, temperature=0.3):
        user = next((m["content"] for m in reversed(messages)
                     if m.get("role") == "user"), "")
        payload = json.loads(user[user.index("{"):])
        return _canned_briefing(payload)


def _canned_briefing(p: dict) -> str:
    """alert ペイロードから定型ブリーフィングを組む (数値はペイロード内のみ)。"""
    meta = p["meta"]
    wallets = {w["name"]: w for w in p["wallets"]}
    lines = []
    lines.append("# WatchDigest weekly briefing")
    lines.append("")
    lines.append(
        f"This week WatchDigest tracked {p['wallet_count']} wallets and the "
        f"deterministic rule engine fired {p['alert_total']} alerts. "
        f"Data window ends {meta['data_window_end_jst']}. "
        "Every figure below comes from the alert payload; nothing is estimated.")
    lines.append("")
    lines.append("## What changed")
    lines.append("")
    for w in p["wallets"]:
        for a in w["alerts"]:
            lines.append(f"- **{w['name']}** — {a['brief']}")
    lines.append("")
    lines.append("## Wallet values in the payload")
    lines.append("")
    for name in ("BreakTheBank", "Tokor", "floatingraft", "getbipped",
                 "CoinTrick", "RichieRicher"):
        w = wallets[name]
        lines.append(f"- {name}: portfolio value ${w['value_usd']:,.0f} "
                     f"across {w['positions_count']} open positions.")
    lines.append("")
    lines.append(
        "Read-only research summary of public data. Not investment advice, "
        "not a trading signal, and not a recommendation to copy any wallet.")
    return "\n".join(lines) + "\n"


class TokenFactoryNebiusAdapter(NebiusAdapter):
    """実 Nebius Token Factory 用 (2026-10-28 に実APIで検証済み)。

    認証情報は環境変数からのみ読む。コード・ログ・出力にキーを書かないこと。
    ネットワーク呼び出しはメソッド実行時にのみ行う (import/初期化では行わない)。
    """

    is_mock = False
    name = "tokenfactory"

    def __init__(self) -> None:
        self.api_key = os.environ.get("NEBIUS_API_KEY", "")
        self.base = os.environ.get("NEBIUS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.model = os.environ.get("NEBIUS_MODEL", DEFAULT_MODEL)

    def _require_key(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "NEBIUS_API_KEY が未設定です。Token Factory でキーを発行し、"
                "環境変数で渡してください (コードに直書きしないこと)。")

    def chat_complete(self, messages, *, max_tokens=1200, temperature=0.3):
        self._require_key()
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        req = urllib.request.Request(
            f"{self.base}/chat/completions",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read())
        self.last_usage = payload.get("usage", {})
        self.last_finish_reason = payload["choices"][0].get("finish_reason")
        import sys as _sys
        print(f"[usage] model={self.model} usage={json.dumps(self.last_usage)} "
              f"finish_reason={self.last_finish_reason}", file=_sys.stderr)
        content = payload["choices"][0]["message"].get("content")
        if not content:
            raise RuntimeError(
                "Token Factory が空の content を返しました "
                f"(finish_reason={payload['choices'][0].get('finish_reason')!r})")
        return content


def get_adapter(name: str | None = None) -> NebiusAdapter:
    """環境変数 WATCHDIGEST_NEBIUS_ADAPTER (既定 mock) で実装を選ぶ。"""
    selected = (name or os.environ.get(
        "WATCHDIGEST_NEBIUS_ADAPTER", "mock")).strip().lower()
    if selected == "mock":
        return MockNebiusAdapter()
    if selected in ("tokenfactory", "live"):
        return TokenFactoryNebiusAdapter()
    raise ValueError(f"未知のアダプタ: {selected} (mock|tokenfactory)")
