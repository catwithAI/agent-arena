"""PPT visual repair env helper tools."""

from __future__ import annotations

from pathlib import Path
import importlib.util

from lane.env_api import EnvContext, env_tool

ENV_DIR = Path(__file__).resolve().parent


def _load_local(name: str):
    path = ENV_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_ppt_visual_repair_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workspace(ctx: EnvContext) -> Path:
    path = Path(getattr(ctx, "attempt_dir", ctx.trace.path.parent)) / "skill_workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


@env_tool(name="task_brief", description="Return the PPT visual polish task contract.", parameters={})
def task_brief(ctx: EnvContext) -> dict:
    workspace = _workspace(ctx)
    return {
        "input_file": "draft.pptx",
        "optional_helper_output": "draft_annotated.pptx",
        "required_outputs": ["polished.pptx"],
        "optional_outputs": ["design_notes.md"],
        "workspace": str(workspace),
        "requirements": [
            "Open draft.pptx from the current workspace. It is a valid, editable PPTX; the task concerns visual quality, not file recovery.",
            "Before editing, render draft.pptx to PNG and actually inspect the rendered slide with visual capabilities; do not rely only on OOXML, extracted text, or object properties.",
            "Optionally use annotate_pptx if object IDs, red bounding boxes, and a 10% slide-width scale bar help diagnosis.",
            "Create polished.pptx in the workspace root.",
            "After editing, render polished.pptx to PNG and visually compare it with the draft render before finalizing.",
            "A short design_notes.md is optional but useful for human review.",
            "Use design judgement to improve hierarchy, composition, alignment, spacing, typography, proportions, and consistency.",
            "Preserve the original theme, meaning, image assets, and slide count; multiple tasteful solutions are valid.",
            "Do not treat opening and re-saving the deck as a solution.",
            "Do not rename required outputs; the scorer reads the exact file names.",
        ],
    }


@env_tool(name="workspace_status", description="List expected input/output files in the workspace.", parameters={})
def workspace_status(ctx: EnvContext) -> dict:
    workspace = _workspace(ctx)
    files = {}
    for name in ("draft.pptx", "draft_annotated.pptx", "object_manifest.json", "polished.pptx", "design_notes.md"):
        path = workspace / name
        files[name] = {
            "exists": path.is_file(),
            "size": path.stat().st_size if path.is_file() else 0,
        }
    return {"workspace": str(workspace), "files": files}


@env_tool(
    name="annotate_pptx",
    description="Create an annotated copy of a PPTX with object IDs, red bounding boxes, and a 10% slide-width scale bar.",
    parameters={
        "type": "object",
        "properties": {
            "input_path": {"type": "string", "description": "PPTX path in workspace", "default": "draft.pptx"},
            "output_path": {"type": "string", "description": "Annotated PPTX path", "default": "draft_annotated.pptx"},
            "manifest_path": {"type": "string", "description": "Object manifest JSON path", "default": "object_manifest.json"}
        },
        "required": []
    },
)
def annotate_pptx(
    ctx: EnvContext,
    input_path: str = "draft.pptx",
    output_path: str = "draft_annotated.pptx",
    manifest_path: str = "object_manifest.json",
) -> dict:
    workspace = _workspace(ctx).resolve()
    annotator = _load_local("ppt_annotation")
    src = (workspace / input_path).resolve()
    out = (workspace / output_path).resolve()
    manifest = (workspace / manifest_path).resolve()
    if workspace not in src.parents and src != workspace:
        return {"error": "input_path must stay inside workspace"}
    if workspace not in out.parents and out != workspace:
        return {"error": "output_path must stay inside workspace"}
    if workspace not in manifest.parents and manifest != workspace:
        return {"error": "manifest_path must stay inside workspace"}
    result = annotator.annotate_pptx(src, out, manifest)
    result["next"] = "Open draft_annotated.pptx or inspect object_manifest.json to identify visual issues in draft.pptx."
    return result
