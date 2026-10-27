# -*- coding: utf-8 -*-
"""通用图像局部编辑辅助节点（方案 A / 方案 B），针对 Qwen-Image 2.1 的零偏移做了优化。

方案 A：全画布局部编辑（保全局信息）
    PreciseAlignPad  → 缩放 + 补边到 32 + 可选框/遮罩，输出 resolution=0 与画布对齐的 mask
    PreciseAlignCropBack → 解码后裁回原尺寸（含缩放逆变换），并输出裁回后的 mask
    PreciseAlignComposite → 用原图合成 mask 外区域

方案 B：裁切局部编辑（省显存，适合大图/移除类任务）
    PreciseAlignCropEdit  → 按框/遮罩裁出编辑区 + 上下文，补边到 32，输出 crop_info 与 mask
    PreciseAlignStitch → 把解码后的裁切结果贴回原图（带羽化）

所有尺寸都保证是 32 的倍数；resolution 统一输出 0。
"""

import torch
import torch.nn.functional as F

PAD_INFO_TYPE = "PRECISE_ALIGN_PAD_INFO"
CROP_INFO_TYPE = "PRECISE_ALIGN_CROP_INFO"
CATEGORY = "image/local edit"
MULTIPLE = 32

PAD_MODES = ["edge", "reflect", "gray", "black", "white"]
ALIGN_MODES = ["center", "top_left"]
SCALE_METHODS = ["lanczos", "bicubic", "bilinear", "area", "nearest-exact"]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _resize_bhwc(image, width, height, method="lanczos"):
    """B,H,W,C 图像缩放。优先用 ComfyUI 的 common_upscale（支持 lanczos）。"""
    if image.shape[2] == width and image.shape[1] == height:
        return image
    x = image.permute(0, 3, 1, 2)
    try:
        import comfy.utils  # noqa
        x = comfy.utils.common_upscale(x, width, height, method, "disabled")
    except Exception:
        mode = method if method in ("nearest-exact", "bilinear", "bicubic", "area") else "bicubic"
        kwargs = {"align_corners": False} if mode in ("bilinear", "bicubic") else {}
        x = F.interpolate(x, size=(height, width), mode=mode, **kwargs)
    return x.permute(0, 2, 3, 1)


def _resize_mask(mask, width, height):
    if mask.shape[-1] == width and mask.shape[-2] == height:
        return mask
    m = mask.unsqueeze(1)
    m = F.interpolate(m, size=(height, width), mode="bilinear", align_corners=False)
    return m.squeeze(1)


def _pad_bhwc(image, top, bottom, left, right, mode="edge", value=0.5):
    if top == bottom == left == right == 0:
        return image
    x = image.permute(0, 3, 1, 2)
    if mode == "edge":
        x = F.pad(x, (left, right, top, bottom), mode="replicate")
    elif mode == "reflect":
        try:
            x = F.pad(x, (left, right, top, bottom), mode="reflect")
        except RuntimeError:
            x = F.pad(x, (left, right, top, bottom), mode="replicate")
    else:
        v = 0.0 if mode == "black" else 1.0 if mode == "white" else float(value)
        x = F.pad(x, (left, right, top, bottom), mode="constant", value=v)
    return x.permute(0, 2, 3, 1)


def _dilate(mask, px):
    if px <= 0:
        return mask
    m = mask.unsqueeze(1)
    m = F.pad(m, (px, px, px, px))
    m = F.max_pool2d(m, kernel_size=2 * px + 1, stride=1)
    return m.squeeze(1)


def _feather(mask, px):
    """高斯羽化（可分离卷积），比盒式模糊更平滑，不会出现线性渐变带。"""
    px = int(px)
    if px <= 0:
        return mask
    sigma = max(0.5, px / 2.0)
    k = 2 * px + 1
    xs = torch.arange(k, dtype=mask.dtype, device=mask.device) - px
    g = torch.exp(-(xs * xs) / (2.0 * sigma * sigma))
    g = g / g.sum()
    m = mask.unsqueeze(1)
    m = F.pad(m, (px, px, 0, 0), mode="replicate")
    m = F.conv2d(m, g.view(1, 1, 1, -1))
    m = F.pad(m, (0, 0, px, px), mode="replicate")
    m = F.conv2d(m, g.view(1, 1, -1, 1))
    return m.squeeze(1)


def _apply_curve(alpha, curve="smoothstep"):
    a = alpha.clamp(0.0, 1.0)
    if curve == "smoothstep":
        return a * a * (3.0 - 2.0 * a)
    return a


def _band_from_mask(mask, fallback_px=8):
    """取 mask 的过渡带（0<mask<1）；硬边 mask 则用外扩环带。"""
    a = mask.float().clamp(0.0, 1.0)
    band = ((a > 0.01) & (a < 0.99)).float()
    if float(band.sum()) >= 16:
        return band
    ring = (_dilate(a, max(8, int(fallback_px))) - a).clamp(0.0, 1.0)
    if float(ring.sum()) < 16:
        return None
    return ring


def _color_match(edited, reference, weights, strength=1.0):
    """用过渡带的均值/标准差做 Reinhard 仿射颜色校正。"""
    w = weights.unsqueeze(-1)
    n = w.sum(dim=(1, 2), keepdim=True) + 1e-6
    mo = (reference * w).sum(dim=(1, 2), keepdim=True) / n
    me = (edited * w).sum(dim=(1, 2), keepdim=True) / n
    vo = (((reference - mo) ** 2) * w).sum(dim=(1, 2), keepdim=True) / n
    ve = (((edited - me) ** 2) * w).sum(dim=(1, 2), keepdim=True) / n
    so = (vo + 1e-6).sqrt()
    se = (ve + 1e-6).sqrt()
    corrected = (edited - me) * (so / se) + mo
    return edited + float(strength) * (corrected - edited)


def _scale_dims(width, height, scale_by, step):
    if abs(scale_by - 1.0) < 1e-6:
        return width, height
    step = max(1, int(step))
    tw = max(step, int(round(width * scale_by / step)) * step)
    th = max(step, int(round(height * scale_by / step)) * step)
    return tw, th


def _norm_mask(mask, batch):
    m = mask.float()
    if m.dim() == 2:
        m = m.unsqueeze(0)
    if m.shape[0] == 1 and batch > 1:
        m = m.expand(batch, -1, -1)
    return m


def _mask_to_canvas(mask, box, crop_x, crop_y, crop_w, crop_h,
                    out_w, out_h, pad_l, pad_t, canvas_w, canvas_h,
                    expand, feather, batch, snap=0):
    """把原图上的 mask / box 变换到目标画布（补边后）的坐标系。"""
    if mask is None and box is None:
        return None
    base = torch.zeros(batch, canvas_h, canvas_w)

    if mask is not None:
        m = _norm_mask(mask, batch)
        m = m[:, crop_y:crop_y + crop_h, crop_x:crop_x + crop_w]
        m = _resize_mask(m, out_w, out_h)
        base[:, pad_t:pad_t + out_h, pad_l:pad_l + out_w] = m.clamp(0.0, 1.0)
    else:
        bx, by, bw, bh = box
        sx = out_w / float(max(1, crop_w))
        sy = out_h / float(max(1, crop_h))
        x1 = pad_l + int(round((bx - crop_x) * sx))
        y1 = pad_t + int(round((by - crop_y) * sy))
        x2 = pad_l + int(round((bx + bw - crop_x) * sx))
        y2 = pad_t + int(round((by + bh - crop_y) * sy))
        if snap > 0:
            # 向外对齐到 32 网格，避免 latent 半格硬边
            x1 = (x1 // snap) * snap
            y1 = (y1 // snap) * snap
            x2 = ((x2 + snap - 1) // snap) * snap
            y2 = ((y2 + snap - 1) // snap) * snap
        x1, x2 = max(0, min(x1, canvas_w)), max(0, min(x2, canvas_w))
        y1, y2 = max(0, min(y1, canvas_h)), max(0, min(y2, canvas_h))
        if x2 > x1 and y2 > y1:
            base[:, y1:y2, x1:x2] = 1.0

    base = _dilate(base, int(expand))
    base = _feather(base, int(feather))
    return base.clamp(0.0, 1.0)


def _mask_bbox(mask):
    """返回整批 mask 的联合包围盒 (x, y, w, h)，没有前景返回 None。"""
    m = mask.float()
    if m.dim() == 2:
        m = m.unsqueeze(0)
    m = (m > 0.5)
    if not bool(m.any()):
        return None
    rows = torch.nonzero(m.any(dim=2), as_tuple=False)  # [N, 2] (b, y)
    cols = torch.nonzero(m.any(dim=1), as_tuple=False)  # [N, 2] (b, x)
    y1 = int(rows[:, 1].min())
    y2 = int(rows[:, 1].max())
    x1 = int(cols[:, 1].min())
    x2 = int(cols[:, 1].max())
    return x1, y1, x2 - x1 + 1, y2 - y1 + 1


def _sanity_warnings(info):
    w, h = info["orig_w"], info["orig_h"]
    pw, ph = info["padded_w"], info["padded_h"]
    msgs = []
    if min(w, h) < 64:
        msgs.append(f"边长 < 64px（{w}x{h}），远低于模型训练分布，结果可能无意义。")
    ratio = w / float(h)
    if ratio > 8.0 or ratio < 1.0 / 8.0:
        msgs.append(f"极端宽高比 {ratio:.2f}，模型可能无法正确理解构图。")
    pad_tokens = (int(info["padded_w"]) // 32) * (int(info["padded_h"]) // 32)
    if int(info.get("multiple", MULTIPLE)) == MULTIPLE:
        if pad_tokens > 4096:
            msgs.append(f"参考 token ≈ {pad_tokens}（> 4096），显存/速度压力很大。")
        elif pad_tokens > 2048:
            msgs.append(f"参考 token ≈ {pad_tokens}（> 2048），请确认显存余量。")
    pad_ratio = 1.0 - (info.get("scaled_w", w) * info.get("scaled_h", h)) / float(pw * ph)
    if pad_ratio > 0.2:
        msgs.append(f"补边占画布 {pad_ratio * 100:.1f}%，边缘复制/填充区域较大，模型可能当成内容。")
    return msgs


def _valid_box(box_x, box_y, box_w, box_h):
    if box_w is None or box_h is None or box_w <= 0 or box_h <= 0:
        return None
    return (int(box_x), int(box_y), int(box_w), int(box_h))


# ---------------------------------------------------------------------------
# 方案 A：全画布局部编辑
# ---------------------------------------------------------------------------

class PreciseAlignPad:
    """A-1：缩放/补边到 32，输出 resolution=0 与画布对齐的 mask。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "原始图像（支持 batch，同批必须同尺寸）"}),
                "pad_mode": (PAD_MODES, {"default": "edge",
                                         "tooltip": "补边方式：edge=边缘复制（推荐）、reflect=镜像、gray/black/white=纯色"}),
                "align": (ALIGN_MODES, {"default": "center",
                                        "tooltip": "补边方向：center=居中（推荐）、top_left=只补右下"}),
            },
            "optional": {
                "pad_value": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01,
                                        "advanced": True,
                                        "tooltip": "pad_mode=gray 时的填充灰度（0=黑，1=白）"}),
                "mask": ("MASK", {"tooltip": "可选：原图尺寸的遮罩（框选/涂抹区域），优先于 box_*"}),
                "box_x": ("INT", {"default": 0, "min": 0, "max": 16384, "step": 1,
                                  "tooltip": "框选区域左上角 X（原图坐标，像素）"}),
                "box_y": ("INT", {"default": 0, "min": 0, "max": 16384, "step": 1,
                                  "tooltip": "框选区域左上角 Y（原图坐标，像素）"}),
                "box_w": ("INT", {"default": 0, "min": 0, "max": 16384, "step": 1,
                                  "tooltip": "框选宽度；与 box_h 一起 >0 时生效"}),
                "box_h": ("INT", {"default": 0, "min": 0, "max": 16384, "step": 1,
                                  "tooltip": "框选高度；与 box_w 一起 >0 时生效"}),
                "mask_expand": ("INT", {"default": 8, "min": 0, "max": 512, "step": 1,
                                        "tooltip": "遮罩向外扩张像素（覆盖边界、避免接缝）"}),
                "mask_feather": ("INT", {"default": 8, "min": 0, "max": 512, "step": 1,
                                         "tooltip": "高斯羽化半径（px），建议 24~48"}),
                "mask_snap": ("BOOLEAN", {"default": True, "advanced": True,
                                          "tooltip": "框选时把矩形向外对齐到 32 网格，避免 latent 半格硬边"}),
                "scale_by": ("FLOAT", {"default": 1.0, "min": 0.05, "max": 8.0, "step": 0.01,
                                       "tooltip": "缩放倍数；1.0 = 跳过缩放（不重采样）"}),
                "scale_method": (SCALE_METHODS, {"default": "lanczos",
                                                 "tooltip": "缩放算法：lanczos 最锐、area 适合缩小"}),
                "scale_step": ("INT", {"default": 32, "min": 8, "max": 256, "step": 8,
                                       "tooltip": "缩放后宽高对齐到该步长（默认 32）"}),
                "multiple": ("INT", {"default": MULTIPLE, "min": 8, "max": 256, "step": 8,
                                     "tooltip": "对齐步长：Qwen 2.1 用 32；SD/SDXL 可用 8；Flux/SD3 可用 16"}),
            },
        }

    RETURN_TYPES = ("IMAGE", "INT", "INT", "INT", PAD_INFO_TYPE, "MASK")
    RETURN_NAMES = ("image", "height", "width", "resolution", "pad_info", "mask")
    SEARCH_ALIASES = ["qwen pad", "qwen 2.1", "pad to 32", "补边", "局部编辑", "noise mask", "mask"]
    OUTPUT_TOOLTIPS = (
        "缩放+补边后的图像（同时喂给文本编码器和 VAEEncode）",
        "补边后高度",
        "补边后宽度",
        "恒为 0，接 TextEncodeQwenImage21.resolution（Qwen 2.1 专用；其他模型忽略此输出）",
        "处理信息，接 Crop Back",
        "画布对齐的遮罩：框内=1，框外=0；未选择区域时全 1",
    )
    FUNCTION = "pad"
    CATEGORY = CATEGORY
    DESCRIPTION = "通用补边对齐：缩放/补边到指定步长（默认 32），输出 resolution（Qwen 2.1 用 0）与遮罩。Pad/align to a multiple with optional region mask."

    def pad(self, image, pad_mode="edge", align="center", pad_value=0.5, multiple=32,
            scale_by=1.0, scale_method="lanczos", scale_step=32,
            mask=None, box_x=0, box_y=0, box_w=0, box_h=0,
            mask_expand=8, mask_feather=8, mask_snap=True):
        multiple = max(1, int(multiple))
        orig_h, orig_w = image.shape[1], image.shape[2]
        batch = image.shape[0]
        scaled_w, scaled_h = _scale_dims(orig_w, orig_h, scale_by, scale_step)
        img = image if (scaled_w, scaled_h) == (orig_w, orig_h) else \
            _resize_bhwc(image, scaled_w, scaled_h, scale_method)

        pad_h = (-scaled_h) % multiple
        pad_w = (-scaled_w) % multiple
        if align == "top_left":
            top, bottom = 0, pad_h
            left, right = 0, pad_w
        else:
            top = pad_h // 2
            bottom = pad_h - top
            left = pad_w // 2
            right = pad_w - left

        padded = _pad_bhwc(img, top, bottom, left, right, pad_mode, pad_value)
        canvas_w, canvas_h = padded.shape[2], padded.shape[1]

        box = _valid_box(box_x, box_y, box_w, box_h)
        canvas_mask = _mask_to_canvas(
            mask, box, 0, 0, orig_w, orig_h, scaled_w, scaled_h,
            left, top, canvas_w, canvas_h, mask_expand, mask_feather, batch,
            snap=multiple if mask_snap else 0,
        )
        if canvas_mask is None:
            canvas_mask = torch.ones(batch, canvas_h, canvas_w)
        elif float(canvas_mask.sum()) == 0:
            print("[PreciseAlignPad] WARNING: 框选/遮罩在画布上没有覆盖任何像素，请检查 box 坐标。")

        info = {
            "orig_w": orig_w, "orig_h": orig_h,
            "scaled_w": scaled_w, "scaled_h": scaled_h,
            "padded_w": canvas_w, "padded_h": canvas_h,
            "left": left, "top": top, "right": right, "bottom": bottom,
            "scale_by": float(scale_by), "scale_method": scale_method,
            "scale_step": int(scale_step),
            "multiple": multiple,
            "has_mask": bool(mask is not None or box is not None),
            "mask_expand": int(mask_expand), "mask_feather": int(mask_feather),
        }
        print(f"[PreciseAlignPad] {orig_w}x{orig_h} -> {canvas_w}x{canvas_h} "
              f"(scale={scale_by}, left={left}, top={top}, mask={'yes' if info['has_mask'] else 'no'})")
        for m in _sanity_warnings(info):
            print("[PreciseAlignPad] WARNING: " + m)
        return (padded, canvas_h, canvas_w, 0, info, canvas_mask)


class PreciseAlignCropBack:
    """A-2：裁回原尺寸（含缩放逆变换），并输出裁回后的 mask。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "VAE Decode 之后的图像（画布尺寸）"}),
                "pad_info": (PAD_INFO_TYPE, {"tooltip": "A-1 输出的 pad_info（记录补边/缩放信息）"}),
            },
            "optional": {
                "mask": ("MASK", {"tooltip": "A-1 输出的 mask（可选）；接了才会输出裁回后的 mask"}),
                "downscale_method": (SCALE_METHODS, {"default": "bicubic",
                                                     "tooltip": "缩放逆变换使用的算法（scale_by≠1 时生效）"}),
            },
        }

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    OUTPUT_TOOLTIPS = ("裁回原始尺寸的图像", "裁回原始尺寸的遮罩（未接 mask 时全 0）")
    SEARCH_ALIASES = ["qwen crop back", "crop back", "裁回", "还原尺寸", "unpad"]
    FUNCTION = "crop"
    CATEGORY = CATEGORY
    DESCRIPTION = "方案A：把解码后的图像按 pad_info 精确裁回原尺寸（含缩放逆变换），并输出裁回后的遮罩。Scheme A: crop back to original size."

    def crop(self, image, pad_info, mask=None, downscale_method="bicubic"):
        b, h, w, c = image.shape
        ow, oh = int(pad_info["orig_w"]), int(pad_info["orig_h"])
        sw, sh = int(pad_info["scaled_w"]), int(pad_info["scaled_h"])
        pw, ph = int(pad_info["padded_w"]), int(pad_info["padded_h"])
        left, top = int(pad_info["left"]), int(pad_info["top"])

        if (w, h) != (pw, ph):
            sx, sy = w / float(pw), h / float(ph)
            left = int(round(left * sx))
            top = int(round(top * sy))
            sw = max(1, int(round(sw * sx)))
            sh = max(1, int(round(sh * sy)))
            print(f"[PreciseAlignCropBack] WARNING: decoded {w}x{h} != padded {pw}x{ph}; proportional crop.")

        left = max(0, min(left, w - 1))
        top = max(0, min(top, h - 1))
        sw = max(1, min(sw, w - left))
        sh = max(1, min(sh, h - top))

        out = image[:, top:top + sh, left:left + sw, :]
        if (sw, sh) != (ow, oh):
            out = _resize_bhwc(out, ow, oh, downscale_method)

        if mask is None:
            out_mask = torch.zeros(b, oh, ow)
        else:
            m = mask if mask.dim() == 3 else mask.unsqueeze(0)
            m = m[:, top:top + sh, left:left + sw]
            if (sw, sh) != (ow, oh):
                m = _resize_mask(m, ow, oh)
            out_mask = m.clamp(0.0, 1.0)

        print(f"[PreciseAlignCropBack] crop x={left}, y={top}, {sw}x{sh} -> resize {ow}x{oh}")
        return (out, out_mask)


# ---------------------------------------------------------------------------
# 方案 B：裁切局部编辑
# ---------------------------------------------------------------------------

class PreciseAlignCropEdit:
    """B-1：按框/遮罩裁出编辑区 + 上下文，补边到 32，输出 crop_info。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "原始图像（支持 batch，同批必须同尺寸）"}),
                "context_factor": ("FLOAT", {"default": 1.5, "min": 1.0, "max": 4.0, "step": 0.05,
                                             "tooltip": "编辑区外扩倍数（上下文），建议 1.3~1.6"}),
                "context_pixels": ("INT", {"default": 64, "min": 0, "max": 2048, "step": 8,
                                           "tooltip": "额外外扩像素（叠加在倍数之后），建议 64~128"}),
                "pad_mode": (PAD_MODES, {"default": "edge",
                                         "tooltip": "裁切块补到 32 倍数时的填充方式：edge=边缘复制（推荐）"}),
                "align": (ALIGN_MODES, {"default": "center",
                                        "tooltip": "补边方向：center=居中（推荐）、top_left=只补右下"}),
            },
            "optional": {
                "pad_value": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01,
                                        "advanced": True,
                                        "tooltip": "pad_mode=gray 时的填充灰度（0=黑，1=白）"}),
                "mask": ("MASK", {"tooltip": "编辑区域遮罩（原图尺寸）；全黑会报错，优先于 box_*"}),
                "box_x": ("INT", {"default": 0, "min": 0, "max": 16384, "step": 1,
                                  "tooltip": "框选区域左上角 X（原图坐标，像素）"}),
                "box_y": ("INT", {"default": 0, "min": 0, "max": 16384, "step": 1,
                                  "tooltip": "框选区域左上角 Y（原图坐标，像素）"}),
                "box_w": ("INT", {"default": 0, "min": 0, "max": 16384, "step": 1,
                                  "tooltip": "框选宽度；与 box_h 一起 >0 时生效"}),
                "box_h": ("INT", {"default": 0, "min": 0, "max": 16384, "step": 1,
                                  "tooltip": "框选高度；与 box_w 一起 >0 时生效"}),
                "mask_expand": ("INT", {"default": 8, "min": 0, "max": 512, "step": 1,
                                        "tooltip": "遮罩向外扩张像素（覆盖边界、避免接缝）"}),
                "mask_feather": ("INT", {"default": 8, "min": 0, "max": 512, "step": 1,
                                         "tooltip": "高斯羽化半径（px），建议 24~48"}),
                "mask_snap": ("BOOLEAN", {"default": True, "advanced": True,
                                          "tooltip": "框选时把矩形向外对齐到 32 网格"}),
                "scale_by": ("FLOAT", {"default": 1.0, "min": 0.05, "max": 8.0, "step": 0.01,
                                       "tooltip": "裁切块缩放倍数；1.0=不缩放；小图可放大到 1024 再编辑"}),
                "scale_method": (SCALE_METHODS, {"default": "lanczos",
                                                 "tooltip": "裁切块缩放算法"}),
                "scale_step": ("INT", {"default": 32, "min": 8, "max": 256, "step": 8,
                                       "tooltip": "缩放后宽高对齐到该步长（默认 32）"}),
                "multiple": ("INT", {"default": MULTIPLE, "min": 8, "max": 256, "step": 8,
                                     "tooltip": "对齐步长：Qwen 2.1 用 32；SD/SDXL 可用 8；Flux/SD3 可用 16"}),
            },
        }

    RETURN_TYPES = ("IMAGE", "INT", "INT", "INT", CROP_INFO_TYPE, "MASK")
    RETURN_NAMES = ("image", "height", "width", "resolution", "crop_info", "mask")
    SEARCH_ALIASES = ["qwen crop", "crop edit", "裁切", "局部", "inpaint crop", "context crop"]
    OUTPUT_TOOLTIPS = (
        "裁切+补边后的编辑区（同时喂给文本编码器和 VAEEncode）",
        "画布高度",
        "画布宽度",
        "恒为 0（Qwen 2.1 专用；其他模型忽略此输出）",
        "裁切信息，接 Stitch Back",
        "画布对齐的遮罩（可接 Set Latent Noise Mask，只改物体不重绘上下文）",
    )
    FUNCTION = "crop_region"
    CATEGORY = CATEGORY
    DESCRIPTION = "通用裁切局部：按框/遮罩裁出编辑区+上下文，补边到指定步长（默认 32）。Crop edit region with context and pad to a multiple."

    def crop_region(self, image, context_factor=1.5, context_pixels=64,
                    pad_mode="edge", align="center", pad_value=0.5, multiple=32,
                    scale_by=1.0, scale_method="lanczos", scale_step=32,
                    mask=None, box_x=0, box_y=0, box_w=0, box_h=0,
                    mask_expand=8, mask_feather=8, mask_snap=True):
        multiple = max(1, int(multiple))
        orig_h, orig_w = image.shape[1], image.shape[2]
        batch = image.shape[0]
        box = _valid_box(box_x, box_y, box_w, box_h)

        if mask is not None:
            region = _mask_bbox(mask)
            if region is None:
                raise ValueError("PreciseAlignCropEdit: 遮罩是全黑的，找不到编辑区域。")
        elif box is not None:
            bx, by, bw, bh = box
            x1, y1 = max(0, bx), max(0, by)
            x2, y2 = min(orig_w, bx + bw), min(orig_h, by + bh)
            if x2 <= x1 or y2 <= y1:
                raise ValueError(
                    f"PreciseAlignCropEdit: box {box} 不在图像 {orig_w}x{orig_h} 范围内。"
                )
            region = (x1, y1, x2 - x1, y2 - y1)
        else:
            raise ValueError("PreciseAlignCropEdit: 需要提供 mask 或 box（box_w/box_h > 0）。")

        rx, ry, rw, rh = region
        cx, cy = rx + rw / 2.0, ry + rh / 2.0
        nw = max(32, int(round(rw * context_factor)) + 2 * int(context_pixels))
        nh = max(32, int(round(rh * context_factor)) + 2 * int(context_pixels))
        x = int(round(cx - nw / 2.0))
        y = int(round(cy - nh / 2.0))

        # 先夹到图像内，再保证窗口不越界
        x = max(0, min(x, max(0, orig_w - 1)))
        y = max(0, min(y, max(0, orig_h - 1)))
        cw = max(1, min(nw, orig_w - x))
        ch = max(1, min(nh, orig_h - y))

        crop = image[:, y:y + ch, x:x + cw, :]
        scaled_w, scaled_h = _scale_dims(cw, ch, scale_by, scale_step)
        if (scaled_w, scaled_h) != (cw, ch):
            crop = _resize_bhwc(crop, scaled_w, scaled_h, scale_method)

        pad_h = (-scaled_h) % multiple
        pad_w = (-scaled_w) % multiple
        if align == "top_left":
            top, bottom = 0, pad_h
            left, right = 0, pad_w
        else:
            top = pad_h // 2
            bottom = pad_h - top
            left = pad_w // 2
            right = pad_w - left

        padded = _pad_bhwc(crop, top, bottom, left, right, pad_mode, pad_value)
        canvas_w, canvas_h = padded.shape[2], padded.shape[1]

        canvas_mask = _mask_to_canvas(
            mask, box, x, y, cw, ch, scaled_w, scaled_h,
            left, top, canvas_w, canvas_h, mask_expand, mask_feather, batch,
            snap=multiple if mask_snap else 0,
        )
        if canvas_mask is None:
            canvas_mask = torch.ones(batch, canvas_h, canvas_w)

        info = {
            "orig_w": orig_w, "orig_h": orig_h,
            "x": x, "y": y, "crop_w": cw, "crop_h": ch,
            "scaled_w": scaled_w, "scaled_h": scaled_h,
            "padded_w": canvas_w, "padded_h": canvas_h,
            "left": left, "top": top, "right": right, "bottom": bottom,
            "scale_by": float(scale_by), "scale_method": scale_method,
            "scale_step": int(scale_step),
            "multiple": multiple,
            "has_mask": bool(mask is not None or box is not None),
            "region": [int(v) for v in region],
        }
        print(f"[PreciseAlignCropEdit] region={region} -> crop x={x}, y={y}, {cw}x{ch} "
              f"-> canvas {canvas_w}x{canvas_h} (scale={scale_by})")
        return (padded, canvas_h, canvas_w, 0, info, canvas_mask)


class PreciseAlignStitch:
    """B-2：把解码后的裁切结果贴回原图（带羽化）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "original_image": ("IMAGE", {"tooltip": "原始整图（贴回目标，尺寸必须与 crop_info 一致）"}),
                "edited_image": ("IMAGE", {"tooltip": "B-1 裁切块的解码结果（画布尺寸）"}),
                "crop_info": (CROP_INFO_TYPE, {"tooltip": "B-1 输出的 crop_info（记录裁切/缩放信息）"}),
            },
            "optional": {
                "mask": ("MASK", {"tooltip": "B-1 输出的 mask（可选）；用于混合与颜色匹配"}),
                "blend_pixels": ("INT", {"default": 16, "min": 0, "max": 512, "step": 1,
                                         "tooltip": "边缘羽化像素，建议 24~48"}),
                "upscale_method": (SCALE_METHODS, {"default": "bicubic", "advanced": True,
                                                   "tooltip": "裁切块缩回原尺寸时使用的算法"}),
                "curve": (["smoothstep", "linear"], {"default": "smoothstep",
                                                     "tooltip": "过渡曲线；smoothstep 接缝更自然"}),
                "color_match": ("BOOLEAN", {"default": False,
                                            "tooltip": "贴回前用边缘过渡带把裁切块颜色拉回原图"}),
                "color_strength": ("FLOAT", {"default": 0.8, "min": 0.0, "max": 1.0, "step": 0.01,
                                             "advanced": True,
                                             "tooltip": "颜色匹配强度（0=关闭，1=完全匹配）"}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    OUTPUT_TOOLTIPS = ("贴回后的整图（原尺寸）",)
    SEARCH_ALIASES = ["qwen stitch", "stitch", "贴回", "拼接", "paste back"]
    FUNCTION = "stitch"
    CATEGORY = CATEGORY
    DESCRIPTION = "方案B：把解码后的裁切块按 crop_info 贴回原图，支持 smoothstep 羽化与颜色匹配。Scheme B: stitch back with blend + color match."

    def stitch(self, original_image, edited_image, crop_info, mask=None,
               blend_pixels=16, upscale_method="bicubic",
               curve="smoothstep", color_match=False, color_strength=0.8):
        oh, ow = int(crop_info["orig_h"]), int(crop_info["orig_w"])
        if original_image.shape[1] != oh or original_image.shape[2] != ow:
            raise ValueError(
                f"PreciseAlignStitch: original_image 尺寸 {original_image.shape[2]}x{original_image.shape[1]} "
                f"与 crop_info 记录的 {ow}x{oh} 不一致。"
            )

        x, y = int(crop_info["x"]), int(crop_info["y"])
        cw, ch = int(crop_info["crop_w"]), int(crop_info["crop_h"])
        sw, sh = int(crop_info["scaled_w"]), int(crop_info["scaled_h"])
        pw, ph = int(crop_info["padded_w"]), int(crop_info["padded_h"])
        left, top = int(crop_info["left"]), int(crop_info["top"])
        b = edited_image.shape[0]

        eh, ew = edited_image.shape[1], edited_image.shape[2]
        if (ew, eh) != (pw, ph):
            sx, sy = ew / float(pw), eh / float(ph)
            left = int(round(left * sx))
            top = int(round(top * sy))
            sw = max(1, int(round(sw * sx)))
            sh = max(1, int(round(sh * sy)))
            print(f"[PreciseAlignStitch] WARNING: edited {ew}x{eh} != padded {pw}x{ph}; proportional map.")

        left = max(0, min(left, ew - 1))
        top = max(0, min(top, eh - 1))
        sw = max(1, min(sw, ew - left))
        sh = max(1, min(sh, eh - top))

        content = edited_image[:, top:top + sh, left:left + sw, :]
        if (sw, sh) != (cw, ch):
            content = _resize_bhwc(content, cw, ch, upscale_method)
        content = content.to(device=original_image.device, dtype=original_image.dtype)

        if mask is not None:
            m = mask if mask.dim() == 3 else mask.unsqueeze(0)
            m = m[:, top:top + sh, left:left + sw]
            if (sw, sh) != (cw, ch):
                m = _resize_mask(m, cw, ch)
            alpha = m.clamp(0.0, 1.0)
        elif blend_pixels > 0:
            alpha = torch.ones(b, ch, cw)
            bp = min(int(blend_pixels), cw // 2, ch // 2)
            if bp > 0:
                ramp = torch.linspace(0.0, 1.0, bp + 2)[1:-1]
                ax = torch.ones(cw)
                ay = torch.ones(ch)
                ax[:bp] = ramp
                ax[-bp:] = ramp.flip(0)
                ay[:bp] = ramp
                ay[-bp:] = ramp.flip(0)
                alpha = (ay[:, None] * ax[None, :]).unsqueeze(0).expand(b, -1, -1)
        else:
            alpha = torch.ones(b, ch, cw)

        alpha = alpha.clamp(0.0, 1.0).to(device=original_image.device)

        if color_match:
            band = _band_from_mask(alpha, fallback_px=max(8, int(blend_pixels)))
            if band is not None:
                original_region = original_image[:, y:y + ch, x:x + cw, :]
                content = _color_match(content, original_region, band, color_strength)
                print(f"[PreciseAlignStitch] color match applied (strength={color_strength})")

        alpha = _apply_curve(alpha, curve)
        alpha = alpha.to(device=original_image.device, dtype=original_image.dtype).unsqueeze(-1)
        out = original_image.clone()
        region = out[:, y:y + ch, x:x + cw, :]
        out[:, y:y + ch, x:x + cw, :] = region * (1.0 - alpha) + content * alpha
        print(f"[PreciseAlignStitch] stitch at x={x}, y={y}, {cw}x{ch} (blend={blend_pixels}, curve={curve})")
        return (out,)


# ---------------------------------------------------------------------------
# 合成（方案 A 收尾）
# ---------------------------------------------------------------------------

class PreciseAlignComposite:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "original_image": ("IMAGE", {"tooltip": "原始图像（用于保留遮罩外的像素）"}),
                "edited_image": ("IMAGE", {"tooltip": "A-2 裁回后的编辑结果（尺寸必须与原图一致）"}),
                "mask": ("MASK", {"tooltip": "画布遮罩：白色=使用编辑结果，黑色=保留原图"}),
                "invert_mask": ("BOOLEAN", {"default": False, "tooltip": "反转遮罩（白黑互换）"}),
                "feather": ("INT", {"default": 0, "min": 0, "max": 256, "step": 1,
                                    "tooltip": "高斯羽化半径（px），用于消除接缝，建议 24~48"}),
                "curve": (["smoothstep", "linear"], {"default": "smoothstep",
                                                     "tooltip": "过渡曲线；smoothstep 接缝更自然"}),
                "color_match": ("BOOLEAN", {"default": False,
                                            "tooltip": "用过渡带的均值/标准差把编辑结果的颜色拉回原图"}),
                "color_strength": ("FLOAT", {"default": 0.8, "min": 0.0, "max": 1.0, "step": 0.01,
                                             "advanced": True,
                                             "tooltip": "颜色匹配强度（0=关闭，1=完全匹配）"}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    OUTPUT_TOOLTIPS = ("合成后的图像：遮罩内来自编辑结果，遮罩外保持原图",)
    SEARCH_ALIASES = ["qwen composite", "composite back", "合成", "颜色匹配", "color match"]
    FUNCTION = "composite"
    CATEGORY = CATEGORY
    DESCRIPTION = "方案A 收尾：用遮罩把编辑结果合成到原图上（高斯羽化 + 可选颜色匹配），遮罩外保持原图像素。Composite with feather + optional color match."

    def composite(self, original_image, edited_image, mask, invert_mask=False, feather=0,
                  curve="smoothstep", color_match=False, color_strength=0.8):
        if original_image.shape != edited_image.shape:
            raise ValueError("PreciseAlignComposite: 两张图尺寸不一致，请先把 Crop Back 接在解码之后。")
        edited_image = edited_image.to(device=original_image.device, dtype=original_image.dtype)
        b, h, w, c = original_image.shape
        m = mask
        if m.dim() == 2:
            m = m.unsqueeze(0)
        elif m.dim() == 4:
            m = m[:, 0]
        m = m.float()
        if m.shape[1] != h or m.shape[2] != w:
            m = F.interpolate(m.unsqueeze(1), size=(h, w), mode="bilinear", align_corners=False).squeeze(1)
        if m.shape[0] == 1 and b > 1:
            m = m.expand(b, -1, -1)
        elif m.shape[0] != b:
            m = m[:1].expand(b, -1, -1)
        if invert_mask:
            m = 1.0 - m
        m = m.clamp(0.0, 1.0).to(device=original_image.device)

        if color_match:
            band = _band_from_mask(m, fallback_px=max(8, int(feather)))
            if band is not None:
                edited_image = _color_match(edited_image, original_image, band, color_strength)
                print(f"[PreciseAlignComposite] color match applied (strength={color_strength})")

        m = _feather(m, int(feather))
        m = _apply_curve(m, curve)
        m = m.to(device=original_image.device, dtype=original_image.dtype).unsqueeze(-1)
        out = original_image * (1.0 - m) + edited_image * m
        print(f"[PreciseAlignComposite] composite {b} image(s) {w}x{h}, curve={curve}")
        return (out,)


NODE_CLASS_MAPPINGS = {
    "PreciseAlignPad": PreciseAlignPad,
    "PreciseAlignCropBack": PreciseAlignCropBack,
    "PreciseAlignCropEdit": PreciseAlignCropEdit,
    "PreciseAlignStitch": PreciseAlignStitch,
    "PreciseAlignComposite": PreciseAlignComposite,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PreciseAlignPad": "Precise Align · Pad",
    "PreciseAlignCropBack": "Precise Align · Crop Back",
    "PreciseAlignComposite": "Precise Align · Composite",
    "PreciseAlignCropEdit": "Precise Align · Crop Edit",
    "PreciseAlignStitch": "Precise Align · Stitch",
}
