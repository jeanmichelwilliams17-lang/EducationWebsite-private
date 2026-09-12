# Diagram Finder — End-to-End Flowchart (Steps 3-6 of workflowintext.txt)

> [!NOTE]
> Sister to `extraction_chatter` steps 1-2. Human-in-loop diagram capture + 10-per-chat bundling + 5-per-chat recheck.

```mermaid
flowchart TD
    classDef inputNode fill:#1e293b,stroke:#64748b,stroke-width:2px,color:#f8fafc;
    classDef processNode fill:#0f172a,stroke:#6366f1,stroke-width:2px,color:#f8fafc;
    classDef aiNode fill:#312e81,stroke:#a855f7,stroke-width:2px,color:#f8fafc;
    classDef humanNode fill:#78350f,stroke:#f59e0b,stroke-width:2px,color:#f8fafc;
    classDef successNode fill:#064e3b,stroke:#10b981,stroke-width:2px,color:#f8fafc;
    classDef gateNode fill:#1e1e38,stroke:#f59e0b,stroke-width:2px,color:#f8fafc;
    classDef reviewNode fill:#7f1d1d,stroke:#ef4444,stroke-width:2px,color:#f8fafc;

    subgraph INPUT ["From extraction_chatter Step 2"]
        A1["JSON with jobuuid/questionuuid/subpartuuid + diagram_info hint+bbox+page + Q numbers"]:::inputNode
        A2["Original whole PDF"]:::inputNode
    end

    subgraph STEP3 ["3. Human Screenshot (failbetter pattern)"]
        A1 --> B1["UI lists expected images: jobuuid_questionuuid_subpartuuid_question_1 / _answer_A + hint + Q+subpart + page"]:::humanNode
        B1 --> B2["Human screenshots PDF via file-drop (failbetterscreenshoter work/frames) auto-named"]:::humanNode
        B2 --> B3["Saved output/diagrams/*.png with exact names"]:::successNode
        A2 -.-> B2
    end

    subgraph STEP4 ["4. Bundled Rewording 10/Chat (Different Accounts)"]
        B3 --> C1["Bundle 10 Qs: whole PDF + images for those 10 + hint + raw_stem + question_number + extracted fields"]:::processNode
        C1 --> C2["MultiProfilePoolManager: 0-10→Slave9, 11-20→Slave8, 21-30→Slave7..."]:::aiNode
        C2 --> C3["NotebookLM reword_and_clean_batch 10/chat → possible_error_flag + reason"]:::aiNode
        C3 --> C4["validator.py KaTeX gate"]:::gateNode
    end

    subgraph STEP5 ["5. Flagged Recheck 5/Chat"]
        C4 -- valid --> D1["valid → store"]:::successNode
        C4 -- flagged + reason --> D2["Collect flagged → 5 per chat to another notebook: whole PDF + images + hint + Q text/number + extracted + reason"]:::aiNode
        D2 --> D3["refine_with_critique original_doc_excerpt + image_hints + reason → fix"]:::aiNode
    end

    subgraph STEP6 ["6. Second Validation + Human"]
        D3 --> E1["Validate only flagged (same validator)"]:::gateNode
        E1 -- valid --> D1
        E1 -- still flagged --> E2["needs_human_review=True → Human Review"]:::reviewNode
        E2 --> F1["Export ZIP + manifest → diagram_finder/output"]:::successNode
        D1 --> F1
    end
```
