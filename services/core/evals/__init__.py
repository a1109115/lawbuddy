# -*- coding: utf-8 -*-
"""M2 金标评测集（03 清单 §17 固定测试资料）。

数据源：case_file/ 王志远等合同诈骗案合成卷宗（8 卷 59 份，_scripts 生成，
虚构案件，可安全入库检索）。金标事实由 case_data.py 人工核对提炼。

评测隔离：设备级配置库（profile）默认落在用户应用数据目录，直接跑评测会污染
本机真实配置（模型/API Key/连接器/技能）。导入本包时若无显式覆盖，则把
`LAWBUDDY_PROFILE_DIR` 指向本次进程的临时目录，评测结束自动清理。
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

if not os.environ.get("LAWBUDDY_PROFILE_DIR", "").strip():
    _eval_profile_dir = Path(tempfile.mkdtemp(prefix="lawbuddy-profile-eval-"))
    os.environ["LAWBUDDY_PROFILE_DIR"] = str(_eval_profile_dir)
    atexit.register(shutil.rmtree, str(_eval_profile_dir), True)
