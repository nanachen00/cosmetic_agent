#!/usr/bin/env python3
"""Apply the second professional audit for the 49 remaining catalog candidates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.services import product_review_service as review


APPROVED = {
    "crawl-66290143d7a961cae5e6": "DR.WU 玻尿酸保濕精華化妝水（經典型）可由官方品名唯一識別。",
    "crawl-8d6e3a82cb0284978800": "HR PX50 極塑黑繃帶為可獨立識別的新款乳霜。",
    "crawl-2ceb1b4f00f312724dd1": "IPSA 極境新生修護精華露為官方精華水／化妝水商品。",
    "crawl-b611ecf8f3c64a28430b": "REVITAL 多胺無痕保濕露可唯一識別。",
    "crawl-37283e68f8e51b792b9d": "AKIMIA 藍銅胜肽微電流面膜可唯一識別。",
    "crawl-cfc3c2c9a90fe5e09c26": "CLARINS 煥顏緊緻膠原日霜可唯一識別。",
    "crawl-538a18602444fde670a1": "ReVive 銀采抗皺超導面膜可唯一識別。",
    "crawl-64c7acb3d52cb26172ea": "SHISEIDO 激抗痕亮采緊緻抗皺精華乳 A+ 可唯一識別。",
    "crawl-9a6d6df9a59e3a4bb7bf": "SISLEY 抗皺活膚御緻永恆活源精粹可唯一識別。",
    "crawl-a11433beebe2c73d408d": "資生堂激抗痕亮采緊緻 ACE 眼霜可唯一識別。",
    "crawl-7c4ebc68000f642e8f99": "Kiehl's 金盞花植物精華潔面泡泡凝露 230ml 可唯一識別。",
    "crawl-ff40778494350d2748ea": "Mentholatum Acnes 集中抗痘精華液 13ml 可唯一識別。",
    "crawl-42e7e0e31ae24b5c6c64": "CHANEL 初生光采淨亮精華露可唯一識別。",
    "crawl-1ed0d0626c2d304ad2e2": "CHANEL 奢華極萃金煥顏霜可唯一識別。",
    "crawl-bed23bd8408b03f6ad9c": "DECORTE 瑩潤粹白擊黑瞬采面膜可唯一識別。",
    "crawl-8b116484adafa4455bf5": "ELEMIS 海洋膠原超時空逆時賦活霜可唯一識別。",
    "crawl-bcd2bf68c8bdc5961a83": "GUERLAIN 皇家蜂王乳雙導眼部精華可唯一識別。",
    "crawl-554da01f63ff8c97dae3": "LA MER 超能平衡精華露可唯一識別。",
    "crawl-6cdb0c9f615f1d86de4a": "LA PRAIRIE 鑽白魚子時空亮顏精萃可唯一識別。",
    "crawl-90b1c87522e3e5308dcd": "LANCOME 超極限肌因精華露是精華型化妝水，與小黑瓶不同。",
    "crawl-e84e8da87d292927c207": "LANCOME 超極限肌因賦活露是小黑瓶 PRO 精華，與精華水不同。",
    "crawl-fd44f0c757e77929b1b2": "POLA B.A 新生奇蹟精華水可唯一識別。",
    "crawl-7181b2843632108bdf23": "SK-II LXP 凝時金緻乳霜可唯一識別。",
    "crawl-9db6a7e9e79bc841ed73": "YSL 金緻奢華極萃精華油可唯一識別。",
    "crawl-5f9f915b614fe5d4e6a1": "LANCOME 超極光淨亮淡斑激萃可唯一識別。",
}

NEEDS_ENRICHMENT = {
    "crawl-4ab464508aa8ab491f1c": "安得利清爽極效夏卡與 MELA 淡斑夏卡是不同 SKU；候選泛稱需回查原始 URL 或包裝後才能唯一對應。",
    "crawl-673002a4412da3406fc8": "中文寫保濕乳液，但原文是 Moisturizing Cream 454g；需先校正正式中文名與品類。",
    "crawl-4cc3d2841e03968f90be": "中文露萃修護液與原文 Cicaplast Baume B5 身分衝突；需先核 SKU 與來源。",
    "crawl-63acfef61b7bf56ca478": "BHR 中文名與 HHR 英文名衝突；需先修正原文名稱。",
    "crawl-d806669c39e4f3fcf86a": "航空版與容量資訊混入商品名；需還原亮采化妝水主商品並查重。",
    "crawl-6915d383ea34e8e663e1": "特大裝規格混入主商品名；需正規化 TIRTIR 柔膚水名稱與規格。",
    "crawl-15cf4a15a51a44775faf": "激光精華彈潤美白乳液被誤分為精華液；需先改為乳液類。",
    "crawl-00cf0e8859dcb86787d7": "POLA 防曬需補齊正式 SPF 品名、官方來源並與規格拆分。",
    "crawl-a6d77aac427abf9c0d28": "資生堂日中防護霜名稱過泛；需補系列、正式英文名與 SPF。",
    "crawl-8013a535be78fbc5d266": "SUQQU 限定防曬噴霧同時供臉、身體與頭髮使用；需標示 multi-use 與限定香型。",
}

DUPLICATES = {
    "crawl-70b4eee33469c8bda4cb": "decorte-liposome-advanced-repair-serum",
    "crawl-69f54dabf4e4be69592d": "cerave-retinol-renewing-serum",
    "crawl-eed042e3860e577d0b95": "cerave-peptide-repair-cream",
    "crawl-085d2d8825414a3998a2": "estee-lauder-advanced-night-repair",
    "crawl-787b24094b2a88f22025": "vt-pdrn-essence-100",
    "crawl-415df6174ab877d04c33": "anua-azelaic-acid-10-serum",
    "crawl-450b4e142c7634a06a08": "skin1004-centella-ampoule-foam",
    "crawl-62f732e020f916d0760e": "kiehls-calendula-herbal-toner-500",
    "crawl-975d1f7f46a74b26c6ba": "origins-mega-mushroom-relief-resilience-lotion",
    "crawl-3749426c8e26504856a8": "shiseido-bio-performance-advanced-super-revitalizing-cream",
    "crawl-237ae99e00da39c04f9e": "mixsoon-bean-essence",
    "crawl-66f45b410d9c5f0cfdec": "crawl-00cf0e8859dcb86787d7",
    "crawl-7798dfde45139cbc99ba": "skin1004-hyalu-cica-water-fit-sun-serum",
}

EXCLUDED = {
    "crawl-707c9b391af59ff9da08": "DHC 純橄玫瑰香氛是口服香氛保健品，不是外用臉部保養。",
}


def apply(key: str, status: str, note: str, duplicate_of: str | None = None) -> dict:
    try:
        result = review.update_product_review(
            key,
            review_status=status,
            reviewer_notes=f"專業核對：{note}",
            duplicate_of=duplicate_of,
            reviewer_id_hash="codex-professional-review-20260927-batch2",
        )
        return {"key": key, "status": status, "ok": True, "result": result}
    except Exception as exc:
        return {"key": key, "status": status, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    rows: list[dict] = []
    for key, note in APPROVED.items():
        rows.append(apply(key, "approved", note + " 缺完整 INCI 時只進商品目錄，品質閘門維持不可推薦。"))
    for key, note in NEEDS_ENRICHMENT.items():
        rows.append(apply(key, "needs_enrichment", note))
    for key, target in DUPLICATES.items():
        rows.append(apply(key, "duplicate", f"與 {target} 為同一主商品或規格／套組變體，不重複建節點。", target))
    for key, note in EXCLUDED.items():
        rows.append(apply(key, "excluded", note))

    output = Path("data/product_review/batch_review_remaining_20260927.jsonl")
    with output.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "type": "batch_start"}, ensure_ascii=False) + "\n")
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    counts: dict[str, int] = {}
    failures = []
    for row in rows:
        label = f"{row['status']}_{'ok' if row['ok'] else 'failed'}"
        counts[label] = counts.get(label, 0) + 1
        if not row["ok"]:
            failures.append({"key": row["key"], "error": row["error"]})
    print(json.dumps({"total": len(rows), "counts": counts, "failures": failures, "audit_log": str(output)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
