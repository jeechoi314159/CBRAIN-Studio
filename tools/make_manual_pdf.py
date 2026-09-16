#!/usr/bin/env python3
"""Render the Korean Markdown manuals as one bookmarked, printable PDF."""
import argparse
import html
from pathlib import Path
import re
import textwrap

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                                Image, PageBreak, KeepTogether, XPreformatted)

ROOT=Path(__file__).resolve().parents[1]
BLUE=colors.HexColor('#285b83')
GRAY=colors.HexColor('#586879')


class ManualDoc(SimpleDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name in ('title','heading'):
            text=flowable.getPlainText()
            key='section-'+str(len(self.headings))
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text,key,level=0)
            self.headings.append(dict(title=text,page=self.page))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--regular-font',type=Path,default=Path.home()/'Library/Fonts/NanumSquareR.ttf')
    parser.add_argument('--bold-font',type=Path,default=Path.home()/'Library/Fonts/NanumSquare_acB.ttf')
    parser.add_argument('--code-font',type=Path,default=Path('/System/Library/Fonts/Supplemental/Courier New.ttf'))
    parser.add_argument('--output',type=Path,default=ROOT/'output/pdf/CBRAIN_Manual_KO.pdf')
    args=parser.parse_args()
    for label,path in [('KR',args.regular_font),('KRB',args.bold_font),('Code',args.code_font)]:
        if not path.is_file():raise SystemExit(f'Provide a Korean TrueType font with --regular-font / --bold-font: {path}')
        pdfmetrics.registerFont(TTFont(label,str(path)))
    pdfmetrics.registerFontFamily('KR',normal='KR',bold='KRB',italic='KR',boldItalic='KRB')
    styles={
        'body':ParagraphStyle('body',fontName='KR',fontSize=10.5,leading=16.1,textColor=colors.HexColor('#263343'),spaceAfter=7,wordWrap='CJK'),
        'title':ParagraphStyle('title',fontName='KRB',fontSize=25,leading=34,textColor=BLUE,spaceAfter=18,keepWithNext=True,wordWrap='CJK'),
        'heading':ParagraphStyle('heading',fontName='KRB',fontSize=19,leading=27,textColor=BLUE,spaceAfter=15,keepWithNext=True,wordWrap='CJK'),
        'sub':ParagraphStyle('sub',fontName='KRB',fontSize=12,leading=18,textColor=BLUE,spaceBefore=8,spaceAfter=8,keepWithNext=True,wordWrap='CJK'),
        'cell':ParagraphStyle('cell',fontName='KR',fontSize=9.5,leading=14,textColor=colors.HexColor('#263343'),wordWrap='CJK'),
        'caption':ParagraphStyle('caption',fontName='KR',fontSize=8.7,leading=13,textColor=GRAY,spaceAfter=10,wordWrap='CJK'),
        'code':ParagraphStyle('code',fontName='Code',fontSize=8.7,leading=13,textColor=colors.HexColor('#344c64'),backColor=colors.HexColor('#eef2f6'),borderPadding=9,spaceBefore=4,spaceAfter=13),
    }
    def inline(text):
        text=text.replace('µ','μ').replace('⌘⇧.', 'Command+Shift+.').replace('⌘','Command').replace('⇧','Shift')
        text=html.escape(text)
        text=re.sub(r'`([^`]+)`',r'<font color="#285b83">\1</font>',text)
        def link(m):
            label,url=m.groups()
            return f'<link href="{url}" color="#285b83"><u>{label}</u></link>' if url.startswith('https://') else f'<font color="#285b83">{label}</font>'
        text=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',link,text)
        text=re.sub(r'\*\*(.+?)\*\*',r'<b>\1</b>',text)
        return text.replace('–','-').replace('—','-').replace('−','-')
    story=[]
    source_paths=[ROOT/'docs/USER_MANUAL_KO.md',ROOT/'docs/GITHUB_PUBLISH_KO.md']
    for source in source_paths:
        if story:story.append(PageBreak())
        lines=source.read_text().splitlines();i=0
        while i<len(lines):
            line=lines[i].strip()
            if not line:i+=1;continue
            if line.startswith('```'):
                block=[];i+=1
                while i<len(lines) and not lines[i].startswith('```'):
                    block.extend(textwrap.wrap(lines[i],width=88,replace_whitespace=False,drop_whitespace=False) or [''])
                    i+=1
                code=html.escape('\n'.join(block))
                code=re.sub(r'([^\x00-\x7f]+)',r'<font name="KR">\1</font>',code)
                story.append(XPreformatted(code,styles['code']));i+=1;continue
            if line.startswith('|'):
                rows=[]
                while i<len(lines) and lines[i].strip().startswith('|'):
                    row=[c.strip() for c in lines[i].strip().strip('|').split('|')]
                    if not all(re.fullmatch(r'[-: ]+',c) for c in row):rows.append(row)
                    i+=1
                count=len(rows[0]);width=499.28
                if count==2:widths=[width*.32,width*.68]
                else:widths=[width*.24,width*.45,width*.31]
                if source.name=='USER_MANUAL_KO.md' and rows[0][0]=='순서':widths=[width*.13,width*.43,width*.44]
                cells=[[Paragraph(inline(('**'+v+'**') if ri==0 else v),styles['cell']) for v in row] for ri,row in enumerate(rows)]
                table=Table(cells,colWidths=widths,repeatRows=1,hAlign='LEFT')
                table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#dfe8f0')),
                    ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f4f6f8')]),
                    ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),
                    ('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),7),
                    ('BOTTOMPADDING',(0,0),(-1,-1),7),('LINEBELOW',(0,0),(-1,0),.6,colors.HexColor('#b7c9d9'))]))
                story.extend([table,Spacer(1,12)]);continue
            image_match=re.fullmatch(r'!\[([^\]]*)\]\(([^)]+)\)',line)
            if image_match:
                caption,path=image_match.groups()
                img=Image(str(source.parent/path))
                factor=min(499.28/img.imageWidth,235/img.imageHeight)
                img.drawWidth=img.imageWidth*factor;img.drawHeight=img.imageHeight*factor
                img.hAlign='LEFT'
                story.append(KeepTogether([img,Spacer(1,5),Paragraph(inline(caption),styles['caption'])]))
                i+=1;continue
            if line.startswith('# '):style='title';line=line[2:]
            elif line.startswith('## '):
                if not (source.name=='GITHUB_PUBLISH_KO.md' and line.startswith('## A.')):story.append(PageBreak())
                style='heading';line=line[3:]
            elif line.startswith('### '):style='sub';line=line[4:]
            else:style='body'
            # Every numbered instruction is a separate, indivisible paragraph.
            if line.startswith('- [ ] '):line='[  ] '+line[6:]
            elif line.startswith('- '):line='• '+line[2:]
            story.append(Paragraph(inline(line),styles[style]));i+=1
    args.output.parent.mkdir(parents=True,exist_ok=True)
    doc=ManualDoc(str(args.output),pagesize=A4,rightMargin=48,leftMargin=48,topMargin=55,bottomMargin=44,
                  title='CBRAIN Studio 2.1.0 - 사용 및 GitHub 게시 매뉴얼',author='CBRAIN',pageCompression=1)
    doc.headings=[]
    def footer(canvas,doc):
        canvas.setStrokeColor(colors.HexColor('#cbd6df'));canvas.setLineWidth(.5)
        canvas.line(48,37,A4[0]-48,37)
        canvas.setFont('KR',8);canvas.setFillColor(GRAY)
        canvas.drawString(48,24,'CBRAIN 2.1.0  |  2026-09-16  |  사용 · 게시 매뉴얼')
        canvas.drawRightString(A4[0]-48,24,str(doc.page))
        canvas.setFont('KRB',8);canvas.setFillColor(BLUE)
        canvas.drawString(48,A4[1]-31,'CBRAIN  /  LAB GUIDE')
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    import json
    qa=ROOT/'tmp/pdfs';qa.mkdir(parents=True,exist_ok=True)
    (qa/'manual-sections.json').write_text(json.dumps(doc.headings,ensure_ascii=False,indent=2)+'\n')
    print(args.output)
    for row in doc.headings:print(row['page'],row['title'])


if __name__=='__main__':main()
