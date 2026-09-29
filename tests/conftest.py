from __future__ import annotations

import sys
import types
from pathlib import Path

plugin_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(plugin_dir.parent))
nekro_source_dir = plugin_dir.parent / "nekro-agent"
if nekro_source_dir.is_dir():
    sys.path.insert(0, str(nekro_source_dir))

package = types.ModuleType("nekro_plugin_media")
package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
sys.modules.setdefault("nekro_plugin_media", package)
