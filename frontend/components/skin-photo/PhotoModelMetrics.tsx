import metrics from "@/public/skin-research/current-metrics.json";
import styles from "./PhotoModelMetrics.module.css";

const labels = { acne_like_bumps: "痘痘樣突起", redness: "泛紅", dark_marks: "深色痕跡", flaking: "脫屑" };
const percent = (value: number) => `${(value * 100).toFixed(1)}%`;

export default function PhotoModelMetrics() {
  return <details className={styles.metrics}>
    <summary>目前能辨識的分類與驗證成績</summary>
    <p>網站目前使用 Qwen3-VL-4B 官方基礎模型。以下是既有 80 張驗證集（64 張皮膚、16 張物件控制圖）的四特徵成績，尚不是獨立手機全臉測試的正確率。</p>
    <div className={styles.scroll}><table>
      <caption>特徵是否出現；每一類分別計算</caption>
      <thead><tr><th>分類</th><th>Accuracy</th><th>精確率 P</th><th>召回率 R</th><th>F1</th></tr></thead>
      <tbody>{Object.entries(labels).map(([tag, label]) => {
        const m = metrics.metrics.historical_base.by_tag[tag as keyof typeof labels];
        return <tr key={tag}><th scope="row">{label}</th><td>{percent(m.accuracy)}</td><td>{percent(m.precision)}</td><td>{percent(m.recall)}</td><td>{percent(m.f1)}</td></tr>;
      })}</tbody>
    </table></div>
    <p>精確率＝判為有特徵時有多少判對；召回率＝實際有特徵時找到多少。Accuracy 包含判對「沒有」，少數類別仍可能漏判，請一起看召回率。深色痕跡與脫屑仍是弱項。</p>
    <p>多類特徵可同時出現；未提供毛孔、皺紋、凹洞深度、油乾肌或疾病分類的準確率。臉部區域輔助線也不是病灶標註。微調研究版仍未達部署門檻。<a href="/skin-research/index.html">查看完整訓練比較 →</a></p>
  </details>;
}
