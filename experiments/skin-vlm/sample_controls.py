"""Synthetic non-skin controls for engineering tests, never clinical labels."""
import json
from pathlib import Path
from PIL import Image,ImageDraw

def create(root):
    root=Path(root);data=root/'data';data.mkdir(parents=True,exist_ok=True)
    chart=Image.new('RGB',(512,512),'white');draw=ImageDraw.Draw(chart)
    colors=['#2176ae','#ef8354','#45b69c','#6a4c93']
    for i,color in enumerate(colors): draw.rectangle((40+i*110,100,115+i*110,400),fill=color)
    draw.text((50,30),'COLOR CHART - NOT A FACE',fill='black')
    chart.save(data/'non_skin_control.png')
    Image.new('RGB',(512,512),'#808080').save(data/'low_contrast_control.png')
    row={'id':'synthetic-control','subject_id':'synthetic-only','image':'non_skin_control.png',
         'source':'Generated geometric color chart','license':'CC0-1.0','usage_permitted':True,
         'label_origin':'synthetic_control','target':{'image_scope':'non_skin','observations':[],
         'limitations':['這是色卡，無法用於觀察膚況。'],'follow_up_question':'可以提供清楚的臉部或皮膚照片嗎？'}}
    (data/'smoke.jsonl').write_text(json.dumps(row,ensure_ascii=False)+'\n')

if __name__=='__main__': create(Path(__file__).resolve().parent)
