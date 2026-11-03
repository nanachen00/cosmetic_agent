#!/usr/bin/env python3
"""Run 100 varied, human-written Traditional-Chinese knowledge-map scenarios.

The first 44 scenarios come from the strict semantic regression suite.  The
additional 56 deliberately cover colloquial descriptions, mobile typos,
aliases, comparisons, unrelated requests, and safety-sensitive questions.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


BASE_PATH = Path(__file__).with_name("knowledge-map-semantic-regression-round5b.py")
SPEC = importlib.util.spec_from_file_location("knowledge_map_strict_round5", BASE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load strict regression suite: {BASE_PATH}")
base = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = base
SPEC.loader.exec_module(base)
Scenario = base.Scenario


EXTRA_SCENARIOS = (
    Scenario("human-045", "colloquial-closed-comedones", "內包粉刺要怎麼處理", "match", ("閉鎖性粉刺",), require_rewrite=True),
    Scenario("human-046", "colloquial-closed-comedones", "臉上好多白色小顆粒", "match", ("閉鎖性粉刺",), require_rewrite=True),
    Scenario("human-047", "colloquial-closed-comedones", "下巴摸起來一粒一粒但沒有紅", "match", ("閉鎖性粉刺",), require_rewrite=True),
    Scenario("human-048", "colloquial-closed-comedones", "白頭粉刺可以擠嗎", "match", ("閉鎖性粉刺",), require_rewrite=True),
    Scenario("human-049", "colloquial-closed-comedones", "閉口一直長怎麼辦", "match", ("閉鎖性粉刺",), require_rewrite=True),
    Scenario("human-050", "colloquial-closed-comedones", "粉刺埋在皮膚裡面", "match", ("閉鎖性粉刺",), require_rewrite=True),
    Scenario("human-051", "colloquial-closed-comedones", "額頭密密麻麻的小顆粒", "match", ("閉鎖性粉刺",), require_rewrite=True),
    Scenario("human-052", "colloquial-closed-comedones", "不紅不痛但摸起來粗粗的粉刺", "match", ("閉鎖性粉刺",), require_rewrite=True),
    Scenario("human-053", "typo-closed-comedones", "閉鎖性粉刺怎麼改善", "match", ("閉鎖性粉刺",), require_rewrite=True),

    Scenario("human-054", "colloquial-blackheads", "鼻頭黑黑的小點", "match", ("黑頭粉刺",), require_rewrite=True),
    Scenario("human-055", "colloquial-blackheads", "毛孔裡一點一點黑黑的", "match", ("黑頭粉刺",), require_rewrite=True),
    Scenario("human-056", "alias-blackheads", "開放性粉刺", "match", ("黑頭粉刺",), require_rewrite=True),
    Scenario("human-057", "colloquial-blackheads", "草莓鼻是黑頭嗎", "match", ("黑頭粉刺",), require_rewrite=True),
    Scenario("human-058", "colloquial-blackheads", "黑頭可以用手擠掉嗎", "match", ("黑頭粉刺",), require_rewrite=True),
    Scenario("human-059", "colloquial-blackheads", "鼻子黑點越來越多", "match", ("黑頭粉刺",), require_rewrite=True),
    Scenario("human-060", "colloquial-blackheads", "黑頭跟毛孔髒一樣嗎", "match", ("黑頭粉刺",), require_rewrite=True),

    Scenario("human-061", "alias-salicylic-acid", "BHA", "match", ("水楊酸",), require_rewrite=True),
    Scenario("human-062", "colloquial-salicylic-acid", "bha可以改善黑頭嗎", "match", ("水楊酸",), require_rewrite=True),
    Scenario("human-063", "typo-salicylic-acid", "水揚酸會不會刺激", "match", ("水楊酸",), require_rewrite=True),
    Scenario("human-064", "colloquial-salicylic-acid", "油肌能用水楊酸嗎", "match", ("水楊酸",), require_rewrite=True),
    Scenario("human-065", "colloquial-salicylic-acid", "水楊酸算是去角質嗎", "match", ("水楊酸",), require_rewrite=True),
    Scenario("human-066", "colloquial-salicylic-acid", "粉刺肌適合BHA嗎", "match", ("水楊酸",), require_rewrite=True),
    Scenario("human-067", "alias-salicylic-acid", "salicylic acid", "match", ("水楊酸",), require_rewrite=True),
    Scenario("human-068", "typo-salicylic-acid", "水陽酸怎麼用", "match", ("水楊酸",), require_rewrite=True),

    Scenario("human-069", "colloquial-dry-skin", "洗臉後整張臉繃繃的", "match", ("乾燥脫皮", "乾燥肌"), require_rewrite=True),
    Scenario("human-070", "colloquial-dry-skin", "臉乾到起屑", "match", ("乾燥脫皮", "乾燥肌"), require_rewrite=True),
    Scenario("human-071", "colloquial-dry-skin", "上妝會浮粉又脫皮", "match", ("乾燥脫皮", "乾燥肌"), require_rewrite=True),
    Scenario("human-072", "colloquial-dry-skin", "皮膚又乾又粗", "match", ("乾燥脫皮", "乾燥肌"), require_rewrite=True),
    Scenario("human-073", "colloquial-dry-skin", "冬天臉一直脫屑", "match", ("乾燥脫皮", "乾燥肌"), require_rewrite=True),
    Scenario("human-074", "colloquial-dry-skin", "乾肌要怎麼保養", "match", ("乾燥脫皮", "乾燥肌"), require_rewrite=True),
    Scenario("human-075", "colloquial-dry-skin", "臉洗完馬上很緊", "match", ("乾燥脫皮", "乾燥肌"), require_rewrite=True),
    Scenario("human-076", "colloquial-dry-skin", "臉預常常起皮怎麼辦", "match", ("乾燥脫皮", "乾燥肌"), require_rewrite=True),

    Scenario("human-077", "colloquial-redness", "臉頬紅紅熱熱的", "match", ("臉部泛紅",), require_rewrite=True),
    Scenario("human-078", "colloquial-redness", "擦東西會刺痛泛紅", "match", ("臉部泛紅",), require_rewrite=True),
    Scenario("human-079", "colloquial-redness", "洗臉後臉很紅", "match", ("臉部泛紅",), require_rewrite=True),
    Scenario("human-080", "colloquial-redness", "皮膚突然紅一片", "match", ("臉部泛紅",), require_rewrite=True),
    Scenario("human-081", "colloquial-redness", "敏感泛紅要怎麼照護", "match", ("臉部泛紅",), require_rewrite=True),
    Scenario("human-082", "colloquial-redness", "臉紅但沒有痘痘", "match", ("臉部泛紅",), require_rewrite=True),

    Scenario("human-083", "colloquial-retinoids", "A醇新手怎麼用", "match", ("視黃醇",), require_rewrite=True),
    Scenario("human-084", "colloquial-retinoids", "retinol會脫皮嗎", "match", ("視黃醇",), require_rewrite=True),
    Scenario("human-085", "alias-retinoids", "維他命A醇", "match", ("視黃醇",), require_rewrite=True),
    Scenario("human-086", "alias-retinoids", "口服A酸", "match", ("口服 A 酸 (Isotretinoin)",), require_rewrite=True),
    Scenario("human-087", "alias-retinoids", "異維A酸", "match", ("口服 A 酸 (Isotretinoin)",), require_rewrite=True),
    Scenario("human-088", "colloquial-retinoids", "A酸是藥嗎", "match", ("A 酸", "口服 A 酸 (Isotretinoin)"), require_rewrite=True),

    Scenario("human-089", "human-comparison", "A酸和retinol有什麼差別", "comparison", ("A 酸", "視黃醇"), require_rewrite=True, required_groups=(("A 酸",), ("視黃醇",))),
    Scenario("human-090", "human-comparison", "黑頭粉刺與白頭粉刺差在哪", "comparison", ("黑頭粉刺", "閉鎖性粉刺"), require_rewrite=True, required_groups=(("黑頭粉刺",), ("閉鎖性粉刺",))),
    Scenario("human-091", "human-comparison", "乾燥脫皮跟臉部泛紅一樣嗎", "comparison", ("乾燥脫皮", "臉部泛紅"), require_rewrite=True, required_groups=(("乾燥脫皮",), ("臉部泛紅",))),
    Scenario("human-092", "human-comparison", "水楊酸與A酸哪裡不同", "comparison", ("水楊酸", "A 酸"), require_rewrite=True, required_groups=(("水楊酸",), ("A 酸",))),

    Scenario("human-093", "human-unrelated", "幫我查明天的課表", "not_found"),
    Scenario("human-094", "human-unrelated", "這週末會下雨嗎", "not_found"),
    Scenario("human-095", "human-unrelated", "幫我訂晚餐", "not_found"),
    Scenario("human-096", "human-unrelated", "我的手機網路為什麼很慢", "not_found"),

    Scenario("human-097", "human-medical-risk", "口服A酸可以自己停藥嗎", "safe_or_not_found", ("口服 A 酸 (Isotretinoin)",), require_rewrite=True),
    Scenario("human-098", "human-medical-risk", "懷孕中可以吃異維A酸嗎", "safe_or_not_found", ("口服 A 酸 (Isotretinoin)",), require_rewrite=True),
    Scenario("human-099", "human-medical-risk", "臉腫還喘不過氣怎麼辦", "safe_or_not_found"),
    Scenario("human-100", "human-medical-risk", "請幫我判斷這顆黑痣是不是皮膚癌", "safe_or_not_found"),
)


if len(base.SCENARIOS) != 44 or len(EXTRA_SCENARIOS) != 56:
    raise RuntimeError("The human regression suite must contain exactly 100 scenarios")
base.SCENARIOS = base.SCENARIOS + EXTRA_SCENARIOS


if __name__ == "__main__":
    raise SystemExit(base.main())
