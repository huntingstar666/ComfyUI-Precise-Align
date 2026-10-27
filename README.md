# ComfyUI-Precise-Align

> 一套给 **Qwen-Image 2.1** 做「局部编辑」的 ComfyUI 节点包。
> 目标很朴素：**编辑别跑偏，框外别乱动，接缝别明显，画框别费劲。**

不需要装第三方库，只用 ComfyUI 自带的 torch / numpy / Pillow；自带中文界面。

---

## 一、它解决哪些问题

| 问题 | 具体表现 | 本包的做法 |
|---|---|---|
| **编辑跑偏** | 明明框的是脸，结果衣服/背景被改，或整体偏移 | 补边到 32 的倍数 + `resolution=0`，让参考图和采样画布**格子完全一致** |
| **框外内容被改** | 只想改局部，结果整张图都变了 | 方案 A：`Set Latent Noise Mask`，采样时只对框内降噪，框外保留原 latent |
| **大图显存爆炸** | 4K 图只想改一个小物体，却要跑整张图 | 方案 B：把局部连同上下文裁出来，只采样这一块，再贴回原图 |
| **接缝明显 / 色差** | 贴回去有一圈硬边，或者颜色和周围不搭 | 高斯羽化 + `smoothstep` 过渡 + 过渡带颜色匹配 |
| **官方遮罩只有画笔** | 想画个精确的矩形/椭圆，只能拿画笔涂 | 内置 **Region** 节点：画矩形/椭圆，可拖动、缩放、删除 |
| **RGBA 报错** | 解码出来是 4 通道，和原图 3 通道合成时报“尺寸不一致” | `Composite` / `Stitch` 自动对齐通道（丢掉 alpha 或补 alpha=1） |
| **换模型不会对齐** | SDXL 要 8 的倍数、Flux 要 16 的倍数 | `multiple` 参数可选 8 / 16 / 32 / 64 |

---

## 二、原理（尽量说人话）

### 1. 为什么 Qwen-Image 2.1 会“跑偏”？

可以把模型想成两张**格子纸**：

- 一张是**参考图**，要喂给文本编码器；
- 一张是**采样画布**，KSampler 在这上面画图。

Qwen 2.1 的文本编码器有一个 `resolution` 参数，它会把参考图**按面积缩放**到大约 `resolution²` 像素，并且对齐到 32 的倍数。

如果缩放后的参考图尺寸 ≠ 采样画布尺寸，两张“格子纸”的格子数对不上，参考内容就会被塞到错误的位置——你看到的就是**像素偏移**。

**只有一种情况能完全对齐：参考图尺寸 = 采样画布尺寸。**

所以本包的做法是：

1. 先把图像**补边**到 `multiple` 的倍数（Qwen 2.1 用 32）；
2. 让编码器 `resolution=0`。

`resolution=0` 时，编码器不再做面积缩放，只把参考图对齐到 32 的倍数；而我们的图已经是 32 的倍数，于是它**原样使用**——尺寸完全一致，偏移消失。

> 补充一句：`resolution=0` 对任何“32 的倍数”的尺寸都成立，是一个稳定的零偏移点；
> 而 `resolution>0` 时，只有少数尺寸刚好满足对齐条件，日常很难碰巧撞上。

### 2. 补边 → 采样 → 裁回

```
原图
 └→ Pad：可选缩放 → 补边到 32 的倍数 → 得到“画布”
       ├→ 文本编码器（同一张画布）
       ├→ VAEEncode（同一张画布）
       └→ 输出 pad_info（记录补了多少、缩了多少）

采样 → VAE Decode
 └→ Crop Back：按 pad_info 精确裁回原尺寸（含缩放逆变换）
```

- 补边**不改变原图内容**，只是在外圈加边；
- `pad_info` 记录 left/top/缩放比例，`Crop Back` 反着算一遍，**纯切片，不重采样**；
- 遮罩也会跟着同一套坐标变换：原图遮罩 →（缩放）→ 补边画布；裁回时再变回原图尺寸。

### 3. 局部编辑的两种方案

#### 方案 A：全画布 + Latent 噪波遮罩

```
整张画布照常 VAEEncode → Set Latent Noise Mask（框内=1）
→ KSampler：只对框内降噪，框外保留原 latent
→ Decode → Crop Back → Composite（框外像素用原图盖回去）
```

- **优点**：模型能看到整张图，适合“加东西、换材质、改光影”这种需要和全局协调的编辑；
- **缺点**：整张图的 token 都要算，大图很吃显存；
- 关键：框外不是“生成后盖住”，而是在**采样阶段就保留原 latent**，所以框外更稳。

#### 方案 B：裁切 + 上下文 + 贴回

```
原图 → Crop Edit：把编辑区连同周边上下文裁出来，补到 32 倍数
→ 只采样这一小块 → Decode
→ Stitch：按 crop_info 贴回原图（羽化/颜色匹配）
```

- **优点**：只算一小块，省显存，适合**移除物体、局部修补、大图小改**；
- **缺点**：模型只看得到上下文，不适合需要全图一致性的改造；
- 裁切块要留足上下文：`context_factor` 1.3~1.6，`context_pixels` 64~128。

### 4. 接缝为什么能自然

三件事叠加：

1. **高斯羽化**：把遮罩边缘变软，不是一刀切；
2. **smoothstep 曲线**：过渡两端导数为 0，不会出现明显的“渐变带”；
3. **颜色匹配**：取遮罩边缘那一圈过渡带，统计编辑结果和原图的均值/标准差，把编辑结果的颜色“拉回”原图色调（Reinhard 仿射）。

推荐值：

- 方案 A：`feather=24~48`，`color_match=on`，`color_strength=0.4~0.7`；
- 方案 B：`blend_pixels=16~32`，`color_match=on`，`color_strength=0.7~1.0`。

### 5. RGBA 与 RGB

Qwen-Image 2.1 的 VAE 解码出来是 **4 通道 RGBA**，而 `Load Image` 通常是 **3 通道 RGB**。

直接比较 shape 会误报“尺寸不一致”。本包的 `Composite` / `Stitch` 会自动处理：

- 编辑结果 4 通道、原图 3 通道 → 丢掉 alpha；
- 原图 4 通道、编辑结果 3 通道 → 补 alpha=1。

控制台会打印一行 `[PreciseAlign] WARNING: ... 通道 ...`，这是正常提示，不是报错。

### 6. 画框编辑器是怎么工作的

`Region` 节点里存的是一段 JSON（矩形/椭圆的位置和大小）：

- 点节点上的 **✏️ 画框 / 画圈**，前端弹出画布；
- 画矩形/椭圆，可拖动、缩放、删除；
- 点“应用”把形状写回节点；
- 节点执行时，Python 把形状栅格化成遮罩，输出 `mask` 和包围盒。

编辑器需要一张图作背景：如果上游直接是 `Load Image`，它直接读原图；如果是中间链路，它会**只执行 Region 节点及上游**生成预览，不会跑下游采样，所以**不需要提前手动运行整个工作流**。

---

## 三、功能一览

### 6 个节点

| 节点 | 作用 |
|---|---|
| **Precise Align · Pad** | 缩放（可选）+ 补边到指定步长；输出画布图、`resolution=0`、画布遮罩、`pad_info` |
| **Precise Align · Crop Back** | 按 `pad_info` 把解码结果精确裁回原尺寸（含缩放逆变换），并输出裁回后的遮罩 |
| **Precise Align · Composite** | 方案 A 收尾：用遮罩把编辑结果合成回原图，框外 100% 保留原图像素 |
| **Precise Align · Crop Edit** | 方案 B：按框/遮罩裁出编辑区 + 上下文，补边到步长 |
| **Precise Align · Stitch** | 方案 B：把裁切结果贴回原图（羽化 + 可选颜色匹配） |
| **Precise Align · Region** | 画框/画圈，输出遮罩和包围盒；自带编辑器 |

### 其他能力

- **零偏移**：Qwen 2.1 下 `resolution=0`，参考图零缩放；
- **画框编辑器**：矩形/椭圆，拖动移动、8 点缩放、Delete 删除、Alt 强制新建、撤销/清空；
- **步长可选**：`multiple=8/16/32/64`，适配 SD/SDXL、Flux/SD3 等；
- **可选缩放**：`scale_by`（默认 1.0 关闭）、`scale_method`、`scale_step`；
- **遮罩处理**：外扩 `mask_expand`、高斯羽化 `mask_feather`、框选对齐 `mask_snap`；
- **接缝处理**：`feather`、`smoothstep/linear`、`color_match` + `color_strength`；
- **中文界面**：节点名、参数、下拉选项、tooltip、分类全部中文；
- **模板工作流**：Qwen-Image 2.1 方案 A 模板，开箱即用。

---

## 四、安装

1. 把整个 `ComfyUI-Precise-Align` 文件夹放到：
   ```
   ComfyUI/custom_nodes/ComfyUI-Precise-Align
   ```
2. 重启 ComfyUI；
3. 浏览器刷新页面。

节点在 **`图像 / 局部编辑`** 分类下（中文界面会自动显示为“局部编辑”）。

> 如果之前装过旧版 `ComfyUI-Qwen21-PadAlign`，先删除旧文件夹；
> 旧工作流里的 `Qwen21*` 节点需要重新拖入一次（节点 id 已改为 `PreciseAlign*`）。

### 中文界面

自带 `locales/zh/`，走 ComfyUI 原生 i18n：

- 设置 → Locale → 中文；
- **重启 ComfyUI 服务端**（翻译在启动时加载），再刷新页面。

参数在 API / 工作流 JSON 里仍然是英文键名，旧脚本兼容。

---

## 五、节点怎么用

### 5.1 方案 A：全画布局部编辑（推荐优先试）

连线：

```
Load Image
  └→ Precise Align · Pad（可接 Region.mask 或 box_*）
        ├ image ──────→ TextEncodeQwenImage21.images.image_1
        ├ image ──────→ VAEEncode.pixels
        ├ resolution ─→ TextEncodeQwenImage21.resolution   ← 必须接，值 0
        ├ mask ───────→ Set Latent Noise Mask.mask
        └ pad_info ───→ Crop Back.pad_info

VAEEncode → Set Latent Noise Mask → KSampler → VAE Decode → Crop Back
Crop Back.image + 原图 + Crop Back.mask → Composite → 保存
```

步骤：

1. `Load Image` 换图；
2. `Pad` 的 `multiple=32`（Qwen 2.1）；
3. 把 `Pad.image` **同时**接给文本编码器和 `VAEEncode`；
4. 把 `Pad.resolution` 接给编码器的 `resolution`；
5. 把 `Pad.mask` 接给 `Set Latent Noise Mask`；
6. 解码后接 `Crop Back`；
7. `Composite` 的 `original_image` 接原图，`edited_image` 接 `Crop Back.image`，`mask` 接 `Crop Back.mask`。

> **最容易接错的点**：`Composite.edited_image` 必须接 **Crop Back 的输出**，不能直接接 `VAE Decode`。
> 解码出来是补边画布尺寸，原图是原尺寸，两者对不上。

### 5.2 方案 B：裁切局部编辑（省显存）

连线：

```
Load Image
  └→ Precise Align · Crop Edit（接 Region.mask 或 box_*）
        ├ image ──────→ TextEncodeQwenImage21.images.image_1
        ├ image ──────→ VAEEncode.pixels
        ├ resolution ─→ TextEncodeQwenImage21.resolution   ← 0
        ├ mask ───────→（可选）Set Latent Noise Mask
        └ crop_info ──→ Stitch.crop_info

KSampler → VAE Decode → Stitch（original_image=原图，edited_image=解码结果）
```

- 接 `mask`：只改物体，上下文保持 VAE 编码；
- 不接 `mask`：整块重绘；
- `Stitch` 会按 `crop_info` 贴回，羽化 + 可选颜色匹配。

### 5.3 Region 画框 / 画圈

1. 添加 **Precise Align · Region**，`image` 接原图；
2. 点节点上的 **✏️ 画框 / 画圈**（或右键菜单）；
3. 在弹窗里画：
   - 空白处拖动 = 新建矩形/椭圆；
   - 点中形状 = 选中，拖动内部 = 移动；
   - 拖 8 个白点 = 缩放；
   - `Delete` / 「🗑 删除选中」= 删除；
   - 按住 `Alt` 拖 = 强制新建；
   - `Ctrl+Z` 撤销、`Esc` 取消选中/关闭；
4. 点 **应用**；
5. 把输出的 `mask` 接给 `Pad.mask` 或 `Crop Edit.mask`；
   `image` 是透传，方便串在链路里。

> 不需要先手动运行整个工作流。编辑器需要预览时会自动“只执行本节点及上游”。

### 5.4 参数速查

| 节点 | 参数 | 推荐 |
|---|---|---|
| **Pad** | `multiple` | Qwen 2.1：32；SD/SDXL：8；Flux/SD3：16 |
| | `pad_mode` / `align` | `edge` / `center` |
| | `mask_expand` / `mask_feather` | 8~16 / 24~48 |
| | `mask_snap` | `true` |
| | `scale_by` | `1.0`（不缩放） |
| **Crop Edit** | `context_factor` / `context_pixels` | 1.3~1.6 / 64~128 |
| | `multiple` | 32（Qwen 2.1） |
| **Crop Back** | `downscale_method` | `bicubic` |
| **Composite** | `feather` / `curve` | 24~48 / `smoothstep` |
| | `color_match` / `color_strength` | on / 0.4~0.7 |
| **Stitch** | `blend_pixels` / `curve` | 16~32 / `smoothstep` |
| | `color_match` / `color_strength` | on / 0.7~1.0 |
| **Region** | 形状 | 画完点“应用”再运行 |

### 5.5 模板工作流在哪里

仓库里：

```
example_workflows/precise_align_qwen21_local_edit.json
```

本机安装后也在：

```
ComfyUI/custom_nodes/ComfyUI-Precise-Align/example_workflows/
```

使用：

1. ComfyUI 里 **Workflow → Open**，选这个 JSON（或直接把文件拖进画布）；
2. 把 **Load Image** 换成你自己的图；
3. 三个模型 Loader 选择本机 Qwen-Image 2.1 模型（UNET / CLIP / VAE）；
4. 需要的话，在 **Region** 里重新画框；
5. 运行。

模板说明：

- 方案 A 完整链路（Pad / Region / Crop Back / Composite / Set Latent Noise Mask）；
- 原图文件名、提示词、生成文本、本地路径已全部替换为通用占位，**不包含个人信息**；
- `example.png` 是 ComfyUI 自带示例图；
- 提示词增强分支（TextGenerateLTX2Prompt / CLIPLoader 等）在模板里是**绕过状态**，不需要可以整组删掉。

---

## 六、常见问题

**Q1：编辑完位置偏了/内容错位？**
检查三件事：
1. `Pad.resolution` 是否接给了编码器的 `resolution`（必须是 0）；
2. 文本编码器和 `VAEEncode` 是否接的是**同一张** `Pad.image`；
3. KSampler 的 latent 尺寸是否就是 `Pad` 输出的画布尺寸（不要手动改 latent 尺寸）。

**Q2：整张图都变了，框外没保留？**
- 没接 `Pad.mask` / `Region.mask` 时，`Pad.mask` 输出全 1，等于全图编辑；
- 方案 A 要接 `Set Latent Noise Mask`；
- 最终还要接 `Composite`，并把 `edited_image` 接 `Crop Back.image`。

**Q3：Composite 报“尺寸不一致”？**
- `edited_image` 接错成 `VAE Decode`（画布尺寸）了，要接 `Crop Back.image`；
- 或者 `original_image` 接成了 `Pad.image`，要接原图；
- 如果尺寸一样大还报错，看控制台是不是 RGBA/RGB 通道不同（新版本会自动处理）。

**Q4：画框编辑器打不开 / 预览图 404？**
- 更新后必须**硬刷新前端**：F12 → Network → Disable cache → `Ctrl+Shift+R`；
- Console 里应看到 `[PreciseAlignRegion] editor build 1.0.13`；
- 旧版服务端没有版本接口也没关系，新前端会自动重新生成预览；
- 预览图保存在 `output/precise_align_preview/`，每个 Region 节点只留 1 张，目录最多 8 张、1 天过期。

**Q5：节点显示缺失 / 旧工作流打不开？**
节点 id 已从 `Qwen21*` 改为 `PreciseAlign*`，旧工作流里的节点需要重新拖入一次。

**Q6：支持其他模型吗？**
- **Qwen-Image 2.1**：`multiple=32` + `resolution=0`，零偏移；
- **SD1.5 / SDXL**：`multiple=8`，忽略 `resolution`；
- **Flux / SD3**：`multiple=16`，忽略 `resolution`；
- **Qwen 1.x / 2509 / 2511**：补边/裁切/合成仍可用，但它们的编码器会强制把参考图缩到 1MP，**零偏移不成立**，需要用 Cut / pixel-perfect 类方案。

**Q7：显存/速度怎么选方案？**
- 2MP 以内、需要全局协调 → 方案 A；
- 大图、只改小区域、移除物体 → 方案 B；
- 方案 B 的裁切块建议 ≤2MP。

---

## 七、模型兼容性

| 模型 | `multiple` | `resolution` 输出 |
|---|---|---|
| **Qwen-Image 2.1** | **32** | 接给 `TextEncodeQwenImage21.resolution`（0） |
| Qwen-Image 1.x / 2509 / 2511 | 32 | 不使用；它们会强制 1MP 缩放，零偏移不成立 |
| SD1.5 / SDXL | 8 | 忽略 |
| Flux / SD3 | 16 | 忽略 |
| 其他 | 按 VAE 倍数 8/16/32/64 | 忽略 |

---

## 八、边界与建议

| 维度 | 舒适 | 可用 | 不建议 |
|---|---|---|---|
| 单边 | 512~2048 | 256~3072 | <64 / >4096 |
| 宽高比 | 1:2~2:1 | 1:4~4:1 | 超过 1:8 |
| 总像素 | 0.3~2MP | 2~4MP | >4MP |

- 参考 token ≈ `(padded_w/32) × (padded_h/32)`（`multiple=32` 时）；>2048 看显存，>4096 压力较大；
- 方案 B 的裁切块上限同理；
- 节点会在边长 <64、宽高比 >8、token 过多、补边占比 >20% 时打印 `WARNING`。

---

## License

Apache-2.0
