# -*- mode: python ; coding: utf-8 -*-
"""旧打包命令兼容入口；产物统一使用 CodeFuel 名称。"""
from pathlib import Path

SPEC = Path(SPECPATH) / 'CodeFuel.spec'
exec(compile(SPEC.read_text(encoding='utf-8'), str(SPEC), 'exec'))
