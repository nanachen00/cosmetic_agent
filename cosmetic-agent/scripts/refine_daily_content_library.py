"""Refine the two reviewed daily-content libraries without creating duplicates."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv('/var/lib/jenkins/tomod/cosmetic-agent/.env')
from database.postgres.connection import postgres_connection

BEAUTY = {
    'beb649fe-c07b-4bad-9303-9bb45c3206a7': {
        'title': '油肌不是洗得越乾淨越好',
        'summary': '早晚各溫和洗一次，流汗後再清潔即可；磨砂、熱水或強力去油可能刺激皮膚，反而讓出油與痘痘更難控制。若產品造成持續刺痛或脫皮，先減少頻率或停用。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/everyday-care/skin-care-basics/dry/oily-skin',
    },
    '1603e0eb-0efa-4555-ade8-2ec0f2b7ab07': {
        'title': '保養品寫「外泌體」，不等於醫療級再生治療',
        'summary': '外用保養品和注射、醫療療程不是同一件事；不能只看到「外泌體」就推論能再生、修復疾病或取代治療。先看完整成分與合法化妝品宣稱；若涉及注射或治療，應由合格醫師評估。',
        'source_name': 'U.S. Food and Drug Administration',
        'source_url': 'https://www.fda.gov/safety/medical-product-safety-information/public-safety-alert-due-marketing-unapproved-stem-cell-and-exosome-products',
    },
    '44950d9b-7b34-41e7-ade1-20a4dd3c68d0': {
        'title': '毛孔不能永久「關起來」，但可以看起來不明顯',
        'summary': '毛孔大小不會被冰水或單一產品永久改變。溫和清潔、避免阻塞毛孔，並依膚況使用水楊酸或維生素 A 類產品，可改善堵塞與毛孔外觀；刺激或脫皮時要減少使用。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/everyday-care/skin-care-secrets/face/treat-large-pores',
    },
    '4bc30d2d-3ed2-45c6-9e3f-8086cf510067': {
        'title': '痘痘不是「沒洗乾淨」，洗太多反而可能惡化',
        'summary': '痘痘和油脂、角質阻塞、發炎等因素有關，不是靠刷洗就能清掉。一天溫和洗臉最多兩次並在流汗後清潔；不要磨砂、用力搓洗或頻繁更換多種抗痘品。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/diseases/acne/DIY/wont-clear',
    },
    '5b87eafd-2477-424b-b129-3cae7c1f3a70': {
        'title': '刺痛不代表保養品「正在生效」',
        'summary': '持續灼熱、紅腫、發癢或起疹，不該當成建立耐受。立即洗掉並停用，先以冷敷或單純凡士林舒緩；若反應嚴重、持續不退或範圍擴大，應尋求皮膚科協助。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/everyday-care/skin-care-secrets/prevent-skin-problems/test-skin-care-products',
    },
}

DERM = {
    '19983f7d-33e8-4c87-892e-e52796d4a3c2': {
        'title': '冒痘先別擠：先做這三件事',
        'summary': '不要擠、摳或針挑；早晚溫和清潔，並只選一種抗痘有效成分依標示使用。連續使用 6 至 8 週仍未改善、出現深層疼痛硬塊或開始留疤，就安排皮膚科評估。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/diseases/acne/skin-care/tips',
    },
    '1e50e7ed-c105-44c9-bee2-9cd676fca214': {
        'title': '新保養品先做 7 至 10 天局部測試',
        'summary': '在手臂內側或手肘彎處選約硬幣大小範圍，按正常用量每天兩次、連續 7 至 10 天。若出現紅、癢、腫或明顯刺痛，立即洗掉並停用；反應嚴重或未緩解時看皮膚科。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/everyday-care/skin-care-secrets/prevent-skin-problems/test-skin-care-products',
    },
    '46e9bbbf-50b0-44e0-9495-9bef5839d04f': {
        'title': '痘痘肌洗臉：早晚各一次，流汗後再洗',
        'summary': '使用溫和、不磨砂的清潔品，以指腹輕洗，不用洗臉刷或顆粒去角質。一天超過兩次可能增加刺激；若洗後緊繃、泛紅或脫皮，應降低清潔力與其他刺激性產品頻率。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/diseases/acne/skin-care/tips',
    },
    '58bc85d3-9890-4aee-bbf1-be234aebfc23': {
        'title': '乾燥肌洗澡：溫水 5 至 10 分鐘，三分鐘內保濕',
        'summary': '避免熱水與長時間沖洗；洗後用毛巾輕拍，皮膚仍微濕時立刻擦無香料乳霜或軟膏。若已裂開、流血、疼痛或持續發癢，需由皮膚科確認是否為濕疹或其他皮膚病。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/everyday-care/skin-care-basics/dry/dermatologists-tips-relieve-dry-skin',
    },
    '78cf4fee-77cd-42d0-8846-572655ce2877': {
        'title': '化妝品用後紅腫或灼熱：立即停用並保留包裝',
        'summary': '先以清水溫和洗掉、停止使用，並保留產品與完整成分表供判斷。若紅腫快速擴散、起水泡、眼唇腫脹，或出現呼吸與吞嚥困難，應立即就醫；呼吸困難屬急症。',
        'source_name': 'U.S. Food and Drug Administration / American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/everyday-care/itchy-skin/rash/rash-101',
    },
    '7c1005e0-0cf9-4be8-85d5-7806e582ecbe': {
        'title': '備孕或懷孕：先停用維生素 A 類保養與痘痘藥',
        'summary': '先檢查成分並停止 retinoid／retinol 類產品；處方痘痘藥、對苯二酚及高濃度水楊酸等也應先詢問婦產科或皮膚科。最安全的做法是帶著產品或成分表請醫師逐項確認。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/everyday-care/skin-care-secrets/routine/pregnancy-skin-care',
    },
    '8e3b303e-6bd1-4779-baa4-a3f1e850d5ca': {
        'title': '成人痘先用單一方案 6 至 8 週，不要同時疊很多酸',
        'summary': '依痘痘型態選一種適合的非處方抗痘成分，按標示規律使用，並搭配溫和清潔與保濕。不要每幾天就換產品；若有深層疼痛、囊腫、留疤或 6 至 8 週仍無改善，請看皮膚科。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/diseases/acne/diy/adult-acne-treatment',
    },
    '98db78ff-0ca5-4458-ac04-ff61767fe092': {
        'title': '防曬不是早上擦一次：戶外約每兩小時補擦',
        'summary': '選廣效、防水且 SPF 30 以上的防曬，出門前均勻塗足；戶外活動約每兩小時補擦，游泳、流汗或擦拭後也要重擦。防曬仍需搭配遮蔭、帽子與衣物。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/everyday-care/sun-protection/shade-clothing-sunscreen/how-to-apply-sunscreen',
    },
    'a836035a-44c6-4e36-a288-4b060303c8a3': {
        'title': '這些皮疹警訊不要等：快速擴散、疼痛、水泡或發燒',
        'summary': '若皮疹快速擴散、明顯疼痛、起水泡或破皮、合併發燒，或影響眼、嘴唇、口腔與生殖器，應儘快就醫。若眼唇腫脹或呼吸、吞嚥困難，應立即急診處理。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/everyday-care/itchy-skin/rash/rash-101',
    },
    'ccd61adb-c642-4a27-b67e-2365b4b1eb1d': {
        'title': '乾燥脫皮先停酸與磨砂，改用無香料乳霜或軟膏',
        'summary': '先暫停去角質酸類、磨砂與高刺激活性成分；洗臉後趁微濕擦無香料乳霜或軟膏。若一至兩週仍未改善，或出現裂傷、流血、疼痛與持續搔癢，請看皮膚科。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/everyday-care/skin-care-basics/dry/pick-moisturizer',
    },
    'd6b14101-9060-4317-8231-227498e20a56': {
        'title': '痣出現 ABCDE 變化，請安排皮膚科檢查',
        'summary': '留意左右不對稱、邊緣不規則、顏色不均、直徑變大，以及持續演變。若痣新出現、快速改變、反覆出血或傷口不癒合，不要只靠照片比對，應由皮膚科實際檢查。',
        'source_name': 'American Academy of Dermatology',
        'source_url': 'https://www.aad.org/public/diseases/skin-cancer/abcdes-melanoma',
    },
    'e6296720-c93e-46f9-83c9-54f6a2dd053a': {
        'title': '敏感肌換產品：一次只加一樣，先測 7 至 10 天',
        'summary': '不要只看「敏感肌適用」或「低敏」標示。先讀完整成分，一次只更換一項產品，並在手臂內側做 7 至 10 天局部測試；若反覆過敏或找不到原因，可由皮膚科做貼膚測試。',
        'source_name': 'American Academy of Dermatology / U.S. Food and Drug Administration',
        'source_url': 'https://www.aad.org/public/everyday-care/skin-care-secrets/prevent-skin-problems/test-skin-care-products',
    },
}

UPDATES = {**BEAUTY, **DERM}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    ids = list(UPDATES)
    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT * FROM daily_contents WHERE id = ANY(%s::uuid[]) ORDER BY content_type,id', (ids,))
            before = [dict(row) for row in cursor.fetchall()]
            if len(before) != len(ids):
                found = {str(row['id']) for row in before}
                raise RuntimeError(f'preflight found {len(before)}/{len(ids)}; missing={set(ids)-found}')
            wrong_types = []
            for row in before:
                expected = 'beauty_tip' if str(row['id']) in BEAUTY else 'dermatologist_faq'
                if row['content_type'] != expected:
                    wrong_types.append((str(row['id']), row['content_type'], expected))
            if wrong_types:
                raise RuntimeError(f'content type mismatch: {wrong_types}')
            print(f'PREFLIGHT OK beauty={len(BEAUTY)} dermatologist={len(DERM)} total={len(ids)}')
            if not args.apply:
                print('DRY RUN only; use --apply to write')
                return
            backup_dir = Path('/var/lib/jenkins/tomod/ops/runtime/backups')
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            backup_path = backup_dir / f'daily-contents-before-refine-{stamp}.json'
            backup_path.write_text(json.dumps(before, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
            for item_id, fields in UPDATES.items():
                cursor.execute(
                    'UPDATE daily_contents SET title=%s, summary=%s, source_name=%s, source_url=%s, updated_at=NOW() WHERE id=%s::uuid',
                    (fields['title'], fields['summary'], fields['source_name'], fields['source_url'], item_id),
                )
            connection.commit()
            cursor.execute('SELECT id,content_type,title,summary,source_url,review_status,is_active FROM daily_contents WHERE id = ANY(%s::uuid[]) ORDER BY content_type,id', (ids,))
            after = [dict(row) for row in cursor.fetchall()]
            if len(after) != len(ids):
                raise RuntimeError('post-write count mismatch')
            english_bad = [r for r in after if 'AAD Welcome!' in r['summary'] or r['title'] == 'How to control oily skin']
            if english_bad:
                raise RuntimeError(f'English scrape residue remains: {english_bad}')
            if any(r['review_status'] != 'published' or r['is_active'] is not True for r in after):
                raise RuntimeError('published/active state changed unexpectedly')
            beauty_titles = {r['title'] for r in after if r['content_type'] == 'beauty_tip'}
            derm_titles = {r['title'] for r in after if r['content_type'] == 'dermatologist_faq'}
            if beauty_titles & derm_titles:
                raise RuntimeError(f'cross-category duplicate titles: {beauty_titles & derm_titles}')
            print(f'APPLIED updated={len(after)} backup={backup_path}')
            print(f'VALIDATED beauty={len(beauty_titles)} dermatologist={len(derm_titles)} exosome_titles={sum("外泌體" in r["title"] for r in after if r["content_type"]=="beauty_tip")}')


if __name__ == '__main__':
    main()
