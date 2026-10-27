# ComfyUI-Precise-Align

通用的「补边对齐 + 局部编辑 + 裁回/贴回」工具包，同时对 **Qwen-Image 2.1** 的零偏移做了专门优化。

包含两套方案：

- **方案 A：全画布局部编辑**（保全局信息，适合"加东西/换材质/局部改造"）
- **方案 B：裁切局部编辑**（省显存、上下文自足，适合"移除物体/局部修补"）

核心保证：

- 输出可**精确裁回/贴回**原图尺寸（纯切片，零重采样）；
- 补边步长可选（8/16/32/64，默认 32）；
- Qwen 2.1 下把 `resolution` 输出接给官方编码器（值 0），参考图零缩放 → 无尺寸导致的像素偏移；
- 缩放功能可选（默认 1.0，完全跳过，不引入重采样）。

---

## 方案怎么选

| | 方案 A 全画布 | 方案 B 裁切 |
|---|---|---|
| 画布 | 整图（缩放+补边） | 框选区域 + 上下文 |
| 全局信息 | ✅ 完整 | ⚠️ 只有上下文 + 局部 |
| 显存/速度 | ❌ 全图 token 都算 | ✅ 只算裁切块 |
| 局部性 | ✅ latent noise mask 硬约束 | ✅ 天然（整块就是编辑区） |
| 接缝 | 小（mask 羽化） | 有（贴回时羽化） |
| 适合 | 增加物品、换材质、改光影、需要和全图协调的编辑 | 移除物体、局部修补、大图上的小改动、批量 |
| 不适合 | 大图（>2MP）上的小改动（浪费算力） | 需要全局一致性的编辑（容易突兀） |

## 模型兼容性

| 模型 | 用法 |
|---|---|
| **Qwen-Image 2.1** | `multiple=32`，把 `resolution` 输出接给 `TextEncodeQwenImage21.resolution`（值是 0）→ 零偏移 |
| Qwen-Image 1.x / 2509 / 2511 | 补边/裁切/合成/贴回通用；但它们的编码器会强制把参考图缩到 1MP，**零偏移不成立**，需要 Cut / pixel-perfect 类方案 |
| SD1.5 / SDXL | 建议 `multiple=8`；补边、遮罩、裁切、贴回都能直接用；忽略 `resolution` 输出 |
| Flux / SD3 | 建议 `multiple=16`；同上 |
| 其他图像模型 | 按自身 VAE 倍数选 8/16/32/64；`resolution` 输出仅 Qwen 2.1 使用 |

> `resolution` 输出恒为 0，只对 Qwen 2.1 有意义；其他模型**不要接**。

---

## 安装

把 `ComfyUI-Precise-Align` 文件夹放进：

```
ComfyUI/custom_nodes/ComfyUI-Precise-Align
```

重启 ComfyUI，节点在 **`图像/局部编辑`** 分类下。无第三方依赖（只用 torch）。

> 节点类型 id 已统一为 `PreciseAlign*`；如果你之前装过旧版（`Qwen21*`），请先删除旧的 `ComfyUI-Qwen21-PadAlign` 文件夹，旧工作流里的节点需要重新拖入一次。

## 中文界面（自带原生 i18n）

本包自带 `locales/zh/`，使用 ComfyUI **原生本地化机制**（`/i18n` 接口自动加载 custom_nodes 下的 `locales/` 目录）：

- **节点名**：中文（如"补边对齐（方案A）""裁切局部（方案B）"）
- **参数名**：中文（如"补边方式""框选宽度""对齐步长""遮罩羽化"）
- **下拉选项**：中文（如"边缘复制（推荐）""双三次"）
- **输出名**：中文（如"补边信息""裁切信息""遮罩"）
- **分类**：`图像/局部编辑`
- **悬停说明**：中文 tooltip

启用方式：ComfyUI 设置 → Locale → 中文；**重启 ComfyUI 服务端**（翻译在启动时缓存）并刷新页面。
参数在 API / 工作流 JSON 里的键名仍是英文，保证旧工作流和脚本兼容。

---

## 节点

### 方案 A

#### A-1. 补边对齐 (Pad to Multiple)

| 输入 | 说明 |
|---|---|
| `image` | 原始图像 |
| `pad_mode` | `edge`（默认，边缘复制）/ `reflect` / `gray` / `black` / `white` |
| `align` | `center`（默认）/ `top_left` |
| `pad_value` | gray 模式的灰度 |
| `multiple` | 对齐步长：Qwen 2.1 用 32；SD/SDXL 可用 8；Flux/SD3 可用 16 |
| `scale_by` | 缩放倍数，**默认 1.0 = 不缩放** |
| `scale_method` | `lanczos`（默认）/ `bicubic` / `bilinear` / `area` / `nearest-exact` |
| `scale_step` | 缩放后宽高对齐步长（默认 32） |
| `mask` | 可选：原图尺寸的遮罩（框选区域） |
| `box_x / box_y / box_w / box_h` | 可选：矩形框（原图坐标），`box_w/box_h > 0` 时生效 |
| `mask_expand` / `mask_feather` | 遮罩外扩（像素）/ 高斯羽化半径（像素） |
| `mask_snap` | 框选矩形向外对齐到步长网格（默认开，避免 latent 半格硬边） |

| 输出 | 说明 |
|---|---|
| `image` | 缩放+补边后的图（喂编码器和 VAEEncode） |
| `height / width` | 画布尺寸 |
| `resolution` | 恒 0（Qwen 2.1 专用；其他模型忽略） |
| `pad_info` | 给 A-2 |
| `mask` | 画布对齐的遮罩（框内 1、框外 0；未选区域时全 1）→ 接 `Set Latent Noise Mask` |

#### A-2. 裁回 (Crop Back)

输入 `image`（解码结果）+ `pad_info`；可选 `mask`（接 A-1 的 mask）、`downscale_method`。
输出 `image`（原尺寸）、`mask`（原尺寸）。

#### A-3. 合成未编辑区 (Composite Back)

输入：`original_image`、`edited_image`、`mask`、`invert_mask`、`feather`（高斯羽化）、
`curve`（`smoothstep`/`linear`）、`color_match`、`color_strength`。
输出合成图。**唯一能保证未编辑区像素/色彩与原图完全一致的方式。**

- `color_match=true`：用过渡带的均值/标准差把编辑结果的颜色拉回原图（Reinhard 仿射），
  适合"加物品/换材质后色调不接"的情况；`color_strength` 控制强度（默认 0.8）。

### 方案 B

#### B-1. 裁切局部 (Crop Edit)

| 输入 | 说明 |
|---|---|
| `image` | 原始图像 |
| `context_factor` | 编辑区外扩倍数（默认 1.5） |
| `context_pixels` | 额外外扩像素（默认 64） |
| `pad_mode / align / pad_value` | 补边方式 |
| `multiple` | 对齐步长（Qwen 2.1 用 32；SD/SDXL 可用 8；Flux/SD3 可用 16） |
| `scale_by / scale_method / scale_step` | 可选缩放（如把小图放大到 1MP 再编辑） |
| `mask` 或 `box_*` | 编辑区域（二者取一，mask 优先） |
| `mask_expand / mask_feather` | 遮罩外扩/羽化 |

输出：`image`（裁切+补边后的编辑块）、`height`、`width`、`resolution`（恒 0，Qwen 2.1 专用）、`crop_info`、`mask`。

#### B-2. 贴回 (Stitch Back)

输入 `original_image`、`edited_image`（解码结果）、`crop_info`；可选 `mask`（接 B-1）、`blend_pixels`（默认 16）、`upscale_method`、`curve`（默认 `smoothstep`）、`color_match`、`color_strength`。
输出贴回后的整图。`color_match` 用裁切块边缘的过渡带做颜色校正，贴回前先把色调拉回原图。

#### R-1. 画框 / 画圈 (Region)

自带形状编辑器，不依赖官方遮罩编辑器（官方只有画笔/橡皮）。

1. 添加 **Precise Align · Region (画框/画圈)**，`image` 接原图；
2. 直接点节点上的 **✏️ 画框 / 画圈** 按钮（或右键菜单）——**不需要提前运行**：
   - 上游是 `Load Image` 时直接读取原图；
   - 上游是中间链路时，自动**只执行本节点及上游**生成预览，不会跑下游采样；
3. 在弹窗里用「矩形 / 圆形」拖动绘制，可多个形状，支持撤销/清空/Ctrl+Z；
4. 点「应用」写回形状，再运行工作流，输出 `mask` 与包围盒 `box_x/y/w/h`。

输出 `mask` 直接接 **Pad / Crop Edit 的 `mask` 输入**即可；`image` 是透传，方便串在链路里。
`box_x/y/w/h` 是联合包围盒，接上也无害，但 `mask` 优先（只接 image + mask 即可）。
遮罩坐标按原图记录，节点内部会随缩放/补边自动映射。

> 编辑器预览图保存在 `output/precise_align_preview/`：**每个 Region 节点只保留 1 张**（按节点 id 覆盖），整个目录最多 8 张、超过 1 天自动删除，不会堆积。若仍遇到旧预览失效，点按钮会自动**只执行本节点及上游**重新生成。
**更新后至少要做这一步（否则会继续运行旧前端）：**
> 浏览器 F12 → Network 勾选 **Disable cache** → **Ctrl+F5**（或 Ctrl+Shift+R）；
> Console 应出现 `[PreciseAlignRegion] editor build 1.0.11`。
>
> 建议同时**完全重启 ComfyUI 进程**（服务端 Python 也有更新，重启后预览会持久化到 output）；
> 服务端日志会出现 `[PreciseAlign] build 1.0.11 loaded`，Console 会出现
> `[PreciseAlignRegion] server build 1.0.11 ok`。
> 若暂时不重启：新前端在点按钮时会自动只执行本节点及上游来生成预览，编辑器仍可用。
>
> 如果页面顶部出现红色提示条，说明前端/服务端版本不一致：浏览器缓存没清或服务端没重启。


---

## 示例 / 模板工作流

- `example_workflows/precise_align_qwen21_local_edit.json`：方案 A（全画布局部编辑）Qwen-Image 2.1 模板，包含 Pad / Region / Crop Back / Composite 的完整连线与 Set Latent Noise Mask。
- 使用前把 **Load Image** 换成自己的图；三个模型 Loader 选择本机 Qwen-Image 2.1 模型；Region 节点里已预置一个居中示例框，可重新画。
- 原始工作流里的提示词、图片文件名、本地路径已全部替换为通用占位；`example.png` 是 ComfyUI 自带示例图。
- 提示词增强分支（TextGenerateLTX2Prompt / CLIPLoader 等）是绕过状态，不需要可直接删除。

## 标准连线

### 方案 A：全画布局部编辑

```
Load Image
  └→ A-1 Pad to Multiple（可接 mask/box）
        ├ image ──────→ TextEncodeQwenImage21.images.image_1（Qwen 2.1）
        ├ image ──────→ VAEEncode
        ├ resolution ─→ TextEncodeQwenImage21.resolution   (0，Qwen 2.1 专用)
        ├ mask ───────→ Set Latent Noise Mask.mask
        └ pad_info ───→ A-2 pad_info
VAEEncode → Set Latent Noise Mask → KSampler → VAE Decode → A-2
A-2.image + A-2.mask + 原图 → A-3 → 输出
```

> `Set Latent Noise Mask` 是官方节点（`model/latent` 分类）。它让采样器**只对 mask 内降噪**，mask 外保留原 latent，从而"全局信息不丢、只改框内"。

### 方案 B：裁切局部编辑

```
Load Image
  └→ B-1 Crop Edit（mask/box + 上下文）
        ├ image ──────→ TextEncodeQwenImage21.images.image_1（Qwen 2.1）
        ├ image ──────→ VAEEncode
        ├ resolution ─→ TextEncodeQwenImage21.resolution   (0，Qwen 2.1 专用)
        ├ mask ───────→（可选）Set Latent Noise Mask
        └ crop_info ──→ B-2 crop_info
KSampler → VAE Decode → B-2 Stitch Back（+ 原图）
```

> B 方案里 `mask` 可选：接上就只改物体（上下文保持 VAE 编码），不接就整块重绘。贴回时用同一个 mask 做混合。

---

## 缩放说明

- 三个缩放参数（`scale_by` / `scale_method` / `scale_step`）默认显示在节点上；`scale_by=1.0` 即关闭缩放。
- `scale_by=1.0`（默认）**完全跳过缩放**，不产生任何重采样；
- 缩放后按 `scale_step` 对齐宽高（默认 32），再补边兜底；
- A-2 会自动做缩放逆变换（裁回后缩回原尺寸）；
- B-2 会自动把裁切块缩回原尺寸后贴回。

---

## 框选/遮罩从哪来

- **本包自带编辑器（推荐）**：`Precise Align · Region` → 先运行一次 → 点「✏️ 画框 / 画圈」→ 画矩形/椭圆 → 应用 → `mask` 接 Pad / Crop Edit；
- **ComfyUI 自带 MaskEditor**：右键 LoadImage → 打开遮罩编辑器（只有画笔/橡皮）→ 涂抹 → 保存，mask 接 A-1/B-1；
- **数字框**：直接填 `box_x/box_y/box_w/box_h`；
- 也可以用 `Bounding Box` 原始节点读出四个数值，手动填入 `box_*`。
- 自定义"弹窗红色选框"属于后续可选功能（需要写前端扩展），当前用以上方式即可。

---

## 原理要点

1. **局部编辑的实现**：方案 A 用 ComfyUI 的 **latent noise mask**（采样器层面硬约束，只对 mask 内降噪）；方案 B 直接裁切画布、只生成编辑区。Qwen 2.1 没有 mask 条件输入，所以只能这样实现。
2. **Qwen 2.1 下参考图必须和采样画布同尺寸**，否则内容会按尺寸比例缩放（像素偏移）。A-1/B-1 输出的 image 同时喂编码器和 VAEEncode 即可保证。
3. noise mask 在 latent 上按 16px 量化，**框边建议对齐步长、羽化 8~16px**，否则接缝明显。
4. 即便尺寸完全对齐，模型仍会重绘；**未编辑区要 100% 保持，必须用 A-3 合成**（或 B 方案贴回）。

---

## 接缝优化

1. **框边对齐**：`mask_snap=true`（默认）会把框选矩形向外对齐到步长网格（Qwen 2.1 用 32），避免 latent 半格硬边。
2. **羽化**：`mask_feather` 已改为**高斯羽化**（可分离卷积），比盒式模糊平滑，不会出现线性渐变带；
   建议 24~48px（约 1.5~3 个 latent 格）。
3. **过渡曲线**：A-3 / B-2 的 `curve` 默认 `smoothstep`，两端导数为 0，接缝过渡更自然。
4. **颜色匹配**：A-3 / B-2 打开 `color_match`，用**过渡带**（0<mask<1 的环带）的均值/标准差做仿射校正，
   只把颜色往原图拉，不会整体改写编辑区。移除/修补类（B）建议 `color_strength=0.7~1.0`；
   加物品/换材质（A）建议 `0.4~0.7`。
5. **低噪融合 pass**（工作流级，无需新节点）：第一次出图后，把合成结果再做一次
   `VAEEncode → Set Latent Noise Mask（mask 外扩 32、羽化 32）→ KSampler(denoise 0.2~0.3, 8~12 步, euler) → 解码`，
   让模型专门调和边界。

推荐参数：A 方案 `mask_snap=on, expand=16, feather=32, curve=smoothstep, color_match=on(0.6)`；
B 方案 `mask_snap=on, expand=12, feather=24, blend_pixels=32, curve=smoothstep, color_match=on(0.8)`。

---

> **RGBA 说明**：Qwen-Image 2.1 的 VAE 解码输出是 **4 通道 RGBA**，而 Load Image 通常是 3 通道 RGB。
> `Composite` / `Stitch` 会自动把编辑结果对齐到原图通道数（多则丢弃 alpha，少则补 alpha=1），
> 控制台会打印 `[PreciseAlign] WARNING: ... 通道 ...`，不需要手动接转换节点。

## 边界

| 维度 | 舒适 | 可用 | 不建议 |
|---|---|---|---|
| 单边 | 512~2048 | 256~3072 | <64 / >4096 |
| 宽高比 | 1:2~2:1 | 1:4~4:1 | 超过 1:8 |
| 总像素 | 0.3~2MP | 2~4MP | >4MP |

参考 token ≈ `(padded_w/32) × (padded_h/32)`（仅当 `multiple=32`、即 Qwen 2.1 时成立）；>2048 看显存，>4096 压力很大。
方案 B 的裁切块上限同理（建议 ≤2MP）。

节点会在以下情况打印 `WARNING`：边长 <64、宽高比 >8、token >2048/4096、补边占比 >20%。

---

## 控制台日志

```
[PreciseAlignPad] 1920x1080 -> 1920x1088 (scale=1.0, left=0, top=4, mask=yes)
[PreciseAlignCropBack] crop x=0, y=4, 1920x1088 -> resize 1920x1080
[PreciseAlignCropEdit] region=(300, 200, 200, 100) -> crop x=250, y=175, 300x150 -> canvas 320x160 (scale=1.0)
[PreciseAlignStitch] stitch at x=250, y=175, 300x150 (blend=16)
```

`WARNING: decoded ... != padded ...` 表示采样尺寸不是补边尺寸：检查 `resolution` 是否接了 0、KSampler 的 latent 是否来自补边图。

---

## 与 BlueBox Cut 的区别

| | 本包 A（Qwen 2.1 模式） | Cut |
|---|---|---|
| 网格对齐 | ✅ `R=0` 恒等 | ✅ 不动点恒等 |
| 是否改原图内容 | ❌ 只补边/缩放（可选） | 可能裁/补/缩放 |
| 工作分辨率 | 保持原生（token 多） | 可压到 1~2MP |
| 局部编辑 | ✅ latent noise mask | ❌ 无 |

本包还多了方案 B（裁切）和缩放，两者几何层面等价。

---

## 实测

- 14 种尺寸 × 2 种 align 补边→裁回逐像素一致；
- `multiple=8/16/32/64`：画布均对齐到对应步长，裁回 `torch.equal` 一致，mask 起点也对齐到步长；
- 缩放 1.5×/2× 后裁回形状正确；
- 方案 B 恒等测试：`edited = 裁切块` 时，贴回结果与原图 `torch.equal`；
- 框选 mask 坐标映射误差 ≤1px；
- 边角区域裁切、纯 mask 输入、尺寸不匹配容错、合成 mask 0/1 均通过；
- 中文 locale 与节点 schema 全量校验通过（参数名/选项/输出一一对应）。

---

## License

Apache-2.0

