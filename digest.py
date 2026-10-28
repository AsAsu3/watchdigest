#!/usr/bin/env python3
"""
ウォレット監視ダイジェスト — プロトタイプ本体 (Rank 6 マイクロSaaS MVP)

フロー: 取得/読込 → 集計 → ベースライン比較 (乖離アラート) → ダイジェスト生成
  - 取得:   Polymarket data-api (公開・認証不要) の /value /positions
            /closed-positions (DESC+ASC) /activity を urllib で取得 (--fetch)。
            呼び出し間隔 0.9 秒以上、429 が返ったらその取得は打ち切る。
  - 読込:   既存スナップショット JSON (--input / --demo)。
            --demo は D1 サンプル取得分 (hidden_files/d1_raw/raw.json +
            closed_asc.json) を使い、ネットワークに一切触れない。
  - 集計:   ウォレットごとにポートフォリオ価値・保有上位・集中度・
            決済サンプル両裾・直近アクティビティのケイデンス等を算出。
  - 比較:   watchlist.json のベースライン + rules.json の閾値で乖離アラートを
            判定 (rank4-service-menu.md §5.1 のアラート定義をそのまま実装)。
  - 生成:   digest.md (人間向け) と digest.json (機械可読・将来の配信/課金の核)
            を出力する。

これは「公開データの事実整理」であり、投資助言・売買シグナル・コピー取引の
推奨ではない。外部送信・課金・アカウント登録は一切行わない。

使い方:
    python3 digest.py --demo            # キャッシュ済み D1 データで生成 + 自己検証
    python3 digest.py --fetch           # 公開APIから新規取得して生成 (結果は snapshots/)
    python3 digest.py --input snap.json # スナップショット JSON から生成

依存は標準ライブラリのみ。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
DEFAULT_CACHE_DIR = BASE_DIR.parent / "hidden_files" / "d1_raw"

DISCLAIMER = (
    "本ダイジェストは Polymarket の公開データ (公式 data-api) のみを用いた事実の整理です。"
    "投資助言ではありません。特定ウォレットへの追随 (コピー取引) を推奨するものでも、"
    "売買シグナルでもありません。過去の数値は将来の成果を保証しません。"
    "数値は取得時点のスナップショットであり、監査を受けた財務数値ではありません。"
    "本資料の作成には AI を使用しています。"
)


def now_jst() -> datetime:
    return datetime.now(JST)


def fmt_usd(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "—"
    if v < 0:
        return f"-${abs(v):,.0f}"
    sign = "+" if (signed and v > 0) else ""
    return f"{sign}${v:,.0f}"


def fmt_ts(epoch: float | None) -> str:
    if not epoch:
        return "—"
    return datetime.fromtimestamp(epoch, JST).strftime("%Y-%m-%d %H:%M JST")


def median_interval_sec(timestamps: list[float]) -> float | None:
    """降順/昇順を問わず、連続取引の間隔 (秒) の中央値。"""
    if len(timestamps) < 2:
        return None
    ts = sorted(timestamps)
    diffs = [b - a for a, b in zip(ts, ts[1:])]
    return float(statistics.median(diffs))


# ---------------------------------------------------------------------------
# スナップショットの正規化
#   内部形式: {address: {name, value, positions[], closed_desc[], closed_asc[],
#                        activity[]}}
# ---------------------------------------------------------------------------
def normalize_d1(raw: dict, closed_asc: dict | None) -> dict:
    asc_map = (closed_asc or {}).get("closed_asc", {})
    out = {}
    for addr, w in raw.get("wallets", {}).items():
        value = None
        vr = w.get("value_raw") or []
        if vr and isinstance(vr[0], dict):
            value = vr[0].get("value")
        out[addr] = {
            "name": w.get("user") or addr[:10],
            "value": value,
            "positions": w.get("positions_raw") or [],
            "closed_desc": w.get("closed_raw") or [],
            "closed_asc": asc_map.get(addr) or [],
            "activity": w.get("activity_raw") or [],
        }
    return out


def normalize_snapshot(data: dict) -> dict:
    """--fetch が保存する形式 / D1 形式のどちらでも受け付ける。"""
    if "wallets" in data and data["wallets"]:
        first = next(iter(data["wallets"].values()))
        if isinstance(first, dict) and "positions" in first:
            return data["wallets"]  # 既に内部形式
        return normalize_d1(data, data.get("_closed_asc"))
    return data


def load_snapshot(path: Path, asc_path: Path | None = None) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    asc = None
    if asc_path and asc_path.exists():
        asc = json.loads(asc_path.read_text(encoding="utf-8"))
    if asc is not None and isinstance(data, dict) and "wallets" in data:
        first = next(iter(data["wallets"].values()), {})
        if isinstance(first, dict) and "positions_raw" in first:
            return normalize_d1(data, asc)
    return normalize_snapshot(data)


# ---------------------------------------------------------------------------
# 公開 API 取得 (--fetch)。読み取りのみ、429 で打ち切り。
# ---------------------------------------------------------------------------
def fetch_json(url: str, timeout: int) -> tuple[int, object | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "wallet-digest-prototype/0.1 (read-only research)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def fetch_snapshot(watchlist: list[dict], rules: dict, out_dir: Path) -> dict:
    cfg = rules["fetch"]
    base = cfg["base_url"]
    interval = float(cfg["interval_sec"])
    timeout = int(cfg["timeout_sec"])
    wallets: dict = {}
    calls = 0
    for entry in watchlist:
        addr = entry["address"]
        endpoints = {
            "value": f"{base}/value?user={addr}",
            "positions": f"{base}/positions?user={addr}&limit={cfg['positions_limit']}&sizeThreshold=0",
            "closed_desc": f"{base}/closed-positions?user={addr}&limit={cfg['closed_limit']}&sortBy=REALIZEDPNL&sortDirection=DESC",
            "closed_asc": f"{base}/closed-positions?user={addr}&limit={cfg['closed_limit']}&sortBy=REALIZEDPNL&sortDirection=ASC",
            "activity": f"{base}/activity?user={addr}&limit={cfg['activity_limit']}&type=TRADE",
        }
        got: dict = {"name": entry["name"], "value": None, "positions": [],
                     "closed_desc": [], "closed_asc": [], "activity": []}
        for key, url in endpoints.items():
            if calls:
                time.sleep(interval)
            status, payload = fetch_json(url, timeout)
            calls += 1
            print(f"[fetch] HTTP {status} {key} {entry['name']}")
            if status == 429:
                print("[fetch] 429 のため取得を打ち切ります (その日の取得はここまで)。")
                wallets[addr] = got
                _save_snapshot(wallets, out_dir)
                return wallets
            if status != 200 or payload is None:
                continue
            if key == "value":
                got["value"] = payload[0].get("value") if payload else None
            else:
                got[key] = payload if isinstance(payload, list) else []
        wallets[addr] = got
    _save_snapshot(wallets, out_dir)
    print(f"[fetch] 完了: {calls} 回呼び出し / ウォレット {len(wallets)} 件")
    return wallets


def _save_snapshot(wallets: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_jst().strftime("%Y%m%d-%H%M")
    path = out_dir / f"snapshot-{stamp}.json"
    path.write_text(json.dumps({"wallets": wallets}, ensure_ascii=False), encoding="utf-8")
    print(f"[fetch] スナップショット保存: {path}")


# ---------------------------------------------------------------------------
# 集計 + アラート判定
# ---------------------------------------------------------------------------
def summarize_wallet(addr: str, w: dict, entry: dict | None,
                     rules: dict, window_end: float | None) -> dict:
    a = rules["alerts"]
    positions = sorted(w["positions"], key=lambda p: p.get("currentValue") or 0,
                       reverse=True)
    value = w["value"]
    total_pos_value = sum(p.get("currentValue") or 0 for p in positions)
    denom = value if value else total_pos_value
    top = []
    for p in positions[:3]:
        cv = p.get("currentValue") or 0
        if cv <= 0:
            continue  # 価値 $0 の決済済み未償還ポジションは「保有上位」に入れない (D1 §3 の注意)
        top.append({
            "title": p.get("title") or "(無題)",
            "outcome": p.get("outcome") or "",
            "current_value": cv,
            "cash_pnl": p.get("cashPnl"),
            "share_pct": (cv / denom * 100) if denom else None,
        })
    top1_share = top[0]["share_pct"] if top else None

    activity = w["activity"]
    timestamps = [x.get("timestamp") for x in activity if x.get("timestamp")]
    med = median_interval_sec(timestamps)
    span_days = ((max(timestamps) - min(timestamps)) / 86400) if len(timestamps) >= 2 else 0.0
    last_ts = max(timestamps) if timestamps else None
    sweep_n = sum(1 for x in activity if (x.get("price") or 0) >= a["sweep_price_threshold"])
    sweep_share = (sweep_n / len(activity)) if activity else 0.0
    sides = {"BUY": 0, "SELL": 0}
    for x in activity:
        if x.get("side") in sides:
            sides[x["side"]] += 1
    markets = len({x.get("conditionId") for x in activity if x.get("conditionId")})

    closed_desc, closed_asc = w["closed_desc"], w["closed_asc"]
    best = max(closed_desc + closed_asc, key=lambda c: c.get("realizedPnl") or 0, default=None)
    worst = min(closed_desc + closed_asc, key=lambda c: c.get("realizedPnl") or 0, default=None)

    alerts: list[dict] = []
    base_value = (entry or {}).get("baseline_value_usd")
    base_med = (entry or {}).get("baseline_median_interval_sec")

    if value is not None and base_value:
        change = (value - base_value) / base_value * 100
        if abs(change) >= a["value_change_pct"]:
            alerts.append({"kind": "value_change", "level": "注意",
                           "text": f"ポートフォリオ価値がベースライン比 {change:+.1f}% 変動"})
    if med is not None and base_med:
        if med <= base_med * a["cadence_collapse_ratio"]:
            alerts.append({"kind": "cadence_collapse", "level": "注意",
                           "text": f"中央値間隔がベースライン {base_med:.0f}秒 → {med:.0f}秒 に急低下 (自動化導入の疑い)"})
    if med is not None and med < a["bot_like_median_interval_sec"]:
        alerts.append({"kind": "bot_like", "level": "注意",
                       "text": f"中央値間隔 {med:.0f}秒 < {a['bot_like_median_interval_sec']}秒 = ボット的一次フィルタに該当"})
    if window_end and last_ts and (window_end - last_ts) / 86400 >= a["dormant_days"]:
        alerts.append({"kind": "dormant", "level": "注意",
                       "text": f"最終取引から {(window_end - last_ts) / 86400:.1f} 日経過 (休止の疑い)"})
    if activity and sweep_share > a["sweep_share_threshold"]:
        alerts.append({"kind": "sweep", "level": "注意",
                       "text": f"価格 {a['sweep_price_threshold']} 以上の取引が {sweep_share * 100:.0f}% (決済直前掃討パターンの疑い)"})
    if top1_share is not None and top1_share >= a["concentration_top1_pct"]:
        alerts.append({"kind": "concentration", "level": "情報",
                       "text": f"最大ポジションがポートフォリオ価値の {top1_share:.0f}% を占める (単一見立てへの集中)"})

    return {
        "address": addr,
        "name": w["name"],
        "theme": (entry or {}).get("theme", ""),
        "baseline_verdict": (entry or {}).get("baseline_verdict", ""),
        "value_usd": value,
        "positions_count": len(positions),
        "top_positions": top,
        "top1_share_pct": top1_share,
        "closed_sample_count": len(closed_desc) + len(closed_asc),
        "closed_best": ({"title": best.get("title"), "realized_pnl": best.get("realizedPnl")} if best else None),
        "closed_worst": ({"title": worst.get("title"), "realized_pnl": worst.get("realizedPnl")} if worst else None),
        "activity_count": len(activity),
        "activity_span_days": round(span_days, 2),
        "median_interval_sec": med,
        "last_trade_jst": fmt_ts(last_ts),
        "last_trade_epoch": last_ts,
        "sweep_share_pct": round(sweep_share * 100, 1),
        "buy_count": sides["BUY"],
        "sell_count": sides["SELL"],
        "markets_count": markets,
        "alerts": alerts,
    }


# ---------------------------------------------------------------------------
# ダイジェスト描画 (Markdown)
# ---------------------------------------------------------------------------
def render_markdown(summaries: list[dict], meta: dict) -> str:
    lines = []
    lines.append("# ウォレット監視ダイジェスト (プロトタイプ生成)")
    lines.append("")
    lines.append(f"**生成日時:** {meta['generated_at_jst']}  ")
    lines.append(f"**データ:** {meta['data_source']}  ")
    lines.append(f"**対象:** ウォッチリスト {len(summaries)} ウォレット / **アラート合計:** {meta['alert_total']} 件")
    lines.append("")
    lines.append(f"> {DISCLAIMER}")
    lines.append("")
    lines.append("## 1. サマリー")
    lines.append("")
    lines.append("| ウォレット | 価値 | 保有件数 | 中央値間隔 | 最終取引 | 最大ポジション集中 | アラート |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in summaries:
        med = f"{s['median_interval_sec']:.0f}秒" if s["median_interval_sec"] is not None else "—"
        conc = f"{s['top1_share_pct']:.0f}%" if s["top1_share_pct"] is not None else "—"
        lines.append(
            f"| {s['name']} | {fmt_usd(s['value_usd'])} | {s['positions_count']} | {med} "
            f"| {s['last_trade_jst']} | {conc} | {len(s['alerts'])} |")
    lines.append("")
    lines.append("## 2. 乖離アラート (ベースライン比較)")
    lines.append("")
    any_alert = False
    for s in summaries:
        for al in s["alerts"]:
            any_alert = True
            lines.append(f"- **[{al['level']}] {s['name']}** ({al['kind']}): {al['text']}")
    if not any_alert:
        lines.append("- なし")
    lines.append("")
    lines.append("## 3. ウォレット別詳細")
    for s in summaries:
        lines.append("")
        lines.append(f"### {s['name']} — `{s['address']}`")
        lines.append("")
        lines.append(f"- テーマ: {s['theme'] or '—'} / ベースライン判定: {s['baseline_verdict'] or '—'}")
        lines.append(f"- ポートフォリオ価値: {fmt_usd(s['value_usd'])} / 保有ポジション: {s['positions_count']} 件")
        lines.append(f"- 直近アクティビティ: {s['activity_count']} 件 / {s['activity_span_days']} 日間 / "
                     f"対象 {s['markets_count']} 市場 / BUY {s['buy_count']} ・ SELL {s['sell_count']} / "
                     f"価格0.95以上 {s['sweep_share_pct']}%")
        if s["top_positions"]:
            lines.append("- 保有上位:")
            for p in s["top_positions"]:
                share = f" ({p['share_pct']:.0f}%)" if p["share_pct"] is not None else ""
                lines.append(f"  - {p['title']} [{p['outcome']}]: {fmt_usd(p['current_value'])}{share}"
                             f" / 含み {fmt_usd(p['cash_pnl'], signed=True)}")
        else:
            lines.append("- 保有上位: 価値のある保有ポジションなし "
                         "(残っているのは価値 $0 の決済済み未償還分のみ)")
        if s["closed_best"]:
            lines.append(f"- 決済サンプル最大の勝ち: {s['closed_best']['title']} "
                         f"{fmt_usd(s['closed_best']['realized_pnl'], signed=True)}")
        if s["closed_worst"]:
            lines.append(f"- 決済サンプル最大の負け: {s['closed_worst']['title']} "
                         f"{fmt_usd(s['closed_worst']['realized_pnl'], signed=True)}")
        lines.append(f"  (決済サンプルは取得上限つきの両裾サンプル {s['closed_sample_count']} 件。"
                     "口座全体の損益ではない)")
    lines.append("")
    lines.append("## 4. 方法と限界")
    lines.append("")
    lines.append("- データソースは Polymarket 公式 data-api の公開エンドポイントのみ "
                 "(/value, /positions, /closed-positions, /activity)。認証不要・読み取り専用。")
    lines.append("- 中央値間隔は直近アクティビティ (最大500件) 内の取引間隔の中央値。"
                 "500件が数時間に収まる口座と数十日に広がる口座では意味が異なるため、対象期間を併記した。")
    lines.append("- 決済済みポジションは方向別サンプルのため、勝率の算出には使えない。")
    lines.append("- 保有ポジションはエンドポイント上限 (500件) に達した場合、件数は下限表示になる。")
    lines.append("- アラートは事実の変化の通知であり、売買の推奨ではない。"
                 "「真似しやすさ」と「収益性」は無関係である (D1 サンプルの結論)。")
    lines.append("")
    lines.append("*— ダイジェスト ここまで —*")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 自己検証 (--demo 時に実行し、失敗したら終了コード 1)
# ---------------------------------------------------------------------------
def self_check(summaries: list[dict]) -> list[tuple[str, bool]]:
    by_name = {s["name"]: s for s in summaries}
    checks: list[tuple[str, bool]] = []
    checks.append(("ウォレット 7 件を処理した", len(summaries) == 7))
    checks.append(("全ウォレットに価値または保有データがある",
                   all(s["value_usd"] is not None or s["positions_count"] for s in summaries)))
    tok = by_name.get("Tokor", {})
    checks.append(("Tokor: 中央値間隔 151 秒 (D1 実測と一致)",
                   tok.get("median_interval_sec") == 151))
    checks.append(("Tokor: 最大ポジション集中アラートが出る (約55%)",
                   any(a["kind"] == "concentration" for a in tok.get("alerts", []))))
    coin = by_name.get("CoinTrick", {})
    checks.append(("CoinTrick: 掃討アラート + ボット的アラートが出る",
                   any(a["kind"] == "sweep" for a in coin.get("alerts", []))
                   and any(a["kind"] == "bot_like" for a in coin.get("alerts", []))))
    wr = by_name.get("wr0ngw4yb3tt0r", {})
    checks.append(("wr0ngw4yb3tt0r: 休止アラートが出る (最終取引 2026-09-30)",
                   any(a["kind"] == "dormant" for a in wr.get("alerts", []))))
    checks.append(("全ウォレットで決済サンプルの両裾 (勝ち/負け) が取れている",
                   all(s["closed_best"] and s["closed_worst"] for s in summaries)))
    return checks


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="ウォレット監視ダイジェスト (Rank 6 プロトタイプ)")
    parser.add_argument("--demo", action="store_true",
                        help="D1 キャッシュデータで生成 + 自己検証 (ネットワーク不使用)")
    parser.add_argument("--fetch", action="store_true",
                        help="公開 data-api から新規取得して生成 (読み取りのみ)")
    parser.add_argument("--input", help="スナップショット JSON のパス")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                        help="--demo 用キャッシュの置き場所")
    parser.add_argument("--watchlist", default=str(BASE_DIR / "watchlist.json"))
    parser.add_argument("--rules", default=str(BASE_DIR / "rules.json"))
    parser.add_argument("--out-dir", default=None, help="出力先 (既定: --demo 時 demo_output/、他は output/)")
    args = parser.parse_args()

    watchlist_doc = json.loads(Path(args.watchlist).read_text(encoding="utf-8"))
    watchlist = watchlist_doc["wallets"]
    entries = {e["address"]: e for e in watchlist}
    rules = json.loads(Path(args.rules).read_text(encoding="utf-8"))

    if args.demo:
        cache = Path(args.cache_dir)
        wallets = load_snapshot(cache / "raw.json", cache / "closed_asc.json")
        source = (f"D1 キャッシュ (hidden_files/d1_raw/raw.json + closed_asc.json、"
                  f"取得 2026-10-24 05:40-05:43 JST)。ネットワーク不使用")
        out_dir = Path(args.out_dir) if args.out_dir else BASE_DIR / "demo_output"
    elif args.fetch:
        wallets = fetch_snapshot(watchlist, rules, BASE_DIR / "snapshots")
        source = "Polymarket data-api から今回取得 (公開・認証不要、読み取りのみ)"
        out_dir = Path(args.out_dir) if args.out_dir else BASE_DIR / "output"
    elif args.input:
        wallets = load_snapshot(Path(args.input))
        source = f"スナップショット JSON: {args.input}"
        out_dir = Path(args.out_dir) if args.out_dir else BASE_DIR / "output"
    else:
        print("使い方: --demo / --fetch / --input <snapshot.json> のいずれかを指定してください。")
        return 2

    if not wallets:
        print("[error] スナップショットが空です。")
        return 1

    window_end = None
    for w in wallets.values():
        for x in w["activity"]:
            if x.get("timestamp"):
                window_end = max(window_end or 0, x["timestamp"])

    summaries = [summarize_wallet(addr, wallets[addr], entries.get(addr), rules, window_end)
                 for addr in [e["address"] for e in watchlist if e["address"] in wallets]]
    for addr, w in wallets.items():  # ウォッチリスト外が混ざっていた場合も落とさない
        if addr not in entries:
            summaries.append(summarize_wallet(addr, w, None, rules, window_end))

    meta = {
        "generated_at_jst": now_jst().strftime("%Y-%m-%d %H:%M JST"),
        "data_source": source,
        "alert_total": sum(len(s["alerts"]) for s in summaries),
        "data_window_end_jst": fmt_ts(window_end),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_markdown(summaries, meta)
    (out_dir / "digest.md").write_text(md, encoding="utf-8")
    (out_dir / "digest.json").write_text(
        json.dumps({"meta": meta, "disclaimer": DISCLAIMER, "wallets": summaries},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {out_dir / 'digest.md'}")
    print(f"[out] {out_dir / 'digest.json'}")
    print(f"[info] ウォレット {len(summaries)} 件 / アラート {meta['alert_total']} 件")

    if args.demo:
        print("\n=== 自己検証 (--demo) ===")
        checks = self_check(summaries)
        all_ok = True
        for label, ok in checks:
            print(f"  [{'OK' if ok else 'NG'}] {label}")
            all_ok = all_ok and ok
        print(f"総合: {'ALL OK' if all_ok else '一部 NG'}")
        return 0 if all_ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
