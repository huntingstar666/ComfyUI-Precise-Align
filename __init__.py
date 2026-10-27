# -*- coding: utf-8 -*-
"""ComfyUI-Precise-Align

通用图像局部编辑 + 零偏移辅助节点：

A. PreciseAlignPad        —— 缩放/补边到指定步长，输出 resolution 与画布遮罩
B. PreciseAlignCropBack   —— 按处理信息把解码结果精确裁回原尺寸
C. PreciseAlignComposite  —— 可选：把未编辑区域用原图像素合成回去
D. PreciseAlignCropEdit   —— 方案 B：裁切编辑区 + 上下文
E. PreciseAlignStitch     —— 方案 B：把裁切结果贴回原图
F. PreciseAlignRegion     —— 画框/画圈生成遮罩与包围盒

详细用法见 README.md。
"""

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./web"
__version__ = "1.0.13"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY", "__version__"]


def _register_version_route():
    """给前端一个版本探测接口：前端发现服务端版本过旧时会提示重启。"""
    try:
        from aiohttp import web
        from server import PromptServer

        async def _version(request):
            return web.json_response({"version": __version__})

        for path in ("/precise_align/version", "/api/precise_align/version"):
            try:
                PromptServer.instance.routes.get(path)(_version)
            except Exception:
                pass
    except Exception as exc:
        print(f"[PreciseAlign] version route skipped: {exc}")


_register_version_route()
print(f"[PreciseAlign] build {__version__} loaded")
