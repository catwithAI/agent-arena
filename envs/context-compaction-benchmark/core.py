"""context-compaction-benchmark has no interactive environment tools.

The agent reads multi-turn prompts (setup facts → pressure filler → probe) and
writes a structured `probe_answers.json` into its workspace. The scorer reads
that artifact plus the finalized wire trace to compute retention and compaction
observability. Materials and the facts manifest are pre-generated and committed
(see build_tasks.py); nothing is generated at run time.
"""

from __future__ import annotations
