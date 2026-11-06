"""PyInstaller 冻结入口：以包成员身份导入 lawbuddy，复用 __main__.main()。

直接以 lawbuddy/__main__.py 作 PyInstaller 脚本会丢失包上下文
（相对导入 `from .rpc import ...` 失败），故经由本启动器。
"""

from lawbuddy.__main__ import main

if __name__ == "__main__":
    main()
