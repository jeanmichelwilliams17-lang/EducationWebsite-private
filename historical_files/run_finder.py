import argparse, asyncio, pathlib, json
from backend.diagram_finder import find_diagrams

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", default="./input_pdfs")
    ap.add_argument("--out-dir", default="./output/diagrams")
    ap.add_argument("--profile", default="Slave 9")
    ap.add_argument("--pdf", default=None)
    args = ap.parse_args()
    pdfs = [pathlib.Path(args.pdf)] if args.pdf else list(pathlib.Path(args.input_dir).glob("*.pdf"))
    for pdf in pdfs:
        out = pathlib.Path(args.out_dir) / pdf.stem
        print(f"Finding diagrams in {pdf} -> {out}")
        manifest = asyncio.run(find_diagrams(pdf, out, profile=args.profile))
        print(f"Done {len(manifest)} diagrams")
        for m in manifest:
            print(f" {m['file']} p{m['page']} {m['hint'][:50]!r} {m['method']}")
