#!/usr/bin/env python3
"""
WatchDigest ブリーフィング生成 + 数値検証 (Nebius ハッカソン移植レイヤー)

フロー: digest.json → alert ペイロード抽出 → Nemotron (Token Factory) が
文章化 → 数値検証器が「文中の全数値がソースデータに存在すること」を確認 →
通過したものだけを brief.md として出力する。

設計原則 (rank7-nebius-submission.md §2):
  - 検知は決定論 (digest.py のルールエンジン)。LLM は計算しない。
  - LLM には alert ペイロード JSON だけを渡す。数値を新たに作ることを
    プロンプトでも禁止するが、それでも検証器で弾く二重構えにする。
  - 検証器は決定論: 文中から抽出した全数値トークンが、digest.json 内の
    数値フィールド (丸め違いを許容) か、文字列フィールド中の数値
    (アラート文・日付等) のいずれかに一致しなければ却下する。

使い方:
    python3 nemotron_brief.py --selftest            # モックで正例+改竄却下テスト (オフライン)
    python3 nemotron_brief.py --demo                # demo_output/digest.json + モックで brief.md 生成
    python3 nemotron_brief.py --digest output/digest.json --adapter tokenfactory
                                                    # 実キーがあれば Token Factory で生成 (要 NEBIUS_API_KEY)
    python3 nemotron_brief.py --demo --dry-run      # API を呼ばず送信ペイロードを表示

依存は標準ライブラリのみ。アダプタは nebius_adapter.py (mock / tokenfactory)。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from nebius_adapter import get_adapter  # noqa: E402

NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

SYSTEM_PROMPT = (
    "You are the narration layer of WatchDigest, a read-only research tool. "
    "You receive ONLY a JSON payload of alerts produced by a deterministic "
    "rule engine. Write a short, plain briefing from it. Hard rules: "
    "(1) Use only numbers that appear in the payload — never invent, round "
    "differently, or estimate a figure. "
    "(2) State only facts present in the payload. "
    "(3) No investment advice, no trading signals, no recommendation to "
    "follow or copy any wallet. "
    "If the payload has no alerts for a wallet, do not invent activity for it."
)


# ---------------------------------------------------------------------------
# ペイロード抽出 (LLM に渡すのは alert と要約数値だけ)
# ---------------------------------------------------------------------------
def _alert_brief(w: dict, a: dict) -> str:
    """アラート1件を、ソース数値だけで構成した英語の素材文にする。"""
    kind = a["kind"]
    if kind == "concentration":
        return (f"concentration alert: its largest position is "
                f"{w['top1_share_pct']:.1f}% of its portfolio value.")
    if kind == "bot_like":
        return (f"bot-like cadence alert: median interval between trades is "
                f"{w['median_interval_sec']:.0f} seconds.")
    if kind == "sweep":
        return (f"sweep alert: {w['sweep_share_pct']:.1f}% of recent trades "
                f"were priced at 0.95 or above, a near-settlement sweep pattern.")
    if kind == "dormant":
        m = re.search(r"([\d.]+)\s*日経過", a["text"])
        days = m.group(1) if m else "?"
        return (f"dormancy alert: no trades for {days} days; "
                f"last trade {w['last_trade_jst']}.")
    if kind == "cadence_collapse":
        m = re.findall(r"([\d.]+)秒", a["text"])
        if len(m) >= 2:
            return (f"cadence collapse alert: median trade interval fell "
                    f"from {m[0]} seconds to {m[1]} seconds.")
        return "cadence collapse alert: median trade interval fell sharply."
    if kind == "value_change":
        m = re.search(r"([+-][\d.]+)%", a["text"])
        pct = m.group(1) if m else "?"
        return f"value change alert: portfolio value moved {pct}% versus baseline."
    return f"{kind} alert."


def build_payload(digest: dict) -> dict:
    wallets = []
    for w in digest["wallets"]:
        wallets.append({
            "name": w["name"],
            "value_usd": w["value_usd"],
            "positions_count": w["positions_count"],
            "top1_share_pct": w["top1_share_pct"],
            "median_interval_sec": w["median_interval_sec"],
            "sweep_share_pct": w["sweep_share_pct"],
            "last_trade_jst": w["last_trade_jst"],
            "alerts": [{"kind": a["kind"], "brief": _alert_brief(w, a)}
                       for a in w["alerts"]],
        })
    return {
        "meta": digest["meta"],
        "wallet_count": len(wallets),
        "alert_total": digest["meta"]["alert_total"],
        "wallets": wallets,
    }


def build_messages(payload: dict) -> list[dict]:
    user = (
        "Write the weekly WatchDigest briefing from this alert payload. "
        "Cover every alert, then list the wallet values given in the payload. "
        "Use only the numbers in the payload.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2))
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# 数値検証器 (決定論)
# ---------------------------------------------------------------------------
def allowed_numbers(digest: dict) -> set[float]:
    """digest.json の全数値 + 全文字列中の数値を、丸め違い込みで集める。"""
    allowed: set[float] = set()

    def add(v: float) -> None:
        allowed.add(float(v))
        allowed.add(float(round(v)))
        allowed.add(float(round(v, 1)))
        allowed.add(float(round(v, 2)))

    def walk(node) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            add(node)
        elif isinstance(node, str):
            for tok in NUM_RE.findall(node):
                add(float(tok.replace(",", "")))
        elif isinstance(node, dict):
            for x in node.values():
                walk(x)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(digest)
    return allowed


def extract_numbers(text: str) -> list[float]:
    return [float(tok.replace(",", "")) for tok in NUM_RE.findall(text)]


def verify_briefing(text: str, digest: dict) -> tuple[bool, list[float], int]:
    """文中の全数値がソースに存在するか検証する。
    戻り値: (通過か, 却下理由の数値リスト, 検査した数値の個数)。
    丸め (整数化/小数1位/2位) はソース側で吸収する。許容誤差は丸めの
    最大ずれ 0.5 まで (それを超える一致は偶然の近傍値として扱わない
    ために、集合に無い値は誤差内でも代表値との差で判定する)。"""
    allowed = allowed_numbers(digest)
    bad: list[float] = []
    nums = extract_numbers(text)
    for n in nums:
        if n in allowed:
            continue
        # 丸めで集合に入らなかったケースの救済: ソース値との差が 0.5 以内
        if any(abs(n - s) <= 0.5 for s in allowed):
            continue
        bad.append(n)
    return (not bad), bad, len(nums)


# ---------------------------------------------------------------------------
# パイプライン
# ---------------------------------------------------------------------------
def run_pipeline(digest_path: Path, adapter, out_dir: Path,
                 dry_run: bool = False) -> int:
    digest = json.loads(digest_path.read_text(encoding="utf-8"))
    payload = build_payload(digest)
    messages = build_messages(payload)
    if dry_run:
        print("=== dry-run: 送信予定ペイロード (API は呼びません) ===")
        print(json.dumps({"model": getattr(adapter, "model", ""),
                          "messages": messages}, ensure_ascii=False, indent=2))
        return 0
    # Nemotron-3.5-Super-VL は reasoning トークンを消費するため、実APIでは
    # 1200 では content が空になる (finish_reason=length を実測, 2026-10-28)。
    # 余裕を持たせて 4000 で呼ぶ。モックはこの引数を無視する。
    text = adapter.chat_complete(messages, max_tokens=4000)
    ok, bad, checked = verify_briefing(text, digest)
    out_dir.mkdir(parents=True, exist_ok=True)
    if ok:
        header = (f"<!-- Generated by WatchDigest narration layer "
                  f"(adapter: {adapter.name}, model: {adapter.model}). "
                  f"Numeric verification: PASS ({checked} numbers checked "
                  f"against source data). -->\n\n")
        (out_dir / "brief.md").write_text(header + text, encoding="utf-8")
        print(f"[verify] PASS — 数値 {checked} 件すべてソースデータと一致")
        print(f"[out] {out_dir / 'brief.md'}")
        return 0
    (out_dir / "brief_rejected.md").write_text(text, encoding="utf-8")
    print(f"[verify] REJECT — ソースに存在しない数値: {bad}")
    print(f"[out] 却下文を保存: {out_dir / 'brief_rejected.md'} (brief.md は更新しません)")
    return 1


def selftest(digest_path: Path) -> int:
    """モックで正例 (通過) と改竄文 (却下) の両方を検証する。"""
    digest = json.loads(digest_path.read_text(encoding="utf-8"))
    adapter = get_adapter("mock")
    payload = build_payload(digest)
    text = adapter.chat_complete(build_messages(payload))

    print("=== 正例: モック生成ブリーフィング ===")
    ok, bad, checked = verify_briefing(text, digest)
    print(f"  検査数値 {checked} 件 / ソース不一致 {bad}")
    print(f"  結果: {'PASS (期待通り通過)' if ok else 'FAIL (通過してはいけない)'}")

    print("=== 負例: 数値を改竄した文 ===")
    tampered = text.replace("$874,492", "$1,012,569")
    tampered = tampered.replace(
        "## Wallet values in the payload",
        "- Fabricated claim: this wallet has a win rate of 87%.\n\n"
        "## Wallet values in the payload")
    ok2, bad2, checked2 = verify_briefing(tampered, digest)
    print(f"  検査数値 {checked2} 件 / ソース不一致 {bad2}")
    print(f"  結果: {'REJECT (期待通り却下)' if not ok2 else 'FAIL (却下されるべき)'}")

    all_ok = ok and not ok2
    print(f"総合: {'ALL OK' if all_ok else '一部 NG'}")
    return 0 if all_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="WatchDigest ブリーフィング生成 + 数値検証")
    parser.add_argument("--digest", help="digest.json のパス")
    parser.add_argument("--demo", action="store_true",
                        help="demo_output/digest.json を使う")
    parser.add_argument("--selftest", action="store_true",
                        help="モックで正例+改竄却下テスト (オフライン)")
    parser.add_argument("--adapter", default=None,
                        help="mock|tokenfactory (既定: 環境変数 WATCHDIGEST_NEBIUS_ADAPTER、無ければ mock)")
    parser.add_argument("--dry-run", action="store_true",
                        help="API を呼ばず送信ペイロードを表示")
    parser.add_argument("--out-dir", default=str(BASE_DIR / "output"))
    args = parser.parse_args()

    if args.demo or args.selftest:
        digest_path = BASE_DIR / "demo_output" / "digest.json"
    elif args.digest:
        digest_path = Path(args.digest)
    else:
        print("使い方: --selftest / --demo / --digest <digest.json> のいずれかを指定してください。")
        return 2

    if args.selftest:
        return selftest(digest_path)
    adapter = get_adapter(args.adapter)
    print(f"[info] adapter={adapter.name} model={adapter.model}")
    return run_pipeline(digest_path, adapter, Path(args.out_dir),
                        dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
