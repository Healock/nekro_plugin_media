"""测试引导：让本仓库在“独立 clone、无 NekroAgent 源码”的环境下可直接运行测试。

背景问题
--------
pytest 会把带 ``__init__.py`` 的目录收集为包并导入插件根的 ``__init__.py``；该文件会导入
完整 NekroAgent（连带 nonebot 等），在精简环境中必然 ``ImportError``。此前只有在目录恰好
命名为 ``nekro_plugin_media`` 时，本文件注册的 ``nekro_plugin_media`` 包桩才会恰好挡住它，
否则整个测试在收集阶段失败（如 36 errors）。

为何桩必须放在这里而不是仓库根
------------------------------
放在仓库根的 ``conftest.py`` 会被 pytest 当作包子模块导入（``<pkg>.conftest``），从而**先**
触发插件根 ``__init__.py`` 的导入，桩还没装上就失败了（实测确认）。而 ``tests/`` 不含
``__init__.py``，本文件以顶层 ``conftest`` 载入，不会触发包的 ``__init__``，因此可以在 pytest
执行任何 ``__init__`` 之前完成预注册。

做法
----
1. 预注册轻量 ``nekro_agent`` 桩：覆盖插件根 ``__init__`` 引用的符号
   （``ConfigBase`` / ``ExtraField`` / ``NekroPlugin``）以及测试用到的 ``core.logger`` 与
   ``plugin.dynamic_import_pkg``；环境中若已有真实 ``nekro_agent`` 则不覆盖。
2. 把插件根目录注册为**仅含 ``__path__`` 的包桩**：既按“实际目录名”注册（使 pytest 后续
   导入 ``<目录名>/__init__.py`` 时解析到桩），又按规范包名 ``nekro_plugin_media`` 注册
   （供测试直接导入子模块）。

注意：仓库根的 ``types.py`` 会遮蔽标准库 ``types``，因此从仓库根执行 ``python -m pytest`` 会在
解释器启动阶段就失败（与本文件无关）；请在仓库根使用 ``pytest`` 命令，或从上级目录执行
``python -m pytest <目录>/tests``。
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import logging
import sys
import types
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _module(name: str) -> types.ModuleType:
    """创建一个带 ``__spec__`` 的模块桩。

    必须带 ``__spec__``：否则 ``importlib.util.find_spec(name)`` 会因 ``__spec__ is None``
    抛 ``ValueError``（其它测试文件里的同类桩安装函数会据此提前返回）。
    """
    module = types.ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, None)
    return module


def _register_package(name: str, path: Path) -> None:
    """注册一个仅含 ``__path__`` 的包桩，避免执行真实的包 ``__init__``。"""
    if name in sys.modules:
        return
    module = _module(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module


def _install_nekro_stub() -> None:
    """在缺少真实 NekroAgent 时安装最小可用桩。"""
    if importlib.util.find_spec("nekro_agent") is not None:
        return

    package = _module("nekro_agent")
    api = _module("nekro_agent.api")
    core = _module("nekro_agent.api.core")
    core.logger = logging.getLogger("nekro_agent.stub")
    core.logger.success = core.logger.info  # Nekro 日志器提供 success，标准 logging 没有
    plugin = _module("nekro_agent.api.plugin")

    def dynamic_import_pkg(spec: str, import_name: str | None = None, **_kwargs: object) -> object:
        return importlib.import_module(import_name or spec.split(">=")[0].strip())

    plugin.dynamic_import_pkg = dynamic_import_pkg
    # 插件根 __init__ 引用的符号占位；正常测试不会实例化它们。
    plugin.ConfigBase = type("ConfigBase", (), {})
    plugin.ExtraField = type("ExtraField", (), {})
    plugin.NekroPlugin = type("NekroPlugin", (), {})

    api.core = core
    api.plugin = plugin
    package.api = api
    for name, module in {
        "nekro_agent": package,
        "nekro_agent.api": api,
        "nekro_agent.api.core": core,
        "nekro_agent.api.plugin": plugin,
    }.items():
        sys.modules.setdefault(name, module)


_install_nekro_stub()
# 先按实际目录名注册（挡住 pytest 对根 __init__.py 的导入），再按规范包名注册（供测试导入）。
_register_package(_PLUGIN_ROOT.name, _PLUGIN_ROOT)
_register_package("nekro_plugin_media", _PLUGIN_ROOT)
