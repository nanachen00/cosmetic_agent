"""Publish aggregate research metrics only; never publish dataset photographs."""
import csv,html,json,time
from pathlib import Path

ROOT=Path(__file__).resolve().parent

def conclusion(s):
    t=s.get('test')
    if not t:return '尚未完成保留測試；不判定改善。'
    delta=t['macro_f1_difference'];ci=t['paired_difference_ci95']
    if delta<=0:return '保留測試未改善，保留原模型。'
    if ci['lower_95']<=0:return '分數提高，但 95% 差異區間包含零，尚無足夠證據判定穩定提升。'
    if not t['research_gate_passed']:return 'F1 有提升，但未通過全部預設研究門檻；不替換原模型。'
    return '通過此局部研究任務的預設門檻，仍須另行驗證真實照片與四類膚況。'

def main():
    cycles=[]
    for name,p in [('第一輪：框選衍生局部',ROOT),('第二輪：顏色配對陰性＋MLP',ROOT/'cycles/v2')]:
        if (p/'summary.json').exists():cycles.append((name,p,json.loads((p/'summary.json').read_text())))
    public=ROOT.parents[2]/'frontend/public/skin-research';public.mkdir(exist_ok=True)
    records=[];sections=[]
    e=lambda x:html.escape(str(x))
    for title,path,s in cycles:
        samples='；'.join(f'{k}：{v["parents"]} 張來源／{v["patches"]} 個局部' for k,v in s['dataset']['counts'].items())
        rows=[('原始模型','validation',s['baseline']['validation'])]+[(r['name'],'validation',r['validation']) for r in s['candidates']]
        if s.get('test'):rows += [('原始模型','locked test',s['test']['baseline']),(s['selected_by_validation'],'locked test',s['test']['selected'])]
        table=''
        for model,split,m in rows:
            records.append({'cycle':title,'model':model,'split':split,**m})
            display_name={'lora-r8-epoch1':'LoRA r8 · 1輪','lora-r8-epoch2':'LoRA r8 · 2輪','lora-r16-epoch2':'LoRA r16 · 2輪','lora-r8-attention-mlp-hardneg':'LoRA · 配對陰性與MLP'}.get(model,model)
            display_split={'validation':'驗證集','locked test':'保留測試集'}.get(split,split)
            table+='<tr><td>'+e(display_name)+'</td><td>'+e(display_split)+'</td><td>'+str(m['n'])+'</td>'+''.join(f'<td>{m[k]*100:.1f}%</td>' for k in ['macro_f1','positive_precision','positive_recall','valid_json_rate'])+f'<td>{m["fp"]}</td><td>{m["fn"]}</td></tr>'
        difference=''
        if s.get('test'):
            t=s['test'];ci=t['paired_difference_ci95'];before=t['baseline'];after=t['selected']
            difference=f'<p class="delta"><strong>保留測試 F1：{before["macro_f1"]*100:.1f}% → {after["macro_f1"]*100:.1f}%</strong>　誤報 {before["fp"]} → {after["fp"]}；漏判 {before["fn"]} → {after["fn"]}。</p><p class="note">差異：{t["macro_f1_difference"]*100:+.1f} 個百分點；配對來源影像重抽樣 95% 區間 [{ci["lower_95"]*100:+.1f}, {ci["upper_95"]*100:+.1f}]。</p>'
        sections.append('<section><h2>'+e(title)+'</h2><p>'+e(samples)+'</p><p><strong>'+e(conclusion(s))+'</strong></p>'+difference+'<div class="scroll"><table><thead><tr><th>版本</th><th>資料組</th><th>局部數</th><th>Macro-F1</th><th>精確率</th><th>召回率</th><th>JSON 合格</th><th>誤報</th><th>漏報</th></tr></thead><tbody>'+table+'</tbody></table></div></section>')
    if records:
        fields=['cycle','model','split',*records[0].keys()];fields=list(dict.fromkeys(fields))
        with (ROOT/'all-comparisons.csv').open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(records)
        (public/'comparison.csv').write_bytes((ROOT/'all-comparisons.csv').read_bytes())
    summary={'updated_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'cycles':[{'title':name,'task':s['task'],'selection':s.get('selected_by_validation'),'conclusion':conclusion(s),'test':s.get('test')} for name,p,s in cycles],'production_model_changed':False,'dataset_restriction':'academic/research only','ui_verification':{'same_feature_three_regions_old_cards':3,'new_groups':1,'retained_evidence_items':3,'test_status':'six grouping contracts passed; Next.js build and TypeScript passed; public sample round trip verified'},'training_data_quality_audit':{'cycle1_train_negative_reviewed':16,'obvious_background_or_clothing':3,'reviewer':'assistant visual screening; not a dermatologist or new ground-truth annotation','response':'cycle2 matches negative crop mean RGB and proximity to positive; excludes previous test parents'}}
    summary['training_data_quality_audit']['cycle2_train_negative_reviewed']=16
    summary['training_data_quality_audit']['cycle2_obvious_background_or_clothing']=1
    compatibility=None
    if (ROOT/'compatibility-probe.json').exists():
        compatibility=json.loads((ROOT/'compatibility-probe.json').read_text())
        summary['full_report_compatibility']={'status':compatibility['status'],'stats':compatibility.get('stats',[]),'note':compatibility['note']}
    (ROOT/'final-comparison.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    page='''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BLINK 訓練優化比較</title><style>body{margin:0;background:#faf8fb;color:#4d4353;font:14px/1.8 system-ui}main{max-width:1150px;margin:auto;padding:28px 20px 60px}h1{font-size:30px;font-weight:650}h2{font-size:19px;color:#765d84}a{color:#876b93}section,.intro{background:#fff;border:1px solid #e7dfea;border-radius:16px;padding:22px;margin:20px 0}.eyebrow{font-size:11px;letter-spacing:.1em;color:#987fa5}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:12px}td,th{padding:11px 9px;text-align:left;border-bottom:1px solid #eee7f1}th{color:#876b93}.delta{color:#876b93}.note{color:#8e7f97;font-size:12px}li{margin:6px 0}</style><main><a href="/skin-photo">← 返回照片分析</a><p class="eyebrow">BLINK RESEARCH · 2026-10-31</p><h1>訓練有沒有更好，讓數據回答。</h1><div class="intro"><strong>評估範圍</strong><p>局部照片是否包含 ACNE04 資料集框選病灶。原始框選是作者提供；未框選局部不代表經醫師確認正常。這份成績不能當成泛紅、色沉、脫屑或疾病診斷準確率。</p><p>兩輪的資料抽樣方式不同，請在各輪內比較原始模型與微調模型。所有研究權重保留在研究環境，網站四類照片分析仍使用原模型。</p><a href="comparison.csv" download>下載完整比較 CSV ↘</a></div>'''+''.join(sections)+'''<section><h2>事先固定的判斷標準</h2><ul><li>先按來源影像及近似重複群組切分，再裁切；同一來源的局部不跨組。</li><li>驗證集選參數與 checkpoint，保留測試集只比較原始模型與驗證集選出的版本。</li><li>無法解析的輸出也算錯誤，不排除拒答來美化成績。</li><li>門檻：test macro-F1 ≥80%、精確率 ≥85%、F1 增加至少3個百分點、JSON合格率 ≥98%。另報告配對重抽樣區間，區間跨零不宣稱已證實改善。</li><li>第一輪三組設定；第二輪修正陰性裁切並加入語言 MLP、翻轉增強，最多兩個 epoch。兩輪完成即停止，不無限消耗 GPU。</li></ul></section><section><h2>這次如何修正</h2><p>第一輪抽查16個訓練陰性局部，其中3個主要是背景或衣服。第二輪挑選與陽性局部顏色接近、距離較近且不覆蓋任何原始標註框的陰性局部，並排除前輪全部保留測試來源。顏色配對不是已驗證的皮膚分割，仍可能有漏標與背景混雜。</p><p>版面：相同特徵合併，部位用標籤呈現，各部位原始描述保留在可展開區域；判讀限制與補充問題合併到下一步。三部位同類特徵由3張卡片整理成1項，3份依據皆保留。</p></section><section><h2>目前證據的限制</h2><p>資料缺可靠人物ID，僅能確認來源影像與近似重複群組分離，不能保證人物完全分離。這是單一訓練 seed 的小型研究，局部裁切不等於實際全臉照片。延遲是共用 MI300X 上單一局部的暖機推論時間，不含模型載入、前處理與網站往返。</p><p>ACNE04 限學術研究；商業用途須另洽原作者。未使用使用者上傳照片訓練。</p><p><a href="https://github.com/xpwu95/LDL">原始資料與授權</a> · <a href="https://huggingface.co/datasets/Layered-Labs/ACNE04">固定版本資料鏡像</a></p></section><p class="note">更新時間 UTC：'''+e(summary['updated_utc'])+'''</p></main></html>'''
    page=page.replace('顏色配對不是已驗證的皮膚分割，仍可能有漏標與背景混雜。','第二輪同樣抽查16個訓練陰性局部，仍有1個主要是背景或衣服。抽查由助手做視覺品管，不是醫師重新標註。顏色配對不是已驗證的皮膚分割，仍可能有漏標與背景混雜。')
    page=page.replace('這是單一訓練 seed 的小型研究，局部裁切不等於實際全臉照片。','這是單一訓練 seed 的小型研究，局部裁切不等於實際全臉照片。第二輪同時變更陰性取樣、語言 MLP、翻轉增強和決策 loss，尚未逐項消融，不能將增益單獨歸因於某一項。')
    if compatibility and compatibility['status']=='complete':
        rows=''.join('<tr><td>'+e(r['version'])+'</td><td>'+str(r['valid_full_report'])+'/'+str(r['model_calls'])+'</td><td>'+('通過' if r['non_skin_refused'] else '未通過')+'</td><td>'+('通過' if r['low_contrast_heuristic_refused'] else '未通過')+'</td></tr>' for r in compatibility['stats'])
        extra='<section><h2>切回完整照片報告的相容性檢查</h2><p>以公開例圖、兩張作者保留影像與控制圖檢查格式和拒絕行為；此處沒有四類自然語言標註，不能當成膚況準確率。低對比控制圖由既有規則先行拒絕。</p><div class="scroll"><table><tr><th>版本</th><th>完整 JSON 合格／模型呼叫</th><th>非皮膚控制圖拒絕</th><th>低對比規則拒絕</th></tr>'+rows+'</table></div></section>'
        page=page.replace('<section><h2>目前證據的限制</h2>',extra+'<section><h2>目前證據的限制</h2>')
    (ROOT/'comparison.html').write_text(page);(public/'overview.html').write_text(page)
    if len(cycles)==2 and all(s.get('test') for _,_,s in cycles):(public/'index.html').write_text(page)
    print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
