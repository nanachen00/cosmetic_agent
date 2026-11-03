"""Experimental visual diary contract; no disease diagnosis or automatic save."""
from __future__ import annotations
import json
from pathlib import Path

TAGS = {'acne_like_bumps', 'redness', 'dark_marks', 'flaking'}
REGIONS = {'forehead', 'cheek', 'nose', 'chin', 'eye_area', 'unknown'}
SCOPES = {'face', 'skin_closeup', 'non_skin', 'uncertain'}
SYSTEM = '''你是保養日記的保守影像觀察助手。只描述照片可見的外觀，不診斷疾病、
不推薦藥物，不推測水分百分比、過敏原、膚質、嚴重程度、精確病灶數量或左右側。
使用者補充只是背景，不能代替照片證據。無把握的特徵不要列出。
只輸出一個 JSON 物件，恰好包含 image_scope、observations、limitations、follow_up_question。
image_scope 必須從以下清單選擇一個字串：face、skin_closeup、non_skin、uncertain。
observations 是陣列，每項恰好包含 tag、region、evidence 三個欄位。
tag 每項只選一個字串：acne_like_bumps、redness、dark_marks、flaking。
region 每項只選一個字串：forehead、cheek、nose、chin、eye_area、unknown。
evidence 填繁體中文的具體可見證據，不能省略。不同特徵分成不同項目。
limitations 填繁體中文限制的陣列。皮膚照片至少說明光線與影像品質會影響判讀。
follow_up_question 填一個繁體中文問題，或空字串。
非皮膚物件用 non_skin；看不清楚用 uncertain；兩者 observations 都必須為空陣列。
空觀察不代表已確認皮膚正常。不要照抄選項清單或以分隔符連接多個選項。
非皮膚物件的格式示例：
{"image_scope":"non_skin","observations":[],"limitations":["此照片無法用於觀察膚況。"],"follow_up_question":"可以提供清楚的皮膚照片嗎？"}
'''

def validate(v):
    if not isinstance(v, dict) or set(v) != {'image_scope','observations','limitations','follow_up_question'}:
        raise ValueError('Unexpected output fields')
    if v['image_scope'] not in SCOPES:
        raise ValueError('Invalid image scope')
    if not isinstance(v['observations'], list) or len(v['observations']) > 12:
        raise ValueError('Invalid observations')
    for o in v['observations']:
        if not isinstance(o, dict) or set(o) != {'tag','region','evidence'}:
            raise ValueError('Unexpected finding fields')
        if o['tag'] not in TAGS or o['region'] not in REGIONS:
            raise ValueError('Unsupported tag/region')
        if not isinstance(o['evidence'], str) or not 1 <= len(o['evidence']) <= 240:
            raise ValueError('Invalid visible evidence')
    lim = v['limitations']
    if not isinstance(lim, list) or len(lim)>8 or any(not isinstance(x,str) or not 1<=len(x)<=240 for x in lim):
        raise ValueError('Invalid limitations')
    if not isinstance(v['follow_up_question'],str) or len(v['follow_up_question'])>240:
        raise ValueError('Invalid question')
    if v['image_scope'] in {'non_skin','uncertain'} and v['observations']:
        raise ValueError('Rejected image cannot yield skin findings')
    if v['image_scope'] in {'face','skin_closeup'} and not lim:
        raise ValueError('Skin observations must retain limitations')
    return v

def parse(text):
    text=text.strip()
    if text.startswith('```json') and text.endswith('```'):
        text=text[7:-3].strip()
    return validate(json.loads(text))

def envelope(v, quality):
    v=validate(v)
    return dict(status='needs_confirmation' if v['image_scope'] in {'face','skin_closeup'} else 'needs_retake',
                source='experimental_vlm', requires_user_confirmation=True, eligible_for_auto_save=False,
                severity=None, observation=v, quality=quality)

def load_rows(path):
    path=Path(path).resolve(); rows=[]
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip(): continue
        r=json.loads(line)
        for k in ('id','subject_id','image','source','license','label_origin'):
            if not isinstance(r.get(k),str) or not r[k].strip(): raise ValueError('Missing '+k)
        if r.get('usage_permitted') is not True: raise ValueError('Confirm permitted data use first')
        image=Path(r['image']);r['image']=str(image if image.is_absolute() else (path.parent/image).resolve())
        if not Path(r['image']).is_file(): raise ValueError('Image missing: '+r['id'])
        validate(r['target']);rows.append(r)
    if not rows or len({r['id'] for r in rows})!=len(rows): raise ValueError('Empty manifest or duplicate IDs')
    return rows
