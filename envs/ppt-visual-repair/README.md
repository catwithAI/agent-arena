# PPT Human-Taste Alignment

This env evaluates presentation usability and whether an agent's design taste aligns with mature human preferences. The two axes are scored separately so aesthetic quality cannot hide occlusion, cropping, missing content, or broken pages.

The tested agent receives `draft.pptx`. The deck is valid and editable; it contains intentional visual-quality issues rather than file corruption. The required output is:

- `polished.pptx`

`design_notes.md` is optional and is never required for a high score. The MCP tool `annotate_pptx` can create `draft_annotated.pptx` and `object_manifest.json` as diagnostic aids.

The tested agent is explicitly required to render `draft.pptx` to PNG and inspect it visually before editing, then render `polished.pptx` again for a before/after visual check. OOXML, text, or object-property inspection alone does not satisfy the task instructions.

## Evaluation model

The hidden human-designed deck is a quality anchor, not a pixel-perfect answer. Multiple visual solutions are valid. The judge compares:

1. `REFERENCE`: hidden human design used to calibrate professional quality.
2. `DRAFT`: the intentionally weak visual draft.
3. `CANDIDATE`: the agent's polished result.

The env includes the single-slide material sets `ppt_0003`, `ppt_0005`, and `ppt_0007`. Multi-page source cases are intentionally excluded so the current judge evaluates one complete slide at a time.

Scoring dimensions:

- `artifact_contract` (10%): `polished.pptx` exists and is valid OOXML.
- `office_render` (10%): all three decks render to PNG previews through LibreOffice.
- `llm_visual_judge` (80%): a multimodal judge scores four usability dimensions and six taste dimensions, including image position/rotation, visual center, left-right density, and functional whitespace. Shrinking text without correcting an unbalanced image placement cannot receive a high composition score or direct acceptance.

Opening and re-saving the draft does not count as visual improvement. The judge caps submissions with no meaningful visual change, submissions that humans would not prefer over the draft, and submissions that lose content.

The judge calls the Anthropic Messages API directly (multimodal request with
the three PNG previews attached as image blocks — no external agent session).
Config is read from `agentlane.yaml`:

```yaml
ppt_visual_repair:
  judge:
    api_key: ""      # defaults to $ANTHROPIC_API_KEY
    base_url: ""     # defaults to https://api.anthropic.com/v1/messages
    model: ""        # defaults to the current Claude model
    timeout: 300
```

Environment variables override YAML: `PPT_JUDGE_API_KEY`, `PPT_JUDGE_BASE_URL`, `PPT_JUDGE_MODEL`, `PPT_JUDGE_TIMEOUT`, or the shared `ANTHROPIC_API_KEY` / `LLM_JUDGE_MODEL`.
