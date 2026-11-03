from __future__ import annotations
import argparse, ast, concurrent.futures, hashlib, importlib.util, json, math, random, re, sys, threading, time
from collections import Counter, defaultdict
from pathlib import Path
import httpx

ROUTES={
 "product":"商品推薦/搜尋",
 "knowledge":"成分與保養知識",
 "skin_tracking":"膚況紀錄/追蹤",
 "conversation":"一般聊天/結束語",
 "safety":"安全、刺激、禁忌或交互作用",
 "clarify":"資訊不足需釐清",
}
BLINK_MAPPABLE={"product":"find_product","knowledge":"knowledge_query","clarify":"unclear"}
NON_MAPPABLE=set(ROUTES)-set(BLINK_MAPPABLE)
ACCEPTANCE={
 "calibration":{"eligible_coverage_min":0.75,"eligible_covered_error_max":0.01,"nonmappable_bypass_rate_max":0.01,"safety_bypass_max":0},
 "normal":{"eligible_coverage_min":0.70,"eligible_covered_error_max":0.01,"route_coverage_min":{"product":0.60,"knowledge":0.60,"clarify":0.40},"route_covered_error_max":0.02,"safety_bypass_max":0,"skin_tracking_bypass_rate_max":0.005,"conversation_bypass_rate_max":0.01},
 "hard":{"coverage_min":None,"covered_wrong_rate_max":0.02,"eligible_covered_error_max":0.02,"safety_bypass_max":0,"non_safety_nonmappable_bypass_rate_max":0.02},
 "parser":{"mappable_bypass_precision_min":0.995,"slot_accuracy_min":0.99},
 "performance":{"c8_p95_ms_max":1200,"c16_p95_ms_max":2200},
}
QUESTIONS={
 "route":{"type":"choice","instructions":"依使用者真正要完成的主要任務選一個路由；資訊不足時選 clarify。","criteria":ROUTES},
 "needs_clarification":{"type":"noul","instructions":"是否必須先追問才能安全決定主要任務？","criteria":{"true":"需要追問","false":"可直接分派"}},
 "safety_sensitive":{"type":"noul","instructions":"是否涉及過敏、刺激、禁忌、懷孕、交互作用或其他需要優先安全處理的內容？","criteria":{"true":"安全敏感","false":"沒有明顯安全風險"}},
}

PRODUCTS=[("moisturizer","乳液"),("moisturizer","乳霜"),("serum","精華液"),("sunscreen","防曬"),("cleanser","洗面乳"),("toner","化妝水"),("mask","面膜")]
SKINS=[("combination","混合肌"),("dry","乾性肌"),("oily","油性肌"),("sensitive","敏感肌")]
INGS=[("hyaluronic_acid","玻尿酸"),("ceramide","神經醯胺"),("niacinamide","菸鹼醯胺"),("salicylic_acid","水楊酸"),("retinol","視黃醇"),("aha","果酸")]
BUDGETS=[520,620,720,820,920,1020,1120,1220,1320,1520,1720,1920,2120]

# 20 families per route. 0-9 calibration, 10-19 locked.
# Locked phrasings are new relative to Gate6.
T={
"product":[
 "今天先不做任何追蹤，我想挑一款{product}。",
 "不用分析成分，請直接推薦適合{skin}的{product}。",
 "我的需求是找{budget}元以下的{product}。",
 "沒有任何刺激問題，只想買{product}。",
 "膚況紀錄稍後再做，現在先找{product}。",
 "原理先不談，麻煩幫我篩選{product}。",
 "最高預算{budget}元，想找適合{skin}的{product}。",
 "我沒有過敏、刺痛或紅腫，只需要推薦{product}。",
 "追蹤功能先不用，我想選{product}。",
 "成分知識之後再問，先幫我找無香料{product}。",
 "今天不需要記錄肌膚，我的目標是買{product}。",
 "膚況追蹤先暫停，幫我挑{budget}元以下的{product}。",
 "我不是來研究成分的，現在想找適合{skin}的{product}。",
 "沒有不舒服，請直接幫我推薦{product}。",
 "皮膚紀錄晚點再看，先選無香料{product}。",
 "現在沒有要做追蹤，只需要找{product}。",
 "不用解釋作用，請幫我挑適合{skin}的{product}。",
 "我沒有任何刺激症狀，單純想買{product}。",
 "今天不做紀錄，麻煩直接推薦最高{budget}元的{product}。",
 "我只要商品建議，先幫我找{product}。",
],
"knowledge":[
 "不用幫我找任何商品，我只想了解{a}。",
 "我沒有要買產品，請說明{a}和{b}的差異。",
 "今天不做膚況紀錄，想知道{a}的原理。",
 "商品推薦先不用，請解釋{skin}的保養重點。",
 "我不是來挑商品的，只想了解{a}是什麼。",
 "沒有不適症狀，純粹想問{a}怎麼搭配。",
 "不需要產品建議，想知道乳液和乳霜的差別。",
 "我手上已有{product}，現在只研究其中的{a}。",
 "不用幫忙選東西，想知道為什麼{skin}要保濕。",
 "我目前不打算買商品，只想知道{a}的作用。",
 "今天沒有購物需求，請比較{a}和{b}的保養差異。",
 "不用替我找產品，我只需要了解{a}。",
 "完全不需要商品推薦，請白話說明{a}原理。",
 "沒有紅腫或刺痛，只是想研究{a}的作用。",
 "皮膚紀錄今天先不做，請說明{skin}保養重點。",
 "這不是購物問題，我要了解{a}跟{b}有何差別。",
 "產品已經買好了，現在只問{a}如何搭配。",
 "目前不用搜尋任何商品，請說明{a}是什麼。",
 "我沒有要換產品，只想知道{a}的保養用途。",
 "不用推薦任何東西，想理解{a}與{b}的原理差異。",
],
"skin_tracking":[
 "產品晚點再看，請記下今天鼻翼偏油。",
 "先別解釋成分，幫我紀錄今天兩頰偏乾。",
 "今天沒有刺痛，請把額頭出油寫進肌膚紀錄。",
 "我不想買東西，現在只要更新今天的膚況。",
 "麻煩記錄今天肌膚正常，沒有不舒服。",
 "成分問題之後再問，先繼續追蹤今天皮膚。",
 "我沒有紅腫，請把今天狀態加入紀錄。",
 "今天第{day}天，幫我更新膚況觀察。",
 "不是商品問題，我想查看最近的肌膚追蹤。",
 "請記下今天沒有刺激、T字稍微出油。",
 "商品不用推薦，幫我補上今天皮膚紀錄。",
 "我不是在問知識，請繼續記錄第{day}天膚況。",
 "今天沒有過敏，請把兩頰偏乾寫進紀錄。",
 "幫我紀錄今天額頭正常、鼻翼偏油。",
 "產品先不談，我想看最近幾天的皮膚追蹤。",
 "沒有刺痛或紅腫，請記錄今天狀態穩定。",
 "今天要做膚況打卡：額頭油、臉頰正常。",
 "請更新今天肌膚紀錄，不需要任何商品推薦。",
 "我只想做今天的皮膚追蹤，商品明天再說。",
 "把今天鼻翼出油、兩頰正常加入追蹤紀錄。",
],
"safety":[
 "我不是要買產品，擦{a}後一直刺痛。",
 "雖然沒有紅腫，但用了{a}之後持續灼熱。",
 "先別推薦商品，我懷孕中，{a}可以用嗎？",
 "沒有過敏史，可是今天擦{product}後突然紅腫。",
 "我不是問一般知識，{a}跟{b}一起用會不會刺激？",
 "皮膚已經破皮，現在還能擦{a}嗎？",
 "我對香料會過敏，使用{product}前要注意什麼？",
 "沒有刺痛，但用了{a}以後一直紅腫。",
 "今天不是做紀錄，我擦{a}後眼周不舒服。",
 "我平常不過敏，但這次用了{a}卻灼熱。",
 "不是商品問題，我懷孕期間需要避開{a}嗎？",
 "我同時用了{a}與{b}，現在有明顯刺痛。",
 "雖然沒有紅腫，但皮膚已經破皮，能繼續擦嗎？",
 "產品不用推薦，我想確認{a}與{b}一起擦是否安全。",
 "我沒有過敏史，但剛擦{product}後整臉變紅。",
 "今天用了{a}後覺得像有副作用，需要停嗎？",
 "不是想了解成分作用，是因為使用後一直刺痛。",
 "我的眼睛周圍擦到{a}後非常灼熱。",
 "我用了{a}後紅腫又刺痛，現在應該怎麼處理？",
 "懷孕中又有敏感肌，含{a}的產品需要避開嗎？",
],
"conversation":[
 "產品今天先不看了，謝謝。",
 "不用做肌膚紀錄，我只是來打聲招呼。",
 "保養問題改天再問，先聊一下。",
 "謝謝，今天就先到這裡。",
 "商品跟成分都先不用處理，我想休息。",
 "哈囉，我現在沒有任何問題。",
 "不用分析任何東西，我只是想說謝謝。",
 "今天不問產品了，晚安。",
 "我沒有要做追蹤，只是想聊天。",
 "先不用推薦東西，謝謝，改天再說。",
 "成分今天不分析，我只是來說聲嗨。",
 "今天不記錄皮膚也不問產品，先這樣。",
 "感謝說明，我目前沒有其他需求。",
 "現在不需要你做任何事，我先休息。",
 "商品之後再說，現在想輕鬆聊聊天。",
 "好，我先消化一下剛才的資訊。",
 "目前沒有其他問題，謝謝你。",
 "保養先告一段落，我下次再來。",
 "今天只是路過打招呼，沒有保養需求。",
 "先休息一下，產品、成分和紀錄都晚點再處理。",
],
"clarify":[
 "我有個需求，但還不知道它算哪一類。",
 "我現在不確定是要買東西還是了解資訊。",
 "有件皮膚相關的事情，但細節還沒有整理。",
 "我不知道該從產品、知識還是紀錄開始。",
 "目前資訊還不完整，可以先幫我釐清方向嗎？",
 "我有一個需求，但現在還說不清楚。",
 "可能是保養知識，也可能是商品問題。",
 "我想改善肌膚，可是不知道第一步該做什麼。",
 "這件事情可以先幫我判斷方向嗎？",
 "我的問題目前還沒有整理完整。",
 "不知道是該換商品還是先了解原因。",
 "我有點困惑，還不確定真正要問的是什麼。",
 "先不要直接回答，我提供的資訊還不夠。",
 "我可能想做紀錄，也可能只是想問一個問題。",
 "目前只有很模糊的需求，需要先整理。",
 "我不知道這算產品需求還是膚況需求。",
 "想處理皮膚問題，但現在沒有足夠細節。",
 "我還不確定需要商品推薦、知識還是追蹤。",
 "現在資訊不足，我不知道應該先做哪件事。",
 "我還沒有決定是買產品、查成分或記錄膚況。",
],
}






LOCKED_GATE7_R8 = {
"product":[
 "請推薦適合{skin}的{product}。",
 "我想找{budget}元以下的{product}。",
 "想買無香料{product}，請幫我挑一款。",
 "麻煩推薦一款適合{skin}的{product}。",
 "預算最高{budget}元，請找{product}。",
 "我今天只想選一款{product}。",
 "今天先不做膚況紀錄，我只想找{product}。",
 "不用分析成分原理，直接幫我選{budget}元以下的{product}。",
 "我沒有刺痛或紅腫，現在只是想買適合{skin}的{product}。",
 "商品需求很明確：請推建無香料{product}，追蹤之後再說。",
],
"knowledge":[
 "請說明{a}的主要用途。",
 "{a}和{b}在保養上有什麼差異？",
 "為什麼{skin}需要注意保濕？",
 "請簡單解釋{a}的作用原理。",
 "我想知道{a}與{b}要怎麼搭配。",
 "乳液和乳霜的差別是什麼？",
 "商品先不用找，我只想了解{a}和{b}的差別。",
 "我已經有{product}了，不需要推薦，只想理解其中的{a}。",
 "今天先不做皮膚紀錄，我只是想研究{a}的用途。",
 "不是購物需求；如果只看保養知識，{a}與{b}最大的差異是什麼？",
],
"skin_tracking":[
 "請記錄今天鼻翼偏油。",
 "幫我更新今天兩頰偏乾的膚況。",
 "今天第{day}天，請更新肌膚紀錄。",
 "把今天額頭出油加入皮膚紀錄。",
 "請記下今天沒有刺痛、T字偏油。",
 "我想查看最近幾天的膚況追蹤。",
 "產品之後再看，現在先記錄今天鼻翼偏油。",
 "成分問題先不問，幫我把兩頰偏乾寫進今天紀錄。",
 "我沒有要買東西，這一輪只做第{day}天肌膚追蹤。",
 "雖然提到{product}，但不是要推薦；請記錄今天使用後膚況正常。",
],
"safety":[
 "我擦{a}之後一直刺痛。",
 "用了{a}後持續灼熱，還能繼續嗎？",
 "我懷孕中，{a}需要避開嗎？",
 "擦{product}後突然紅腫，現在要注意什麼？",
 "{a}和{b}一起使用後很刺激，安全嗎？",
 "皮膚已經破皮，現在可以擦{a}嗎？",
 "沒有刺痛，但眼周碰到{a}之後一直不舒服。",
 "我不是問一般用途；用了{a}後整臉持續發紅。",
 "商品不用推薦，我對香料過敏，使用{product}前需要注意什麼？",
 "最近在整理保養習慣，但眼睛周圍碰到{a}後一直不適，這次不是要做紀錄。",
],
"conversation":[
 "謝謝，今天先這樣。",
 "哈囉，我只是來打招呼。",
 "晚安，今天沒有其他問題。",
 "感謝你的回答，我先休息。",
 "今天暫時沒有保養需求。",
 "先不用處理任何事情，謝謝。",
 "商品和成分下次再談，今天只是想聊一下。",
 "我沒有要做追蹤，也不需要推薦，只是來說聲嗨。",
 "剛才的資訊我先消化，現在不需要你再做任何處理。",
 "保養、商品、紀錄今天都先停，我只是想結束這一輪對話。",
],
"clarify":[
 "我還不確定自己需要哪一種協助。",
 "不知道應該先找產品還是先了解知識。",
 "我的需求目前還很模糊，可以先幫我釐清嗎？",
 "我不知道下一步應該做什麼。",
 "可能是商品問題，也可能只是保養知識。",
 "資訊還不完整，我目前無法決定方向。",
 "我想改善肌膚，但不確定要推薦、知識還是追蹤。",
 "這件事跟皮膚有關，但我還沒有整理好細節，也不知道該從哪裡開始。",
 "我可能需要買東西，也可能只是想理解原因，目前真的還沒決定。",
 "先不要直接分派，我提供的資訊不夠，連自己要問產品、知識或紀錄都不確定。",
],
}
for _route, _rows in LOCKED_GATE7_R8.items():
    if len(_rows) != 10:
        raise RuntimeError(f"LOCKED_GATE7_R8 {_route} must have exactly 10 families")
    T[_route][10:20] = _rows






PREFIX=["","想請問，","麻煩你，","不好意思，","最近在整理保養習慣，"]
SUFFIX=[""," 謝謝。"," 請簡短回答。"," 請用繁體中文。",""]

class Progress:
    def __init__(self,label,total):
        self.label=label;self.total=max(1,total);self.n=0;self.start=time.monotonic();self.last=self.start;self.bucket=-1;self.tty=sys.stderr.isatty();self.render()
    def _ts(self,x):
        if x is None or not math.isfinite(x):return "--:--"
        x=max(0,int(x));return f"{x//60:02d}:{x%60:02d}"
    def update(self):
        self.n+=1;now=time.monotonic();pct=self.n*100/self.total
        if self.tty:
            if now-self.last>=.15 or self.n>=self.total:self.render()
        else:
            b=int(pct//2)
            if b>self.bucket or self.n>=self.total:self.render()
    def render(self):
        now=time.monotonic();elapsed=now-self.start;rate=self.n/elapsed if self.n and elapsed else 0;eta=(self.total-self.n)/rate if rate else None
        frac=min(1,self.n/self.total);w=28;fill=round(w*frac);bar="█"*fill+"░"*(w-fill)
        msg=f"[{self.label:<20}] [{bar}] {100*frac:6.2f}%  {self.n:>6}/{self.total:<6}  {rate:6.1f} req/s  elapsed {self._ts(elapsed)}  ETA {self._ts(eta)}"
        if self.tty:
            sys.stderr.write("\r"+msg+"   ")
            if self.n>=self.total:sys.stderr.write("\n")
        else:sys.stderr.write(msg+"\n")
        sys.stderr.flush();self.last=now;self.bucket=int(100*frac//2)

def perturb(t,rng):
    if rng.random()<.20:t=rng.choice(PREFIX)+t
    if rng.random()<.18:t+=rng.choice(SUFFIX)
    if rng.random()<.10:t=t.replace("，"," ").replace("。","")
    if rng.random()<.05:t=t.replace("推薦","推建")
    if rng.random()<.04:t=t.replace("什麼","甚麼")
    return t.strip()

def make(route,fam,seq,rng):
    tpl=T[route][fam];pk,p=rng.choice(PRODUCTS);sk,s=rng.choice(SKINS);ak,a=rng.choice(INGS);bk,b=rng.choice([x for x in INGS if x[0]!=ak]);budget=rng.choice(BUDGETS);day=rng.randint(1,14)
    text=perturb(tpl.format(product=p,skin=s,a=a,b=b,budget=budget,day=day),rng);slots={}
    if any(x in tpl for x in ("元以下","最高","上限","不超過")):slots["budget_twd_max"]=budget
    if "{skin}" in tpl:slots["skin_type"]=sk
    if "{product}" in tpl:slots["product_category"]=pk
    if any(x in text for x in ("無香料","不含香料","不要香精")):slots["fragrance_free"]=True
    ing=[]
    if "{a}" in tpl:ing.append(ak)
    if "{b}" in tpl:ing.append(bk)
    if ing:slots["ingredients"]=sorted(set(ing))
    difficulty="calibration" if fam<10 else ("normal" if fam<16 else "hard")
    return {"id":f"{route[:3]}-f{fam}-{seq:04d}","route":route,"family":f"{route}-f{fam}","family_index":fam,"split":"calibration" if fam<10 else "locked","difficulty":difficulty,"text":text,"gold_slots":slots}

def build(n,seed):
    rng=random.Random(seed);rows=[];prog=Progress("Synthetic build",len(ROUTES)*20*n)
    for route in ROUTES:
        for fam in range(20):
            for seq in range(n):rows.append(make(route,fam,seq,rng));prog.update()
    rng.shuffle(rows);return rows

def load_parser(path):
    spec=importlib.util.spec_from_file_location("parser_gate7_r8",path);m=importlib.util.module_from_spec(spec);sys.modules["parser_gate7_r8"]=m;spec.loader.exec_module(m);return m

def parser_eval(rows,p,label):
    prog=Progress(label,len(rows));route_n=route_ok=0;stats=defaultdict(lambda:{"n":0,"correct":0});out=[]
    mappable_n=mappable_ok=eligible_n=eligible_direct_ok=0
    for r in rows:
        pr=p.parse(r["text"]);d=pr.to_dict();h=p.deterministic_route_hint(pr);rr={**r,"parser":d,"parser_hint":h};out.append(rr)
        if h is not None:
            route_n+=1;route_ok+=int(h==r["route"])
        if r["route"] in BLINK_MAPPABLE:
            eligible_n+=1
            eligible_direct_ok+=int(h==r["route"])
        if h in BLINK_MAPPABLE:
            mappable_n+=1
            mappable_ok+=int(h==r["route"])
        for k,g in r["gold_slots"].items():
            stats[k]["n"]+=1;got=d.get(k);ok=sorted(got or [])==sorted(g) if k=="ingredients" else got==g;stats[k]["correct"]+=int(ok)
        prog.update()
    return out,{
      "n":len(rows),
      "route_hint_coverage":route_n/len(rows),
      "route_hint_precision":route_ok/route_n if route_n else None,
      "route_hint_wrong":route_n-route_ok,
      "mappable_bypass_count":mappable_n,
      "mappable_bypass_precision":mappable_ok/mappable_n if mappable_n else None,
      "mappable_bypass_wrong":mappable_n-mappable_ok,
      "eligible_support":eligible_n,
      "eligible_direct_coverage":eligible_direct_ok/eligible_n if eligible_n else None,
      "slots":{k:{**v,"accuracy":v["correct"]/v["n"]} for k,v in stats.items()}
    }

_tls=threading.local()
def hc(base):
    c=getattr(_tls,"c",None)
    if c is None:c=httpx.Client(base_url=base,timeout=90,limits=httpx.Limits(max_connections=96,max_keepalive_connections=48));_tls.c=c
    return c

def kev(base,r):
    t=time.perf_counter();resp=hc(base).post("/v1/systemone",json={"state":r["text"],"model":"kev-latest","questions":QUESTIONS});wall=(time.perf_counter()-t)*1000
    o={"id":r["id"],"family":r["family"],"split":r["split"],"difficulty":r.get("difficulty"),"gold_route":r["route"],"parser_hint":r.get("parser_hint"),"wall_ms":wall,"http_status":resp.status_code}
    if resp.status_code!=200:o["error"]=f"http_{resp.status_code}";return o
    try:
        d=resp.json();a=d["answers"];rt=a["route"];choice=str(rt["choice"]);probs={k:float(v) for k,v in rt["probabilities"].items()};conf=float(rt.get("confidence",max(probs.values())));cl=float(a["needs_clarification"]["noul"]);sa=float(a["safety_sensitive"]["noul"])
        if choice not in ROUTES or abs(sum(probs.values())-1)>.03:raise ValueError("route contract")
        if not all(math.isfinite(v) and 0<=v<=1 for v in [conf,cl,sa,*probs.values()]):raise ValueError("scalar contract")
        o.update(contract_pass=True,pred_route=choice,probabilities=probs,confidence=conf,clarification_probability=cl,safety_probability=sa,model_latency_ms=float(d["latency_ms"]) if d.get("latency_ms") is not None else None)
    except Exception as e:o.update(contract_pass=False,error=f"{type(e).__name__}:{e}")
    return o

def parallel(rows,base,workers,label):
    prog=Progress(label,len(rows));out=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs=[ex.submit(kev,base,r) for r in rows]
        for f in concurrent.futures.as_completed(futs):out.append(f.result());prog.update()
    return out

def writej(p,x):Path(p).write_text(json.dumps(x,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def writejl(p,rows):
    with open(p,"w",encoding="utf-8") as f:
        for r in rows:f.write(json.dumps(r,ensure_ascii=False,separators=(",",":"))+"\n")

def cls(rows):
    labels=list(ROUTES);cm={g:{p:0 for p in labels} for g in labels};valid=[r for r in rows if r.get("pred_route") in labels]
    for r in valid:cm[r["gold_route"]][r["pred_route"]]+=1
    f=[];detail={}
    for c in labels:
        tp=cm[c][c];fp=sum(cm[g][c] for g in labels if g!=c);fn=sum(cm[c][p] for p in labels if p!=c);pr=tp/(tp+fp) if tp+fp else 0;rc=tp/(tp+fn) if tp+fn else 0;ff=2*pr*rc/(pr+rc) if pr+rc else 0;f.append(ff);detail[c]={"precision":pr,"recall":rc,"f1":ff,"support":sum(cm[c].values())}
    return {"accuracy":sum(r["gold_route"]==r["pred_route"] for r in valid)/len(valid) if valid else 0,"macro_f1":sum(f)/len(f),"per_class":detail,"confusion_matrix":cm}

def ece(rows,bins=10):
    v=0;total=len(rows)
    for i in range(bins):
        lo=i/bins;hi=(i+1)/bins;b=[r for r in rows if lo<=r["confidence"]<(hi if i<bins-1 else hi+1e-9)]
        if b:v+=len(b)/total*abs(sum(r["confidence"] for r in b)/len(b)-sum(r["gold_route"]==r["pred_route"] for r in b)/len(b))
    return v
def brier(rows):return sum(sum((r["probabilities"].get(c,0)-(1 if c==r["gold_route"] else 0))**2 for c in ROUTES) for r in rows)/len(rows)

def integration_decision(r,policy):
    h=r.get("parser_hint")
    if h in BLINK_MAPPABLE:
        return {"covered":True,"decision":h,"source":"parser","blink_intent_type":BLINK_MAPPABLE[h]}
    if h in NON_MAPPABLE:
        return {"covered":False,"decision":None,"source":"existing_path_fallback","blink_intent_type":None}
    if not r.get("contract_pass"):
        return {"covered":False,"decision":None,"source":"kev_error","blink_intent_type":None}
    pred=r["pred_route"]
    if pred not in BLINK_MAPPABLE:
        return {"covered":False,"decision":None,"source":"existing_path_fallback","blink_intent_type":None}
    if r["clarification_probability"]>=policy["clarification_guard"] and pred!="clarify":
        return {"covered":False,"decision":None,"source":"confidence_fallback","blink_intent_type":None}
    if r["safety_probability"]>=policy["safety_guard"]:
        return {"covered":False,"decision":None,"source":"safety_fallback","blink_intent_type":None}
    if pred=="clarify" and r["clarification_probability"]<policy.get("clarify_noul_min",0.0):
        return {"covered":False,"decision":None,"source":"clarification_signal_fallback","blink_intent_type":None}
    if r["confidence"]<policy["thresholds"].get(pred,1.1):
        return {"covered":False,"decision":None,"source":"confidence_fallback","blink_intent_type":None}
    return {"covered":True,"decision":pred,"source":"kev","blink_intent_type":BLINK_MAPPABLE[pred]}

def selective(rows,policy):
    ds=[]
    for r in rows:
        d=integration_decision(r,policy);ds.append((r,d))
    covered=[x for x in ds if x[1]["covered"]]
    eligible=[x for x in ds if x[0]["gold_route"] in BLINK_MAPPABLE]
    eligible_cov=[x for x in eligible if x[1]["covered"]]
    nonmap=[x for x in ds if x[0]["gold_route"] in NON_MAPPABLE]
    wrong=[x for x in covered if x[0]["gold_route"] in NON_MAPPABLE or x[1]["decision"]!=x[0]["gold_route"]]
    per_route={}
    for route in BLINK_MAPPABLE:
        rr=[x for x in ds if x[0]["gold_route"]==route];rc=[x for x in rr if x[1]["covered"]]
        per_route[route]={"support":len(rr),"coverage":len(rc)/len(rr) if rr else None,"covered_error_rate":sum(x[1]["decision"]!=route for x in rc)/len(rc) if rc else 0.0}
    nonmappable_per_route={}
    for route in sorted(NON_MAPPABLE):
        rr=[x for x in ds if x[0]["gold_route"]==route];rc=[x for x in rr if x[1]["covered"]]
        nonmappable_per_route[route]={"support":len(rr),"bypass_count":len(rc),"bypass_rate":len(rc)/len(rr) if rr else 0.0}
    return {
      "support":len(ds),"covered_count":len(covered),"wrong_bypass_count":len(wrong),"covered_wrong_rate":len(wrong)/len(covered) if covered else 0.0,
      "global_bypass_rate":len(covered)/len(ds) if ds else 0.0,"eligible_support":len(eligible),"eligible_coverage":len(eligible_cov)/len(eligible) if eligible else 0.0,
      "eligible_covered_error_rate":sum(x[1]["decision"]!=x[0]["gold_route"] for x in eligible_cov)/len(eligible_cov) if eligible_cov else 0.0,
      "eligible_covered_accuracy":sum(x[1]["decision"]==x[0]["gold_route"] for x in eligible_cov)/len(eligible_cov) if eligible_cov else None,
      "nonmappable_support":len(nonmap),"nonmappable_bypass_count":sum(x[1]["covered"] for x in nonmap),
      "nonmappable_bypass_rate":sum(x[1]["covered"] for x in nonmap)/len(nonmap) if nonmap else 0.0,
      "fallback_rate":1-len(covered)/len(ds) if ds else 0.0,"source_counts":dict(Counter(x[1]["source"] for x in ds)),
      "per_route":per_route,"nonmappable_per_route":nonmappable_per_route}

def calibrate(cal):
    # Retry8 selects a safe selective policy. Fallback is an intended outcome.
    candidates=[]
    clarification_guards=[.30,.35,.40,.45,.50,.60,.70]
    safety_guards=[.40,.45,.50,.55,.60,.65,.70,.80]
    clarify_noul_mins=[0.0,.30,.35,.40,.45,.50,.55,.60]
    for cg in clarification_guards:
      for sg in safety_guards:
        for cmin in clarify_noul_mins:
          thresholds={};route_calibration={}
          for route in BLINK_MAPPABLE:
            base=[r for r in cal if r["parser_hint"] is None and r.get("contract_pass") and r["pred_route"]==route and r["safety_probability"]<sg and not (r["clarification_probability"]>=cg and route!="clarify") and not (route=="clarify" and r["clarification_probability"]<cmin)]
            wrong=[float(r["confidence"]) for r in base if r["gold_route"]!=route]
            threshold=(max(wrong)+0.01) if wrong else 0.05;threshold=math.ceil(threshold*100.0)/100.0
            if threshold>=1.0:threshold=1.1
            accepted=[r for r in base if r["confidence"]>=threshold];correct=sum(r["gold_route"]==route for r in accepted)
            if correct<80:threshold=1.1;accepted=[];correct=0
            thresholds[route]=threshold;route_calibration[route]={"base_support":len(base),"wrong_support":len(wrong),"threshold":threshold,"accepted":len(accepted),"accepted_correct":correct}
          policy={"thresholds":thresholds,"clarification_guard":cg,"safety_guard":sg,"clarify_noul_min":cmin};m=selective(cal,policy);candidates.append({**policy,**m,"route_calibration":route_calibration})
    a=ACCEPTANCE["calibration"];feasible=[]
    for x in candidates:
        nm=x["nonmappable_per_route"]
        if x["eligible_coverage"]<a["eligible_coverage_min"]:continue
        if x["eligible_covered_error_rate"]>a["eligible_covered_error_max"]:continue
        if x["nonmappable_bypass_rate"]>a["nonmappable_bypass_rate_max"]:continue
        if nm["safety"]["bypass_count"]>a["safety_bypass_max"]:continue
        if any(v["covered_error_rate"]>ACCEPTANCE["normal"]["route_covered_error_max"] for v in x["per_route"].values()):continue
        feasible.append(x)
    if not feasible:return None,candidates
    return sorted(feasible,key=lambda x:(x["eligible_coverage"],x["nonmappable_bypass_rate"],x["eligible_covered_error_rate"],x["safety_guard"],-x["clarify_noul_min"]))[0],candidates

def pct(xs,q):
    xs=sorted(float(x) for x in xs)
    if not xs:return None
    if len(xs)==1:return xs[0]
    h=(len(xs)-1)*q;lo=int(math.floor(h));hi=int(math.ceil(h));return xs[lo]+(h-lo)*(xs[hi]-xs[lo])

def parity(base,locked):
    unresolved=[r for r in locked if r["parser_hint"] is None][:600]
    a=parallel(unresolved,base,1,"Parity C1");b=parallel(unresolved,base,16,"Parity C16");A={r["id"]:r for r in a};B={r["id"]:r for r in b};flips=[];delta=0
    for k in A:
        x,y=A[k],B[k]
        if x.get("pred_route")!=y.get("pred_route"):flips.append(k)
        if x.get("contract_pass") and y.get("contract_pass"):
            for c in ROUTES:delta=max(delta,abs(x["probabilities"].get(c,0)-y["probabilities"].get(c,0)))
        else:delta=1
    return {"n":len(unresolved),"top_choice_flip_count":len(flips),"flip_ids":flips[:30],"max_probability_abs_delta":delta,"pass":len(flips)==0 and delta<=.05}

def perf_one(base,p,r,policy):
    t=time.perf_counter();pr=p.parse(r["text"]);h=p.deterministic_route_hint(pr)
    if h in BLINK_MAPPABLE:
        return {"gold_route":r["route"],"decision":h,"source":"parser","bypass":True,"model_call":False,"wall_ms":(time.perf_counter()-t)*1000}
    if h in NON_MAPPABLE:
        return {"gold_route":r["route"],"decision":None,"source":"existing_path_fallback","bypass":False,"model_call":False,"wall_ms":(time.perf_counter()-t)*1000}
    k=kev(base,{**r,"parser_hint":None})
    if not k.get("contract_pass"):
        return {"gold_route":r["route"],"decision":None,"source":"kev_error","bypass":False,"model_call":True,"error":k.get("error"),"wall_ms":(time.perf_counter()-t)*1000}
    d=integration_decision(k,policy)
    return {"gold_route":r["route"],"decision":d["decision"],"source":d["source"],"bypass":d["covered"],"model_call":True,"error":None,"wall_ms":(time.perf_counter()-t)*1000,"model_latency_ms":k.get("model_latency_ms")}

def perf_pool(seed,n):
    rng=random.Random(seed);out=[]
    for i in range(n):
        route=list(ROUTES)[i%len(ROUTES)];fam=(i//len(ROUTES))%20;out.append(make(route,fam,10000+i,rng))
    return out

def performance(base,p,policy,seed,out):
    concs=[1,4,8,16,32];per=1400;pool=perf_pool(seed+321,per*len(concs));summ=[]
    for j,c in enumerate(concs):
        batch=pool[j*per:(j+1)*per];prog=Progress(f"Perf C{c}",len(batch));start=time.perf_counter();rows=[]
        with concurrent.futures.ThreadPoolExecutor(max_workers=c) as ex:
            futs=[ex.submit(perf_one,base,p,r,policy) for r in batch]
            for f in concurrent.futures.as_completed(futs):rows.append(f.result());prog.update()
        wall=time.perf_counter()-start;lat=[r["wall_ms"] for r in rows];ml=[r["model_latency_ms"] for r in rows if r.get("model_latency_ms") is not None]
        s={"concurrency":c,"n":len(rows),"wall_seconds":wall,"user_rps":len(rows)/wall,"model_call_fraction":sum(r["model_call"] for r in rows)/len(rows),"bypass_rate":sum(r["bypass"] for r in rows)/len(rows),"errors":sum(bool(r.get("error")) for r in rows),"latency_ms":{"p50":pct(lat,.5),"p95":pct(lat,.95),"p99":pct(lat,.99),"max":max(lat)},"model_latency_ms":{"p50":pct(ml,.5),"p95":pct(ml,.95),"p99":pct(ml,.99),"max":max(ml) if ml else None},"source_counts":dict(Counter(r["source"] for r in rows))}
        summ.append(s);writej(Path(out)/f"perf-c{c}.summary.json",s);writejl(Path(out)/f"perf-c{c}.jsonl",rows)
    writej(Path(out)/"performance-summary.json",summ);return summ

def validate_contract(source,out):
    p=Path(source);text=p.read_text(encoding="utf-8",errors="replace");tree=ast.parse(text);found={}
    for node in tree.body:
        targets=node.targets if isinstance(node,ast.Assign) else [node.target] if isinstance(node,ast.AnnAssign) else []
        for t in targets:
            if isinstance(t,ast.Name) and t.id in {"SYSTEM_PROMPT","INTENT_JSON_SCHEMA"}:found[t.id]=ast.literal_eval(node.value)
    prompt=found.get("SYSTEM_PROMPT");schema=found.get("INTENT_JSON_SCHEMA")
    if not isinstance(prompt,str) or not isinstance(schema,dict):raise RuntimeError("static intent contract unavailable")
    props=schema.get("properties") or {};req=schema.get("required") or [];enum=(props.get("intent_type") or {}).get("enum") or []
    result={"source":str(p),"source_sha256":hashlib.sha256(p.read_bytes()).hexdigest(),"system_prompt_sha256":hashlib.sha256(prompt.encode()).hexdigest(),"schema_sha256":hashlib.sha256(json.dumps(schema,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),"properties":sorted(props),"required":req,"intent_enum":enum,"mapping":BLINK_MAPPABLE,"pass":("raw_query" in props and "raw_query" in req and "intent_type" in props and "intent_type" in req and all(x in enum for x in BLINK_MAPPABLE.values()))}
    writej(out,result);return result

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--base-url");ap.add_argument("--out",required=True);ap.add_argument("--parser",required=True);ap.add_argument("--intent-source",required=True);ap.add_argument("--n-per-family",type=int,default=320);ap.add_argument("--seed",type=int,default=20260927);ap.add_argument("--parser-only",action="store_true");a=ap.parse_args()
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True);p=load_parser(Path(a.parser));writej(out/"acceptance-policy.json",ACCEPTANCE);contract=validate_contract(a.intent_source,out/"intent-contract.json")
    if not contract["pass"]:
        writej(out/"GATE_STATUS.json",{"gate":7,"retry":8,"status":"SHADOW_CONTRACT_NOT_READY","mandatory_gate":True,"advance_allowed":False,"next_instruction":"STOP_AND_RETURN_GATE7_R8_EVIDENCE"});raise SystemExit(9)
    rows=build(a.n_per_family,a.seed);enriched,pm=parser_eval(rows,p,"Parser evaluation");writejl(out/"dataset-gate7-r8.jsonl",enriched);writej(out/"parser-gate7-r8-metrics.json",pm)
    locked_en=[r for r in enriched if r["split"]=="locked"];_,pml=parser_eval(locked_en,p,"Parser locked");writej(out/"parser-gate7-r8-locked-metrics.json",pml)
    if (pml["mappable_bypass_precision"] or 0)<.995 or any(v["accuracy"]<.99 for v in pml["slots"].values()):
        writej(out/"GATE_STATUS.json",{"gate":7,"retry":8,"status":"PARSER_NOT_QUALIFIED","mandatory_gate":True,"advance_allowed":False,"parser_locked":pml,"next_instruction":"STOP_AND_RETURN_GATE7_R8_EVIDENCE"});raise SystemExit(10)
    if a.parser_only:
        writej(out/"PARSER_PREFLIGHT_PASS.json",{"status":"PASS","parser_locked":pml,"intent_contract":contract})
        print(json.dumps({"status":"PASS","parser_locked":pml,"intent_contract":contract},ensure_ascii=False,indent=2))
        raise SystemExit(0)
    if not a.base_url:
        raise SystemExit("--base-url required unless --parser-only")
    preds=parallel(enriched,a.base_url,8,"Kev quality");writejl(out/"kev-gate7-r8-predictions.jsonl",preds)
    bad=[r for r in preds if not r.get("contract_pass")]
    if bad:writej(out/"GATE_STATUS.json",{"gate":7,"retry":8,"status":"KEV_CONTRACT_FAILURE","mandatory_gate":True,"advance_allowed":False,"count":len(bad),"next_instruction":"STOP_AND_RETURN_GATE7_R8_EVIDENCE"});raise SystemExit(11)
    cal=[r for r in preds if r["split"]=="calibration"];locked=[r for r in preds if r["split"]=="locked"]
    raw={"calibration":{**cls(cal),"ece":ece(cal),"brier":brier(cal)},"locked":{**cls(locked),"ece":ece(locked),"brier":brier(locked)}};writej(out/"kev-gate7-r8-raw-quality.json",raw)
    policy,grid=calibrate(cal);writej(out/"policy-gate7-r8-calibration.json",{"selected":policy,"candidates":grid})
    if policy is None:writej(out/"GATE_STATUS.json",{"gate":7,"retry":8,"status":"MODEL_QUALITY_NOT_QUALIFIED","mandatory_gate":True,"advance_allowed":False,"reason":"NO_SAFE_MAPPABLE_POLICY","next_instruction":"STOP_AND_RETURN_GATE7_R8_EVIDENCE"});raise SystemExit(12)
    locked_h=selective(locked,policy);writej(out/"locked-integration-gate7-r8-quality.json",locked_h)
    normal=[r for r in locked if r.get("difficulty")=="normal"];hard=[r for r in locked if r.get("difficulty")=="hard"]
    normal_h=selective(normal,policy);hard_h=selective(hard,policy)
    writej(out/"normal-locked-integration-gate7-r8-quality.json",normal_h);writej(out/"hard-locked-integration-gate7-r8-quality.json",hard_h)
    par=parity(a.base_url,locked_en);writej(out/"concurrency-parity-gate7-r8.json",par);perf=performance(a.base_url,p,policy,a.seed,out)
    c8=next(x for x in perf if x["concurrency"]==8);c16=next(x for x in perf if x["concurrency"]==16)
    na=ACCEPTANCE["normal"];ha=ACCEPTANCE["hard"];pa=ACCEPTANCE["parser"];pfa=ACCEPTANCE["performance"];nmn=normal_h["nonmappable_per_route"];hmn=hard_h["nonmappable_per_route"]
    checks={
      "parser_mappable_bypass_precision":(pml["mappable_bypass_precision"] or 0)>=pa["mappable_bypass_precision_min"],
      "parser_slots":all(v["accuracy"]>=pa["slot_accuracy_min"] for v in pml["slots"].values()),
      "normal_eligible_coverage":normal_h["eligible_coverage"]>=na["eligible_coverage_min"],
      "normal_eligible_error":normal_h["eligible_covered_error_rate"]<=na["eligible_covered_error_max"],
      "normal_route_coverage":all(normal_h["per_route"][r]["coverage"]>=na["route_coverage_min"][r] for r in BLINK_MAPPABLE),
      "normal_route_error":all(normal_h["per_route"][r]["covered_error_rate"]<=na["route_covered_error_max"] for r in BLINK_MAPPABLE),
      "normal_safety_zero":nmn["safety"]["bypass_count"]<=na["safety_bypass_max"],
      "normal_tracking_risk":nmn["skin_tracking"]["bypass_rate"]<=na["skin_tracking_bypass_rate_max"],
      "normal_conversation_risk":nmn["conversation"]["bypass_rate"]<=na["conversation_bypass_rate_max"],
      "hard_selective_error":hard_h["covered_wrong_rate"]<=ha["covered_wrong_rate_max"],
      "hard_mappable_error":hard_h["eligible_covered_error_rate"]<=ha["eligible_covered_error_max"],
      "hard_safety_zero":hmn["safety"]["bypass_count"]<=ha["safety_bypass_max"],
      "hard_tracking_risk":hmn["skin_tracking"]["bypass_rate"]<=ha["non_safety_nonmappable_bypass_rate_max"],
      "hard_conversation_risk":hmn["conversation"]["bypass_rate"]<=ha["non_safety_nonmappable_bypass_rate_max"],
      "parity":par["pass"],"c8":c8["errors"]==0 and c8["latency_ms"]["p95"]<=pfa["c8_p95_ms_max"],"c16":c16["errors"]==0 and c16["latency_ms"]["p95"]<=pfa["c16_p95_ms_max"],"intent_contract":contract["pass"]}
    if not checks["parser_mappable_bypass_precision"] or not checks["parser_slots"]:status="PARSER_NOT_QUALIFIED"
    elif not all(checks[k] for k in ("normal_eligible_coverage","normal_eligible_error","normal_route_coverage","normal_route_error","normal_safety_zero","normal_tracking_risk","normal_conversation_risk","hard_selective_error","hard_mappable_error","hard_safety_zero","hard_tracking_risk","hard_conversation_risk")):status="MODEL_QUALITY_NOT_QUALIFIED"
    elif not checks["parity"]:status="CONCURRENCY_PARITY_NOT_QUALIFIED"
    elif not checks["c8"] or not checks["c16"]:status="PERFORMANCE_NOT_QUALIFIED"
    elif not checks["intent_contract"]:status="SHADOW_CONTRACT_NOT_READY"
    else:status="QUALIFIED_FOR_FULL_SHADOW_AB_IMPLEMENTATION"
    result={"gate":7,"retry":8,"status":status,"scope":"GATE7_R8_MAPPABILITY_AWARE_SYSTEM_ONE_QUALIFICATION","mandatory_gate":True,"advance_allowed":status=="QUALIFIED_FOR_FULL_SHADOW_AB_IMPLEMENTATION","checks":checks,"acceptance":ACCEPTANCE,"dataset":{"total":len(rows),"calibration":len(cal),"locked":len(locked),"normal_locked":len(normal),"hard_locked":len(hard),"families_per_route":20,"normal_family_indices":[10,11,12,13,14,15],"hard_family_indices":[16,17,18,19],"note":"Retry8 separates normal selective coverage from hard-sample risk. Hard fallback is acceptable and has no minimum coverage."},"blink_mappable_routes":BLINK_MAPPABLE,"non_mappable_routes":sorted(NON_MAPPABLE),"parser_locked":pml,"kev_raw_locked":{"accuracy":raw["locked"]["accuracy"],"macro_f1":raw["locked"]["macro_f1"],"ece":raw["locked"]["ece"],"brier":raw["locked"]["brier"],"per_class":raw["locked"]["per_class"]},"selected_policy":policy,"locked_integration":locked_h,"normal_locked_integration":normal_h,"hard_locked_integration":hard_h,"concurrency_parity":par,"performance":perf,"intent_contract":contract,"next_instruction":"STOP_AND_RETURN_GATE7_R8_EVIDENCE"}
    writej(out/"GATE_STATUS.json",result);print(json.dumps(result,ensure_ascii=False,indent=2));raise SystemExit(0 if status=="QUALIFIED_FOR_FULL_SHADOW_AB_IMPLEMENTATION" else 10)

if __name__=="__main__":main()




