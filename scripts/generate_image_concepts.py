#!/usr/bin/env python3
from __future__ import annotations
import json, os, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from google import genai

STATE=Path("automation_state")
APPROVED=STATE/"approved_story.json"
PACKAGE=STATE/"design_options.json"
MODEL=os.getenv("GEMINI_TEXT_MODEL","gemini-2.5-flash")

def text(*xs: Any)->str:
    for x in xs:
        if isinstance(x,str) and x.strip(): return x.strip()
    return ""

def clean_json(s:str)->str:
    s=s.strip()
    s=re.sub(r"^```(?:json)?\s*","",s,flags=re.I)
    s=re.sub(r"\s*```$","",s)
    return s.strip()

def main():
    if not APPROVED.exists(): raise FileNotFoundError(APPROVED)
    a=json.loads(APPROVED.read_text(encoding="utf-8"))
    if a.get("state")!="APPROVED_STORY":
        raise RuntimeError(f"Expected APPROVED_STORY, got {a.get('state')!r}")
    issue=text(a.get("issue_date"),a.get("date"))
    story=a.get("recommended_story") if isinstance(a.get("recommended_story"),dict) else {}
    story_title=text(story.get("title"),a.get("title"),a.get("en_copy"))
    story_summary=text(a.get("jp_copy"),a.get("en_copy"),story.get("summary"),story.get("description"))
    source=text(story.get("source"),a.get("source"),a.get("source_name"))
    client=genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    schema={
      "image_concepts":[
        {"number":1,"title_ja":"...","title_en":"...","concept_ja":"...","concept_en":"...","visual_direction":"..."},
        {"number":2,"title_ja":"...","title_en":"...","concept_ja":"...","concept_en":"...","visual_direction":"..."},
        {"number":3,"title_ja":"...","title_en":"...","concept_ja":"...","concept_en":"...","visual_direction":"..."}
      ],
      "title_ideas":[
        {"number":1,"title":"...","meaning_ja":"..."},
        {"number":2,"title":"...","meaning_ja":"..."},
        {"number":3,"title":"...","meaning_ja":"..."}
      ]
    }
    prompt=f"""
You are the art director and headline writer for The Daily Duck.
Create EXACTLY 3 distinct image concepts and EXACTLY 3 catchy English title ideas for the approved story.

TITLE RULES:
- The title must feel unmistakably The Daily Duck.
- Prefer clever duck wordplay such as QUACK, DUCK, WADDLE, BILL, FEATHER, POND when it fits naturally.
- Short, punchy, memorable, suitable for a large social-card headline.
- Do not merely repeat the news headline.
- Example quality bar: "QUACKSTRONAUT", "DODO DNA? QUACKING AMAZING!"
- No misleading factual claims.

IMAGE RULES:
- Three meaningfully different visual directions.
- Premium, cheerful editorial imagery; simple rather than overly vintage.
- No text, logos or watermarks inside the generated hero image.
- The final X card is composed separately at 1500x1200 (5:4).

Return ONLY valid JSON matching this shape:
{json.dumps(schema,ensure_ascii=False,indent=2)}

APPROVED STORY
Date: {issue}
Headline: {story_title}
Summary: {story_summary}
Source: {source}
"""
    r=client.models.generate_content(model=MODEL,contents=prompt.strip())
    raw=getattr(r,"text","")
    data=json.loads(clean_json(raw))
    if len(data.get("image_concepts",[]))!=3 or len(data.get("title_ideas",[]))!=3:
        raise ValueError("Gemini must return exactly 3 image concepts and 3 title ideas.")
    for i,x in enumerate(data["image_concepts"],1): x["number"]=i
    for i,x in enumerate(data["title_ideas"],1): x["number"]=i
    package={
      "state":"WAITING_DESIGN_SELECTION","issue_date":issue,
      "created_at":datetime.now(timezone.utc).isoformat(),
      "approved_story":a,
      "image_concepts":data["image_concepts"],
      "title_ideas":data["title_ideas"],
      "approval_format":"<image 1-3> <title 1-3>"
    }
    STATE.mkdir(parents=True,exist_ok=True)
    PACKAGE.write_text(json.dumps(package,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("Created 3 image concepts + 3 title ideas.")
    print("STATE: WAITING_DESIGN_SELECTION")

if __name__=="__main__": main()
