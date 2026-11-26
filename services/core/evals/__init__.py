# -*- coding: utf-8 -*-
"""M2 金标评测集（03 清单 §17 固定测试资料）。

数据源：case_file/ 王志远等合同诈骗案合成卷宗（8 卷 59 份，_scripts 生成，
虚构案件，可安全入库检索）。金标事实由 case_data.py 人工核对提炼。

评测隔离：设备级配置库（profile）默认落在用户应用数据目录，直接跑确定性评测
会污染本机真实配置（模型/API Key/连接器/技能）。导入本包时若无显式覆盖，则把
`LAWBUDDY_PROFILE_DIR` 指向本次进程的临时目录，评测结束自动清理。

例外：需要真实模型的评测（`wfa_law --profile full|directed|autonomous`）必须读
用户已配置的同一个受保护配置源，否则会出现「应用里能用、评测说没配置模型」的
分裂状态。这类评测在开始时调用 `use_device_profile()` 撤掉本次进程的临时隔离。
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# 是否由本包自动施加了临时隔离（用户/Electron 显式指定 LAWBUDDY_PROFILE_DIR 时为 False）
EVAL_PROFILE_ISOLATED = False
_eval_profile_dir: Path | None = None

if not os.environ.get("LAWBUDDY_PROFILE_DIR", "").strip():
    _eval_profile_dir = Path(tempfile.mkdtemp(prefix="lawbuddy-profile-eval-"))
    os.environ["LAWBUDDY_PROFILE_DIR"] = str(_eval_profile_dir)
    EVAL_PROFILE_ISOLATED = True
    atexit.register(shutil.rmtree, str(_eval_profile_dir), True)


def use_device_profile() -> bool:
    """撤掉本进程的临时 profile 隔离，改用本机受保护的设备级配置。

    只对「自动施加的隔离」生效；用户或 Electron 显式指定的 profile 目录保持不动。
    返回是否发生了切换。确定性评测不要调用它。
    """
    global EVAL_PROFILE_ISOLATED
    if not EVAL_PROFILE_ISOLATED:
        return False
    os.environ.pop("LAWBUDDY_PROFILE_DIR", None)
    EVAL_PROFILE_ISOLATED = False
    from lawbuddy import profile as profile_mod

    profile_mod.set_profile_dir(None)
    return True
