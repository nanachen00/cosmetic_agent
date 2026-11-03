# BLINK 臉部影像研究第三輪：執行結果

更新：2026-11-01 UTC

## 結論

目前模型**還不足以稱為成熟的臉部膚況辨識**。已經有實證的是 ACNE04
局部裁切的「標註框內是否有痘痘樣病灶」；顏色、凸起高度、三角度與個人長期變化
目前只有可執行契約與合成壓力測試，尚缺真實校正資料及專家標註。

因此正式 App 維持原始 Qwen3-VL-4B-Instruct；第一輪通過的研究 LoRA 不自動切換，
第二輪 LoRA 明確不採用。

## 已完成的五項

| 項目 | 已執行 | 目前判斷 |
|---|---|---|
| 1. 病灶框與計數 | AP50、precision、recall、count MAE 程式與測試；新 frozen-v3 準備器 | 可開始新偵測器訓練；ACNE04 是框，不可假裝成真實 mask |
| 2. 顏色 | sRGB→CIELAB、CIEDE2000、ROI 暗部／高光排除、曝光／白平衡／陰影控制矩陣 | 合成色偏非常大；未有實體色卡前只可做相對顏色 |
| 3. 三角度 | front/left/right 完整性與跨視角重複率 | 重複出現不等於已證明凸起高度；需立體或光度校正 |
| 4. 個人基準 | 同人＋同裝置＋同拍攝設定＋同部位，至少三天；median/MAD | 可做日記相對變化，不是疾病或療效判斷 |
| 5. Qwen 整合 | 嚴格 `blink.skin_evidence.v1` 證據封包 | Qwen 只負責解釋結構化證據，不取代數值模組 |

本地五模組契約測試：15/15 通過。完整 Python 回歸：核心與新研究 30/30、既有研究
指標 5/5、相機／比較服務 8/8。前端 Node 測試因本地鏡像缺少 `typescript` 套件而
未執行，不能列為通過。

## 不同資料的適用性

| 資料 | 可做什麼 | 目前是否實測 | 限制／授權 |
|---|---|---|---|
| ACNE04 | 病灶框偵測、局部 presence、計數誤差 | 是，v1/v2 各 96 局部 locked test | 研究／學術用途；無可靠 subject ID；框不是 mask |
| AcneSCU | 細分類與 instance segmentation | 否 | 論文報告 276 圖、31,777 instances；使用前須核對原始存取與授權，不能只信第三方鏡像 |
| SCIN | 外部多樣性、可判讀性、膚色分層 | 否 | 10,000+ 圖；自訂 SCIN 授權、署名且禁止重新識別；不是痘痘框資料 |
| CHROMA-FIT | 真實膚色量測誤差 | 否 | DSLR 情境不同於手機；不是痘痘資料；使用前核對資料條款 |

禁止從一般網路任意抓臉部照片回訓。每張圖都要有來源、允許用途、授權／同意、人物、
日期與裝置分組。

## 目前正確率與低分原因

第一輪 ACNE04 locked test：原模型 macro-F1 85.4%，LoRA r16 為 89.6%；誤報 9→6、
漏判 5→4，通過該局部研究門檻。

第二輪較難的顏色配對負例：原模型 macro-F1 81.2%、precision 77.8%、recall 87.5%；
LoRA r8 為 macro-F1 83.3%、precision 88.1%、recall 77.1%。它修正 7 個誤報，卻新增
5 個漏判；macro-F1 差異的 95% 配對 bootstrap 區間為 -4.1 到 +7.6 個百分點，故不採用。

錯誤型態是「過度保守」，不是單純容量不足。下一輪採三個 validation-only 候選：

1. r8、alpha 16、LR 5e-5、陽性權重 1.15、最多 2 epochs。
2. r8、alpha 16、LR 1e-4、1 epoch；困難陽性只能由 training cross-validation 挖掘。
3. r16、alpha 32、LR 5e-5、陽性權重 1.15、1 epoch，僅作容量檢查。

候選必須同時達到 valid JSON ≥98%、precision ≥85%、recall ≥85%、macro-F1 ≥85%；
否則全部停止，不因「總分稍高」接受漏判惡化。真正的視覺加強主線應是小病灶 detector，
不是只擴大語言層 LoRA，因目前 LoRA 仍凍結 visual encoder。

## 控制變因

切分單位是 subject＋date＋device；ACNE04 沒有 subject ID 時只能誠實標示為來源圖／近重複
群組隔離。一次只改 exposure、white balance、blur、directional shadow 其中一項；只保留
一個預先登記的 warm＋shadow 交互項。固定解析度、壓縮、距離、角度、模型 revision、prompt、
decoder。膚色、裝置、光線、模糊、病灶類別與部位都要分層報告；n<20 不作群組結論。

## 現行 App 檢查

現行頁面可選單張照片、相機預覽、公開例圖、原模型／微調模型比較，結果已依特徵合併並
展開各部位證據。外部 `http://IP` 不符合瀏覽器 secure context，因此網頁相機只能在 HTTPS
或 localhost 使用；目前已有手機原生拍照／選檔備援。尚未具備三角度序列、實體色卡偵測、
固定距離引導或個人基準資料，因此仍應標示為實驗功能。

## GPU 紀錄

`gpu_monitor.py` 會用 `amd-smi`，失敗再用 `rocm-smi`；每 2 秒保存使用率、顯存、溫度、
功耗、PyTorch 可見記憶體、程序返回碼及平均／峰值摘要。本地紀錄器自測通過；本機無 AMD
GPU，所以真實 MI300X 數字須在遠端訓練時產生。

遠端執行：

```bash
cd /var/lib/jenkins/tomod/experiments/skin-vlm/research/v3

python3 prepare_cycle3_data.py \
  --raw ../cycles/v2/data/raw \
  --output records/data/frozen-v3 \
  --exclude-manifest ../records/data/frozen-v1/train.jsonl \
  --exclude-manifest ../records/data/frozen-v1/val.jsonl \
  --exclude-manifest ../records/data/frozen-v1/test.jsonl \
  --exclude-manifest ../records/cycles/v2/data/frozen-v2/train.jsonl \
  --exclude-manifest ../records/cycles/v2/data/frozen-v2/val.jsonl \
  --exclude-manifest ../records/cycles/v2/data/frozen-v2/test.jsonl

python3 gpu_monitor.py --output records/gpu/cycle3-training.jsonl --interval 2 -- \
  ../../.venv/bin/python train_cycle3.py \
  --data records/data/frozen-v3 --model ../../model --output records/training
```

訓練完成後先查看 `records/training/selection.json` 與
`records/gpu/cycle3-training.summary.json`。這一步只做 validation 選擇，不會自動評 test 或部署。

## 主要研究來源

- ACNE04 / LDL: https://github.com/xpwu95/LDL
- AcneSCU / SA-RPN: https://doi.org/10.1109/JBHI.2023.3304727
- SCIN: https://github.com/google-research-datasets/scin
- CHROMA-FIT: https://openaccess.thecvf.com/content/WACV2024W/DVPBA/html/Pangelinan_The_CHROMA-FIT_Dataset_Characterizing_Human_Ranges_of_Melanin_for_Increased_WACVW_2024_paper.html
- Smartphone distance/angle colour study: https://pubmed.ncbi.nlm.nih.gov/36772956/
