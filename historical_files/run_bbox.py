import logging, asyncio, json, pathlib
logging.basicConfig(level=20, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
from backend.ai_provider import NotebookLMProvider
async def main():
    p=pathlib.Path("input_pdfs/2025allproblems_1.pdf")
    if not p.exists():
        p=pathlib.Path("extraction_chatter/input_pdfs/2025allproblems_1.pdf")
    prov=NotebookLMProvider(profile="Slave 9")
    r=await prov.extract_document_questions(p, chunk_size=1000)
    out=pathlib.Path("output/whole_2025_bbox.json")
    if not out.parent.exists():
        out=pathlib.Path("extraction_chatter/output/whole_2025_bbox.json")
    out.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"EXTRACTED {len(r)}")
    with_bbox=sum(1 for q in r if "[" in str(q.get("diagram_info",{})))
    print(f"with_bbox_hint {with_bbox}")
    for q in r:
        if q.get("has_visual_diagram"):
            hint=q['diagram_info']['question']['hint']
            print(f"Q{q['question_number']} hint={hint!r}")
asyncio.run(main())
