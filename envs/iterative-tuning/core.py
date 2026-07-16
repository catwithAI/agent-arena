"""iterative-tuning has no interactive environment tools.

The agent writes a final submission (12 params) into solution.py in the attempt
workspace, iterating locally against a black-box tester. The scorer evaluates
that submission against the private objective and reports % of the known optimum.
"""

from __future__ import annotations
