"""SES source model: metadata, structural discovery, dependencies, graph.

"SES" here is the source representation type (a folder of object files). Object
files declare exactly one Weaver object via a metadata block: a Python module
docstring or a leading SQL ``/* ... */`` comment. Discovery is structural and
static; object modules are never imported at build time.
"""

from __future__ import annotations
