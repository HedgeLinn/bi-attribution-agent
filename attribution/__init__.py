"""归因引擎(AttributionEngine)。

对外暴露 AttributionEngine 类,harness 只 `from attribution.engine import AttributionEngine`。
契约来源:docs/ATTRIBUTION_CONTRACT.md,字段名/签名均为契约,不得改动。
"""

from .engine import AttributionEngine

__all__ = ["AttributionEngine"]
__version__ = "0.1.0"
