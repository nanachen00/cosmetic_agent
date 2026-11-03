"""Enrich the 16 trend-evidenced mask products with verified source data.

The migration is deliberately review-safe: every touched product remains
recommendable=false and quality_review_required=true. Ambiguous identities are
also made inactive. Run without --apply for a read-only preflight.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from neo4j import GraphDatabase


FULL = {
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10582": {
        "official_name": "Torriden DIVE-IN 5D 微分子玻尿酸保濕面膜",
        "description": "以多重玻尿酸、泛醇與尿囊素提供補水與舒緩；適合乾燥、缺水與一般膚況。",
        "ingredients_list": ["Water", "Butylene Glycol", "Glycerin", "Dipropylene Glycol", "Sodium Hyaluronate Crosspolymer", "Sodium Hyaluronate", "Sodium Acetylated Hyaluronate", "Hydrolyzed Sodium Hyaluronate", "Hydrolyzed Hyaluronic Acid", "Panthenol", "Trehalose", "Allantoin", "Chamomilla Recutita Flower Extract", "Calendula Officinalis Flower Extract", "Melia Azadirachta Leaf Extract", "Melia Azadirachta Flower Extract", "Coccinia Indica Fruit Extract", "Solanum Melongena Fruit Extract", "Ocimum Sanctum Leaf Extract", "Curcuma Longa Root Extract", "Corallina Officinalis Extract", "Glyceryl Acrylate/Acrylic Acid Copolymer", "PVM/MA Copolymer", "Saccharide Isomerate", "Carbomer", "Xanthan Gum", "Tromethamine", "Pentylene Glycol", "1,2-Hexanediol", "Hydroxyacetophenone", "Portulaca Oleracea Extract", "Malachite Extract", "Ethylhexylglycerin"],
        "key_ingredients": ["5D 多重玻尿酸", "泛醇", "尿囊素", "海藻糖"],
        "concerns": ["乾燥", "缺水", "敏弱舒緩"],
        "functional_categories": ["保濕", "舒緩", "屏障照護"],
        "source_url": "https://torriden.us/products/dive-in-mask",
        "source_urls": ["https://torriden.us/products/dive-in-mask", "https://torriden.tw/collections/torriden-dive-in", "https://www.watsons.com.tw/torriden-%E6%A1%83%E7%91%9E%E4%B8%B9-5d%E5%BE%AE%E5%88%86%E5%AD%90%E7%8E%BB%E5%B0%BF%E9%85%B8%E4%BF%9D%E6%BF%95%E9%9D%A2%E8%86%9C-10%E5%85%A5-%E7%AE%B1%E8%B3%BC/p/BP_331310"],
        "price": 699.0, "sale_price": 699.0, "list_price": 990.0, "currency": "TWD", "price_status": "observed_current", "price_source_url": "https://www.watsons.com.tw/torriden-%E6%A1%83%E7%91%9E%E4%B8%B9-5d%E5%BE%AE%E5%88%86%E5%AD%90%E7%8E%BB%E5%B0%BF%E9%85%B8%E4%BF%9D%E6%BF%95%E9%9D%A2%E8%86%9C-10%E5%85%A5-%E7%AE%B1%E8%B3%BC/p/BP_331310",
    },
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10586": {
        "official_name": "我的美麗日記 活能抗老淨亮面膜",
        "description": "含菸鹼醯胺、多種維他命 C 衍生物與發酵來源成分，定位為淨亮、保濕與彈潤保養。",
        "ingredients_list": ["Water", "Propanediol", "Niacinamide", "Glycerin", "Methyl Gluceth-20", "Hydroxyacetophenone", "Polyglycerin-3", "Urea", "Sphingomonas Ferment Extract", "Caprylic/Capric Triglyceride", "Phenoxyethanol", "Sorbeth-30 Tetraoleate", "Xanthan Gum", "Disodium EDTA", "PEG-40 Hydrogenated Castor Oil", "Fragrance", "3-O-Ethyl Ascorbic Acid", "Acetyl Glycyl Beta-Alanine", "Ascorbyl Glucoside", "Ascorbyl Tetraisopalmitate", "Nicotinamide Mononucleotide", "Diglucosyl Gallic Acid"],
        "key_ingredients": ["菸鹼醯胺", "3-O-乙基抗壞血酸", "抗壞血酸葡糖苷", "四異棕櫚酸抗壞血酸酯", "NMN"],
        "concerns": ["暗沉", "乾燥", "彈性不足"],
        "functional_categories": ["淨亮", "保濕", "彈潤", "抗老外觀保養"],
        "source_url": "https://www.beautydiary.com.tw/product_detail/102",
        "source_urls": ["https://www.beautydiary.com.tw/product_detail/102", "https://www.beautydiary.com.tw/news_detail/59", "https://711go.7-11.com.tw/product/260718", "https://www.momoshop.com.tw/product/13968009?sourcePageType=4"],
        "price": 219.0, "sale_price": 219.0, "list_price": 250.0, "currency": "TWD", "price_status": "observed_current", "price_source_url": "https://www.momoshop.com.tw/product/13968009?sourcePageType=4",
    },
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10614": {
        "official_name": "我的美麗日記 蜜若藍超能補水面膜",
        "description": "以保濕劑、玻尿酸、聚麩胺酸、神經醯胺與植物油脂提供補水與屏障照護。",
        "ingredients_list": ["Water", "Glycerin", "Propylene Glycol", "Butylene Glycol", "Phenoxyethanol", "Chlorphenesin", "Carbomer", "Ammonium Acryloyldimethyltaurate/VP Copolymer", "Polysorbate 20", "Aloe Barbadensis Leaf Juice", "Sodium Hyaluronate", "Xanthan Gum", "Sodium Polyglutamate", "Sodium Lactate", "Sodium PCA", "Hydroxyethyl Urea", "Limnanthes Alba Seed Oil", "Triethanolamine", "Dipotassium Glycyrrhizate", "Iodopropynyl Butylcarbamate", "Potassium Hydroxide", "Fragrance", "Arnica Montana Flower Extract", "Citric Acid", "Camellia Oleifera Seed Oil", "Citrus Aurantium Amara Flower Oil", "Panthenol", "Rubus Chamaemorus Seed Oil", "Hydrolyzed Yeast Extract", "Propanediol", "Xylitylglucoside", "Anhydroxylitol", "Potassium Carbonate", "Xylitol", "Sodium Lauroyl Lactylate", "Glucose", "Myrothamnus Flabellifolia Leaf/Stem Extract", "Cetyl Hydroxyethylcellulose", "Ceramide NP", "Polyglucuronic Acid", "Ceramide AP", "Phytosphingosine", "Ascorbic Acid", "Cholesterol", "Ethylhexylglycerin", "Potassium Sorbate", "Sodium Benzoate", "Lecithin", "Ceramide EOP"],
        "key_ingredients": ["玻尿酸鈉", "聚麩胺酸鈉", "神經醯胺", "泛醇", "蘆薈"],
        "concerns": ["乾燥", "缺水", "屏障照護"],
        "functional_categories": ["保濕", "滋潤", "屏障照護"],
        "source_url": "https://www.beautydiary.com.tw/product_detail/82",
        "source_urls": ["https://www.beautydiary.com.tw/product_detail/82", "https://www.beautydiary.com.tw/201908event/", "https://www.yesstyle.com/zh_TW/my-beauty-diary-%E6%88%91%E7%9A%84%E7%BE%8E%E9%BA%97%E6%97%A5%E8%A8%98/info.html/pid.1104102471", "https://www.momoshop.com.tw/product/6944881"],
        "price": 199.0, "sale_price": 199.0, "list_price": 250.0, "currency": "TWD", "price_status": "observed_current", "price_source_url": "https://www.momoshop.com.tw/product/6944881",
    },
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10631": {
        "official_name": "BIODANCE Bio-Collagen Real Deep Mask",
        "description": "水凝膠面膜，官方定位為補水、彈潤、毛孔外觀與肌膚光澤照護。",
        "ingredients_list": ["Water", "Glycerin", "Acrylates Copolymer", "Niacinamide", "Ceratonia Siliqua Gum", "Chondrus Crispus Extract", "Dipropylene Glycol", "Betaine", "Algin", "Agar", "Hydroxyacetophenone", "1,2-Hexanediol", "Potassium Chloride", "Caprylyl Glycol", "Sucrose", "Allantoin", "Polyglyceryl-10 Laurate", "Ethylhexylglycerin", "Galactomyces Ferment Filtrate", "Adenosine", "Disodium EDTA", "Pyrus Communis Fruit Extract", "Rosa Damascena Flower Water", "Collagen Extract", "Iris Florentina Root Extract", "Cucumis Melo Fruit Extract", "Hedera Helix Leaf/Stem Extract", "Butylene Glycol", "Dipotassium Glycyrrhizate", "Hydrolyzed Hyaluronic Acid", "Lactobacillus Ferment", "Bifida Ferment Filtrate", "Lactobacillus Ferment Lysate", "Tocopherol"],
        "key_ingredients": ["膠原萃取", "低分子玻尿酸", "菸鹼醯胺", "腺苷", "發酵來源成分"],
        "concerns": ["乾燥", "彈性不足", "毛孔外觀", "暗沉"],
        "functional_categories": ["保濕", "彈潤", "毛孔外觀", "淨亮", "屏障照護"],
        "source_url": "https://biodance.com/products/biodance-bio-collagen-real-deep-mask",
        "source_urls": ["https://biodance.com/products/biodance-bio-collagen-real-deep-mask"],
        "price_status": "foreign_currency_not_comparable",
    },
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10585": {
        "official_name": "LANEIGE 超能亮睡美人晚安面膜 70ml",
        "description": "免沖洗晚安面膜，定位為夜間補水、柔嫩與淨亮外觀保養；含香料。",
        "ingredients_list": ["Water", "Butylene Glycol", "Glycerin", "Trehalose", "Methyl Trimethicone", "1,2-Hexanediol", "Squalane", "Phenyl Trimethicone", "PCA Dimethicone", "Caprylyl Methicone", "Ammonium Acryloyldimethyltaurate/VP Copolymer", "Carbomer", "Tromethamine", "Lactobacillus Ferment Lysate", "Niacinamide", "Glyceryl Caprylate", "Acrylates/C10-30 Alkyl Acrylate Crosspolymer", "Propanediol", "Ethylhexylglycerin", "Malachite Extract", "Xylitylglucoside", "Propylene Glycol", "Anhydroxylitol", "Fragrance", "Sodium Hyaluronate", "Sodium Metaphosphate", "Xylitol", "Raffinose", "Oenothera Biennis Oil", "Hydroxypropyltrimonium Hyaluronate", "Tryptophan", "Lactobacillus Ferment", "Beta-Glucan", "Limonene", "Linalool", "Acrylates/Stearyl Methacrylate Copolymer", "Tremella Fuciformis Sporocarp Extract", "Hyaluronic Acid", "Sodium Trimetaphosphate", "Xanthan Gum", "Polyglutamic Acid", "Ceramide NP", "Tocopherol"],
        "key_ingredients": ["角鯊烷", "菸鹼醯胺", "玻尿酸", "β-葡聚糖", "神經醯胺 NP"],
        "concerns": ["乾燥", "缺水", "粗糙", "暗沉"],
        "functional_categories": ["保濕", "柔嫩", "淨亮", "舒緩"],
        "source_url": "https://tw.laneige.com/water-sleeping-mask-70ml-25.html",
        "source_urls": ["https://tw.laneige.com/water-sleeping-mask-70ml-25.html"],
        "price": 900.0, "sale_price": 900.0, "list_price": 1000.0, "currency": "TWD", "price_status": "official_observed", "price_source_url": "https://tw.laneige.com/water-sleeping-mask-70ml-25.html",
    },
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10651": {
        "official_name": "AKIMIA 藍銅胜肽微電流面膜",
        "description": "含藍銅胜肽與六胜肽，搭配面膜材質的微電流設計；官方提醒開放性傷口避免使用。",
        "ingredients_list": ["Aqua", "1,3-Butylene Glycol", "Glycereth-26", "Propylene Glycol", "Glyceryl Glucoside", "Urea", "Hibiscus Esculentus Fruit Extract", "Artemisia Capillaris Flower Extract", "Ammonium Polyacryloyldimethyl Taurate", "Xanthan Gum", "Sodium Hyaluronate", "Copper Tripeptide-1", "Acetyl Hexapeptide-8", "Phenoxyethanol", "Chlorphenesin", "Sodium Benzoate"],
        "key_ingredients": ["Copper Tripeptide-1", "Acetyl Hexapeptide-8", "玻尿酸鈉", "秋葵果萃取"],
        "concerns": ["彈性不足", "乾燥", "細紋外觀"],
        "functional_categories": ["彈潤", "保濕", "抗老外觀保養"],
        "source_url": "https://www.akimia.com/products/electroluxe-3pcs",
        "source_urls": ["https://www.akimia.com/products/electroluxe-3pcs"],
        "price": 1000.0, "sale_price": 750.0, "list_price": 1000.0, "currency": "TWD", "price_status": "sold_out_member_price", "price_source_url": "https://www.akimia.com/products/electroluxe-3pcs",
    },
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10653": {
        "official_name": "RÉVIVE 銀采抗皺超導面膜",
        "description": "官方定位為彈潤、豐盈、保濕與細紋外觀照護，含菸鹼醯胺、泛醇、玻尿酸及多種胜肽。",
        "ingredients_list": ["Water", "Glycerin", "Dipropylene Glycol", "PEG-8", "Methyl Gluceth-20", "Bis-PEG-15 Methyl Ether Dimethicone", "PEG/PPG-14/7 Dimethyl Ether", "Butylene Glycol", "1,2-Hexanediol", "Hydroxyacetophenone", "Panthenol", "Niacinamide", "Xylitylglucoside", "Citrus Reticulata Fruit Extract", "Allantoin", "Arginine", "Carbomer", "Anhydroxylitol", "Sodium Hyaluronate", "Dipotassium Glycyrrhizate", "Mannitol", "Xylitol", "Xanthan Gum", "Polyacrylate Crosspolymer-6", "Hydroxyethylcellulose", "PPG-26-Buteth-26", "PEG-40 Hydrogenated Castor Oil", "Pelargonium Graveolens Leaf Oil", "Lonicera Japonica Flower Extract", "Bis-Ethoxydiglycol Cyclohexane 1,4-Dicarboxylate", "Ectoin", "Saccharide Isomerate", "Sea Water", "Acetyl Tetrapeptide-11", "Polysorbate 60", "Disodium Phosphate", "Acetyl Tetrapeptide-9", "Hexapeptide-9", "Hydrolyzed Sodium Hyaluronate", "Sodium Phosphate", "Phenoxyethanol", "Hydrolyzed Rhodophyceae Extract", "Citric Acid", "Sodium Citrate", "Caprylyl Glycol", "Acetyl Hexapeptide-8", "Red 4", "Yellow 6"],
        "key_ingredients": ["菸鹼醯胺", "泛醇", "Ectoin", "玻尿酸", "多種胜肽"],
        "concerns": ["彈性不足", "乾燥", "細紋外觀", "暗沉"],
        "functional_categories": ["彈潤", "保濕", "柔嫩", "抗老外觀保養"],
        "source_url": "https://www.revive.com.tw/products/volumizing-anti-wrinkle-face-mask",
        "source_urls": ["https://www.revive.com.tw/products/volumizing-anti-wrinkle-face-mask"],
        "price": 5500.0, "list_price": 5500.0, "currency": "TWD", "price_status": "official_observed", "price_source_url": "https://www.revive.com.tw/products/volumizing-anti-wrinkle-face-mask",
    },
}


PARTIAL = {
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10600": {"official_name": "森田藥粧 超A醇逆齡抗皺修復面膜", "description": "官方通路標示 A 醇、胜肽、積雪草與角鯊烷等重點成分；完整 INCI 尚待人工補件。", "key_ingredients": ["A 醇", "胜肽", "積雪草", "角鯊烷"], "concerns": ["乾燥", "粗糙", "彈性不足", "細紋外觀"], "functional_categories": ["彈潤", "修護", "舒緩", "保濕"], "source_url": "https://www.watsons.com.tw/morita-%E6%A3%AE%E7%94%B0%E8%97%A5%E7%B2%A7%E8%B6%85a%E9%86%87%E9%80%86%E9%BD%A1%E6%8A%97%E7%9A%BA%E4%BF%AE%E5%BE%A9%E9%9D%A2%E8%86%9C7%E5%85%A5/p/BP_285592", "source_urls": ["https://www.watsons.com.tw/morita-%E6%A3%AE%E7%94%B0%E8%97%A5%E7%B2%A7%E8%B6%85a%E9%86%87%E9%80%86%E9%BD%A1%E6%8A%97%E7%9A%BA%E4%BF%AE%E5%BE%A9%E9%9D%A2%E8%86%9C7%E5%85%A5/p/BP_285592"], "price": 290.0, "sale_price": 290.0, "list_price": 399.0, "currency": "TWD", "price_status": "observed_current"},
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10613": {"official_name": "TTM 提提研 保濕金箔黑面膜", "description": "官方定位為保濕與屏障照護，並標示無香精；完整 INCI 尚待人工補件。", "key_ingredients": ["金箔", "保濕複合成分"], "concerns": ["乾燥", "缺水"], "functional_categories": ["保濕", "屏障照護"], "source_url": "https://www.ttmask.com/product/%E4%BF%9D%E6%BF%95%E9%87%91%E7%AE%94%E9%BB%91%E9%9D%A2%E8%86%9C", "source_urls": ["https://www.ttmask.com/product/%E4%BF%9D%E6%BF%95%E9%87%91%E7%AE%94%E9%BB%91%E9%9D%A2%E8%86%9C"], "price": 80.0, "list_price": 80.0, "currency": "TWD", "price_status": "official_observed"},
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10584": {"official_name": "SK-II 青春敷面膜", "description": "官方定位為高濃度 PITERA™ 面膜，提供補水與淨亮外觀保養；台灣版本完整 INCI 尚待人工補件。", "key_ingredients": ["PITERA™"], "concerns": ["乾燥", "暗沉"], "functional_categories": ["保濕", "淨亮"], "source_url": "https://sk-ii.com.tw/product/facial-treatment-mask", "source_urls": ["https://sk-ii.com.tw/product/facial-treatment-mask", "https://www.momoshop.com.tw/product/10415074"], "price": 2871.0, "sale_price": 2871.0, "list_price": 3190.0, "currency": "TWD", "price_status": "observed_current", "price_source_url": "https://www.momoshop.com.tw/product/10415074"},
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10583": {"official_name": "MIRAE 未來美 專業院線 PDRN 外泌體新生面膜", "description": "官方頁標示 PDRN、乳源外泌體與胜肽等重點成分；宣稱僅以保養品外觀改善表述，完整 INCI 尚待人工補件。", "key_ingredients": ["PDRN", "乳源外泌體", "胜肽"], "concerns": ["乾燥", "彈性不足", "毛孔外觀", "暗沉"], "functional_categories": ["保濕", "彈潤", "毛孔外觀", "淨亮"], "source_url": "https://www.mirae8.com/SalePage/Index/10663607", "source_urls": ["https://www.mirae8.com/SalePage/Index/10663607"], "price_status": "promotion_not_comparable"},
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10622": {"official_name": "肌研白潤高效美白晚安精華凍膜 90g", "description": "官方標示傳明酸、四種維他命 C 與雙重玻尿酸，定位為淨亮、保濕與晚安修護；完整 INCI 尚待人工補件。", "key_ingredients": ["傳明酸", "四種維他命 C", "雙重玻尿酸"], "concerns": ["暗沉", "斑點外觀", "乾燥"], "functional_categories": ["淨亮", "保濕", "修護"], "source_url": "https://www.hadalabo.com.tw/products/94", "source_urls": ["https://www.hadalabo.com.tw/products/94", "https://www.watsons.com.tw/%E8%82%8C%E7%A0%94-%E8%82%8C%E7%A0%94%E7%99%BD%E6%BD%A4%E9%AB%98%E6%95%88%E7%BE%8E%E7%99%BD%E6%99%9A%E5%AE%89%E7%B2%BE%E8%8F%AF%E5%87%8D%E8%86%9C90g/p/BP_302833"], "price": 249.0, "sale_price": 249.0, "list_price": 549.0, "currency": "TWD", "price_status": "observed_out_of_stock"},
}


BLOCKED = {
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10612": {"data_block_reason": "retired_product_and_official_identity_unverified", "description": "舊品已下架，現有官方站無法驗證此名稱、版本與完整成分，暫停推薦。", "source_url": "https://www.rebskin.com/", "source_urls": ["https://www.rebskin.com/"]},
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10615": {"official_name": "MEDI-PEEL Hyaluron Rose Energy Tox Mask / Ampoule Mask", "data_block_reason": "multiple_market_formulas_require_sku_or_batch_binding", "description": "不同市場存在同名或近似名稱版本，需先綁定 SKU、條碼或包裝版本才可補入成分與推薦。"},
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10661": {"data_block_reason": "database_name_mixes_two_distinct_aq_products", "description": "現有名稱疑似混合 AQ 晚霜與面膜資訊，需以 SKU、條碼或包裝照片重新識別。", "source_url": "https://www.cosmedecorte.com.tw/aq", "source_urls": ["https://www.cosmedecorte.com.tw/aq"]},
    "4:e143ed48-6937-480d-8b51-a4cf19047907:10623": {"official_name": "我的美麗日記 清酒酒粕面膜（待確認舊版）", "data_block_reason": "trend_record_name_does_not_match_official_historical_product", "description": "熱門證據對應的歷史商品疑為清酒酒粕面膜，與資料庫現名不一致；需人工確認原始榜單與包裝。", "source_url": "https://www.beautydiary.com.tw/faq", "source_urls": ["https://www.beautydiary.com.tw/faq", "https://mybeautydiary-jp.com/lineup/sakeless.html"]},
}


def merged_rows():
    rows = []
    for status, collection, ingredient_level in (
        ("full_official", FULL, "official_full"),
        ("partial_official", PARTIAL, "official_key_only"),
        ("blocked_identity", BLOCKED, "blocked"),
    ):
        for element_id, fields in collection.items():
            props = dict(fields)
            props.update({
                "data_completion_status": status,
                "data_review_status": "pending_human_review",
                "data_enrichment_version": "trend-mask-v1-20260929",
                "ingredient_evidence_level": ingredient_level,
                "quality_review_required": True,
                "recommendable": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            if status == "blocked_identity":
                props["is_active"] = False
                props["name_verification_status"] = "ambiguous"
            else:
                props["name_verification_status"] = "official_verified"
            if "price" in props:
                props["price_observed_at"] = "2026-09-29"
            rows.append({"element_id": element_id, "props": props})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    rows = merged_rows()
    assert len(rows) == 16
    driver = GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]))
    try:
        with driver.session() as session:
            current = session.run(
                "MATCH (p:Product) WHERE elementId(p) IN $ids RETURN elementId(p) AS id, p.name AS name, p.recommendable AS recommendable ORDER BY id",
                ids=[row["element_id"] for row in rows],
            ).data()
            if len(current) != 16:
                raise RuntimeError(f"Preflight found {len(current)}/16 products")
            unsafe = [row for row in current if row["recommendable"] is not False]
            if unsafe:
                raise RuntimeError(f"Preflight refused: recommendable state changed for {unsafe}")
            print(f"PREFLIGHT OK products={len(current)} full={len(FULL)} partial={len(PARTIAL)} blocked={len(BLOCKED)}")
            if not args.apply:
                print("DRY RUN only; use --apply to write")
                return
            result = session.execute_write(
                lambda tx: tx.run(
                    "UNWIND $rows AS row MATCH (p:Product) WHERE elementId(p)=row.element_id SET p += row.props RETURN count(p) AS updated",
                    rows=rows,
                ).single()["updated"]
            )
            if result != 16:
                raise RuntimeError(f"Transaction updated {result}/16 products")
            validation = session.run(
                "MATCH (p:Product) WHERE elementId(p) IN $ids RETURN p.data_completion_status AS status, count(*) AS count, sum(CASE WHEN p.recommendable=false AND p.quality_review_required=true THEN 1 ELSE 0 END) AS gated ORDER BY status",
                ids=[row["element_id"] for row in rows],
            ).data()
            print(f"APPLIED updated={result} validation={validation}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
