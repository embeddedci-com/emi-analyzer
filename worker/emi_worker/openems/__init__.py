"""openEMS integration: CSX generation, meshing, execution and post-processing.

We drive openEMS through its XML interface rather than its Python bindings — Debian ships
the binaries without the Python module, and the XML is stable across versions. See
``docs/csx-xml-notes.md`` for the schema, verified empirically against the binary.
"""

from .mesh import Mesh, MeshError, MeshSpec, build_mesh
from .model import BuiltModel, ModelError, Port, SolveParams, build_model
from .run import OpenEMSError, RunProgress, RunResult, run_openems

__all__ = [
    "BuiltModel", "Mesh", "MeshError", "MeshSpec", "ModelError", "OpenEMSError",
    "Port", "RunProgress", "RunResult", "SolveParams", "build_mesh", "build_model",
    "run_openems",
]
