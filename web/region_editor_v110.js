import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_NAME = "PreciseAlignRegion";
const PA_EDITOR_VERSION = "1.0.10";
console.log(`[PreciseAlignRegion] editor build ${PA_EDITOR_VERSION}`);

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function viewUrl(info) {
  const q = new URLSearchParams({
    filename: info.filename || "",
    subfolder: info.subfolder || "",
    type: info.type || "input",
  });
  const path = `/view?${q.toString()}`;
  try {
    return api.apiURL(path);
  } catch (e) {
    return path;
  }
}

function normInfo(im) {
  return infoFromImageLike(im);
}

function infoFromImageLike(im) {
  if (!im) return null;
  if (typeof im === "string") return fromWidgetValue(im);
  if (im.filename) {
    return { filename: im.filename, subfolder: im.subfolder || "", type: im.type || "temp" };
  }
  if (im.src) {
    try {
      const u = new URL(im.src, window.location.href);
      const filename = u.searchParams.get("filename");
      if (filename) {
        return {
          filename,
          subfolder: u.searchParams.get("subfolder") || "",
          type: u.searchParams.get("type") || "temp",
        };
      }
    } catch (e) {
      /* ignore */
    }
  }
  return null;
}

function fromWidgetValue(v) {
  if (typeof v !== "string" || !v.trim()) return null;
  const s = v.replace(/\\/g, "/");
  const idx = s.lastIndexOf("/");
  if (idx > 0) {
    return { filename: s.slice(idx + 1), subfolder: s.slice(0, idx), type: "input" };
  }
  return { filename: s, subfolder: "", type: "input" };
}

function findUpstreamImage(node) {
  if (Array.isArray(node._paImages) && node._paImages.length) {
    const info = normInfo(node._paImages[node._paImages.length - 1]);
    if (info) return info;
  }
  // node.imgs（部分前端会给带 ui.images 的节点自动挂上）
  if (Array.isArray(node.imgs) && node.imgs.length) {
    const info = infoFromImageLike(node.imgs[node.imgs.length - 1]);
    if (info) return info;
  }
  let cur = node;
  for (let depth = 0; depth < 6; depth++) {
    const inp = cur.inputs?.find((i) => i.type === "IMAGE" && i.link != null);
    if (!inp) break;
    const link = cur.graph?.links?.[inp.link];
    if (!link) break;
    const origin = cur.graph.getNodeById(link.origin_id);
    if (!origin) break;
    if (Array.isArray(origin.imgs) && origin.imgs.length) {
      const info = infoFromImageLike(origin.imgs[origin.imgs.length - 1]);
      if (info) return info;
    }
    for (const wname of ["image", "image_upload", "filename"]) {
      const w = origin.widgets?.find(
        (w) => w.name === wname && typeof w.value === "string" && w.value.trim().length > 0
      );
      const info = w ? fromWidgetValue(w.value) : null;
      if (info) return info;
    }
    cur = origin;
  }
  return null;
}

function loadShapes(raw, nw, nh) {
  if (!raw) return [];
  try {
    const d = JSON.parse(raw);
    const sw = +d.width || nw;
    const sh = +d.height || nh;
    const sx = nw / sw;
    const sy = nh / sh;
    return (Array.isArray(d.shapes) ? d.shapes : [])
      .filter((s) => s && s.w > 0 && s.h > 0)
      .map((s) => ({
        type: s.type === "ellipse" || s.type === "circle" ? "ellipse" : "rect",
        x: +s.x * sx,
        y: +s.y * sy,
        w: +s.w * sx,
        h: +s.h * sy,
      }));
  } catch (e) {
    return [];
  }
}

let styleInjected = false;
function injectStyle() {
  if (styleInjected) return;
  styleInjected = true;
  const el = document.createElement("style");
  el.textContent = `
  .pa-overlay { position: fixed; inset: 0; z-index: 100000; background: rgba(0,0,0,.62);
    display: flex; align-items: center; justify-content: center; }
  .pa-panel { background: #1c1f24; color: #e8eaf0; border: 1px solid #3a3f47;
    border-radius: 10px; box-shadow: 0 18px 50px rgba(0,0,0,.55); max-width: 96vw; }
  .pa-head { display: flex; align-items: center; justify-content: space-between;
    padding: 10px 14px; border-bottom: 1px solid #333842; font-size: 14px; font-weight: 600; }
  .pa-head small { color: #9aa3af; font-weight: 400; margin-left: 8px; }
  .pa-tools { display: flex; gap: 8px; align-items: center; padding: 10px 14px; flex-wrap: wrap; }
  .pa-btn { background: #2a2f36; border: 1px solid #3d444e; color: #dfe3e8;
    padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .pa-btn:hover { background: #343b45; }
  .pa-btn.active { background: #8a2b2b; border-color: #e04b4b; color: #fff; }
  .pa-btn.primary { background: #2f6f3e; border-color: #3f9d55; }
  .pa-btn.danger { background: #4a2a2a; border-color: #7a3a3a; }
  .pa-canvas-wrap { padding: 0 14px; overflow: auto; max-height: calc(100vh - 200px); }
  .pa-canvas { display: block; cursor: crosshair; background: #111;
    border: 1px solid #3a3f47; border-radius: 4px; }
  .pa-foot { display: flex; justify-content: space-between; align-items: center;
    gap: 10px; padding: 10px 14px; border-top: 1px solid #333842; }
  .pa-count { color: #9aa3af; font-size: 12px; }
  .pa-hint { color: #9aa3af; font-size: 12px; padding: 0 14px 8px; }
  `;
  document.head.appendChild(el);
}

// ---------------------------------------------------------------------------
// editor
// ---------------------------------------------------------------------------

function waitForPreview(node, timeoutMs = 20000) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (imgs) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      try {
        api.removeEventListener("executed", onExecutedEvt);
      } catch (e) {
        /* ignore */
      }
      resolve(imgs);
    };
    const onExecutedEvt = (evt) => {
      const d = evt?.detail || {};
      const nid = d.display_node ?? d.node;
      if (String(nid) !== String(node.id)) return;
      const imgs = d.output?.images || d.images;
      if (Array.isArray(imgs) && imgs.length) {
        node._paImages = imgs;
        finish(imgs);
      }
    };
    const timer = setTimeout(() => finish(null), timeoutMs);
    try {
      api.addEventListener("executed", onExecutedEvt);
    } catch (e) {
      /* ignore */
    }
  });
}

function showBusy(text) {
  injectStyle();
  const el = document.createElement("div");
  el.className = "pa-overlay";
  el.innerHTML = `<div class="pa-panel" style="padding:18px 24px;font-size:13px">${text}</div>`;
  document.body.appendChild(el);
  return () => el.remove();
}

async function refreshPreview(node) {
  // 清掉可能已失效的旧预览（ComfyUI 启动时会清理 temp），只执行本节点及上游
  node._paImages = null;
  console.log("[PreciseAlignRegion] refreshing preview via partial execution...");
  const busy = showBusy("正在生成预览…（只执行本节点及上游）");
  try {
    const wait = waitForPreview(node);
    await app.queuePrompt(-1, 1, { queueNodeIds: [String(node.id)] });
    const imgs = await wait;
    if (Array.isArray(imgs) && imgs.length) node._paImages = imgs;
  } catch (e) {
    console.warn("[PreciseAlignRegion] partial execution failed:", e);
  } finally {
    busy();
  }
}

function tryOpenEditor(node, widget, info, allowRefresh) {
  const candidates = [];
  for (const t of [info.type || "temp", "input", "temp"]) {
    if (!candidates.some((c) => c.type === t)) candidates.push({ ...info, type: t });
  }
  let idx = 0;
  const tryNext = async () => {
    if (idx >= candidates.length) {
      if (allowRefresh) {
        await refreshPreview(node);
        const fresh = findUpstreamImage(node);
        console.log("[PreciseAlignRegion] refreshed image info:", fresh);
        if (
          fresh &&
          (fresh.filename !== info.filename ||
            (fresh.type || "temp") !== (info.type || "temp"))
        ) {
          tryOpenEditor(node, widget, fresh, false);
          return;
        }
      }
      alert(
        "预览图加载失败：\n" +
          viewUrl(info) +
          "\n\n已尝试重新生成预览但仍失败。请确认 image 输入已接图，且工作流没有报错。" +
          `\n\n(预览器版本 ${PA_EDITOR_VERSION})`
      );
      return;
    }
    const cand = candidates[idx++];
    const img = new Image();
    img.onload = () => buildModal(node, widget, img);
    img.onerror = () => {
      console.warn("[PreciseAlignRegion] preview load failed:", viewUrl(cand));
      tryNext();
    };
    img.src = viewUrl(cand);
  };
  tryNext();
}

async function openEditor(node) {
  injectStyle();
  const widget = node.widgets?.find((w) => w.name === "shapes_json");
  if (!widget) {
    alert("Precise Align Region：找不到 shapes_json 控件。");
    return;
  }
  let info = findUpstreamImage(node);
  if (!info) {
    await refreshPreview(node);
    info = findUpstreamImage(node);
  }
  console.log("[PreciseAlignRegion] image info:", info, "paImages:", node._paImages);
  if (!info) {
    alert(
      "自动生成预览失败。\n\n" +
        "请确认：\n" +
        "1) 本节点的 image 输入已经接上图像；\n" +
        "2) 工作流可以正常执行（没有缺模型/报错）。\n\n" +
        "也可以先点一次“运行”，再重新点“✏️ 画框 / 画圈”。" +
        `\n\n(预览器版本 ${PA_EDITOR_VERSION})`
    );
    return;
  }
  tryOpenEditor(node, widget, info, true);
}

function buildModal(node, widget, img) {
  injectStyle();

  const nw = img.naturalWidth || img.width;
  const nh = img.naturalHeight || img.height;
  const maxW = Math.max(220, Math.min(window.innerWidth - 80, 1100));
  const maxH = Math.max(160, window.innerHeight - 220);
  const scale = Math.min(maxW / nw, maxH / nh);
  const cssW = Math.max(120, Math.round(nw * scale));
  const cssH = Math.max(80, Math.round(nh * scale));

  let shapes = loadShapes(widget.value, nw, nh);
  let tool = "rect";
  let drawing = null;
  let start = null;

  const overlay = document.createElement("div");
  overlay.className = "pa-overlay";
  const panel = document.createElement("div");
  panel.className = "pa-panel";

  const head = document.createElement("div");
  head.className = "pa-head";
  head.innerHTML = `画框 / 画圈<small>${nw} × ${nh} · 红框为编辑区域</small>`;

  const tools = document.createElement("div");
  tools.className = "pa-tools";

  const btnRect = document.createElement("button");
  btnRect.className = "pa-btn active";
  btnRect.textContent = "▭ 矩形";
  const btnEllipse = document.createElement("button");
  btnEllipse.className = "pa-btn";
  btnEllipse.textContent = "◯ 圆形 / 椭圆";
  const btnUndo = document.createElement("button");
  btnUndo.className = "pa-btn";
  btnUndo.textContent = "↶ 撤销";
  const btnClear = document.createElement("button");
  btnClear.className = "pa-btn danger";
  btnClear.textContent = "清空";
  tools.append(btnRect, btnEllipse, btnUndo, btnClear);

  const wrap = document.createElement("div");
  wrap.className = "pa-canvas-wrap";
  const canvas = document.createElement("canvas");
  canvas.className = "pa-canvas";
  canvas.width = nw;
  canvas.height = nh;
  canvas.style.width = cssW + "px";
  canvas.style.height = cssH + "px";
  wrap.appendChild(canvas);

  const hint = document.createElement("div");
  hint.className = "pa-hint";
  hint.textContent = "拖动鼠标画形状；可画多个。应用后由节点生成遮罩。";

  const foot = document.createElement("div");
  foot.className = "pa-foot";
  const count = document.createElement("span");
  count.className = "pa-count";
  const actions = document.createElement("div");
  actions.style.display = "flex";
  actions.style.gap = "8px";
  const btnCancel = document.createElement("button");
  btnCancel.className = "pa-btn";
  btnCancel.textContent = "取消";
  const btnApply = document.createElement("button");
  btnApply.className = "pa-btn primary";
  btnApply.textContent = "应用";
  actions.append(btnCancel, btnApply);
  foot.append(count, actions);

  panel.append(head, tools, wrap, hint, foot);
  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  document.body.style.overflow = "hidden";

  const ctx = canvas.getContext("2d");

  function updateCount() {
    count.textContent = `形状：${shapes.length} 个`;
  }

  function drawShape(s, dashed) {
    ctx.save();
    if (dashed) ctx.setLineDash([Math.max(6, nw / 160), Math.max(4, nw / 260)]);
    ctx.lineWidth = Math.max(2, Math.round(nw / 480));
    ctx.strokeStyle = "#ff2d2d";
    ctx.fillStyle = "rgba(255,45,45,0.22)";
    ctx.beginPath();
    if (s.type === "ellipse" || s.type === "circle") {
      ctx.ellipse(
        s.x + s.w / 2,
        s.y + s.h / 2,
        Math.max(s.w / 2, 0.5),
        Math.max(s.h / 2, 0.5),
        0,
        0,
        Math.PI * 2
      );
    } else {
      ctx.rect(s.x, s.y, s.w, s.h);
    }
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  function render() {
    ctx.clearRect(0, 0, nw, nh);
    ctx.drawImage(img, 0, 0, nw, nh);
    for (const s of shapes) drawShape(s, false);
    if (drawing) drawShape(drawing, true);
    updateCount();
  }

  function toImage(e) {
    const r = canvas.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * nw;
    const y = ((e.clientY - r.top) / r.height) * nh;
    return {
      x: Math.max(0, Math.min(nw, x)),
      y: Math.max(0, Math.min(nh, y)),
    };
  }

  function setTool(t) {
    tool = t;
    btnRect.classList.toggle("active", t === "rect");
    btnEllipse.classList.toggle("active", t === "ellipse");
    canvas.style.cursor = "crosshair";
  }

  function close() {
    document.body.style.overflow = "";
    document.removeEventListener("keydown", onKey);
    overlay.remove();
  }

  function onKey(e) {
    if (e.key === "Escape") close();
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
      e.preventDefault();
      shapes.pop();
      render();
    }
  }

  canvas.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    start = toImage(e);
    drawing = { type: tool, x: start.x, y: start.y, w: 0, h: 0 };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drawing || !start) return;
    const p = toImage(e);
    drawing.x = Math.min(start.x, p.x);
    drawing.y = Math.min(start.y, p.y);
    drawing.w = Math.abs(p.x - start.x);
    drawing.h = Math.abs(p.y - start.y);
    render();
  });
  canvas.addEventListener("pointerup", (e) => {
    if (!drawing) return;
    canvas.releasePointerCapture?.(e.pointerId);
    if (drawing.w >= 3 && drawing.h >= 3) {
      shapes.push({
        type: drawing.type,
        x: drawing.x,
        y: drawing.y,
        w: drawing.w,
        h: drawing.h,
      });
    }
    drawing = null;
    start = null;
    render();
  });
  canvas.addEventListener("pointercancel", () => {
    drawing = null;
    start = null;
    render();
  });

  btnRect.onclick = () => setTool("rect");
  btnEllipse.onclick = () => setTool("ellipse");
  btnUndo.onclick = () => {
    shapes.pop();
    render();
  };
  btnClear.onclick = () => {
    shapes = [];
    render();
  };
  btnCancel.onclick = close;
  btnApply.onclick = () => {
    const clean = shapes.map((s) => ({
      type: s.type,
      x: Math.round(s.x),
      y: Math.round(s.y),
      w: Math.round(s.w),
      h: Math.round(s.h),
    }));
    widget.value = JSON.stringify({ width: nw, height: nh, shapes: clean });
    node.setDirtyCanvas(true, true);
    app.graph?.setDirtyCanvas?.(true, true);
    close();
  };

  document.addEventListener("keydown", onKey);
  render();
}

// ---------------------------------------------------------------------------
// extension
// ---------------------------------------------------------------------------

function showStaleBanner(message) {
  if (document.getElementById("pa-stale-banner")) return;
  const el = document.createElement("div");
  el.id = "pa-stale-banner";
  el.style.cssText =
    "position:fixed;top:0;left:0;right:0;z-index:100001;background:#b91c1c;color:#fff;" +
    "font:13px/1.6 system-ui,sans-serif;padding:10px 16px;" +
    "box-shadow:0 2px 8px rgba(0,0,0,.4);white-space:pre-wrap;";
  el.textContent = message;
  document.body.appendChild(el);
}

async function checkServerBuild() {
  let url = "/api/precise_align/version";
  try {
    url = api.apiURL("/precise_align/version");
  } catch (e) {
    /* ignore */
  }
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    if (String(d.version) !== PA_EDITOR_VERSION) {
      showStaleBanner(
        `Precise Align 版本不一致：前端 v${PA_EDITOR_VERSION} / 服务端 v${d.version}。\n` +
          "请完全重启 ComfyUI 进程（关闭后重新启动），然后 Ctrl+F5。"
      );
    } else {
      console.log(`[PreciseAlignRegion] server build ${d.version} ok`);
    }
  } catch (e) {
    console.warn("[PreciseAlignRegion] server version check failed:", e);
    showStaleBanner(
      "Precise Align 服务端仍是旧版本（没有版本接口）。\n" +
        "请完全重启 ComfyUI 进程（不是刷新页面），再 Ctrl+F5。"
    );
  }
}

setTimeout(checkServerBuild, 1200);

app.registerExtension({
  name: "Comfy.PreciseAlign.RegionEditor",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_NAME) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const res = onNodeCreated?.apply(this, arguments);

      const shapes = this.widgets?.find((w) => w.name === "shapes_json");
      if (shapes) {
        shapes.computeSize = () => [0, -4];
        shapes.hidden = true;
      }

      const btn = this.addWidget("button", "✏️ 画框 / 画圈", null, () => openEditor(this));
      btn.serialize = false;
      if (this.size) this.setSize([Math.max(this.size[0], 240), this.size[1]]);
      return res;
    };

    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      onExecuted?.apply(this, arguments);
      if (Array.isArray(message?.images) && message.images.length) {
        this._paImages = message.images;
        console.log("[PreciseAlignRegion] preview ready:", message.images);
      }
    };

    const getExtraMenuOptions = nodeType.prototype.getExtraMenuOptions;
    nodeType.prototype.getExtraMenuOptions = function (_, options) {
      getExtraMenuOptions?.apply(this, arguments);
      options.unshift({
        content: "✏️ 画框 / 画圈",
        callback: () => openEditor(this),
      });
    };
  },
});
