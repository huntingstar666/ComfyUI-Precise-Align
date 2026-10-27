# -*- coding: utf-8 -*-
"""ComfyUI-Precise-Align

通用图像局部编辑 + 零偏移辅助节点：

A. PreciseAlignPad        —— 缩放/补边到指定步长，输出 resolution 与画布遮罩
B. PreciseAlignCropBack   —— 按处理信息把解码结果精确裁回原尺寸
C. PreciseAlignComposite  —— 可选：把未编辑区域用原图像素合成回去
D. PreciseAlignCropEdit   —— 方案 B：裁切编辑区 + 上下文
E. PreciseAlignStitch     —— 方案 B：把裁切结果贴回原图

详细用法见 README.md。
"""

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
