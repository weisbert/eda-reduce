"""
img2b64.py — 把图片压到能塞进一个聊天框，转成 base64 文本。

跟 `plot_digitize` 是同一条线的两头：那个把波形截图**变回数**，这个把**图本身**
搬过去。有些东西没有底层数据可导 —— 别人报告里的曲线、datasheet 上的图、
一张原理图截图 —— 只能把图本身传过去，那就得先压到一条消息装得下。

这个工具跑在隔离区**外面** —— 你自己机器上把图压成文本，再粘进聊天框，
工具本身从不进气隙包。所以它直接依赖 Pillow，不受「尽量纯标准库」那条约束
（那条只管要跟着数据传进去的工具）。见 README「依赖：看这个工具跑在哪」。

## 两个会翻车的地方

1. **压过头了看不出来。** base64 长度达标了，但坐标轴数字已经糊成一团，
   传过去等于没传。所以预览是「压完再解码回来」的那张图，而且能 1:1 原像素看 ——
   拿压缩前的缩略图当预览等于自己骗自己。
2. **压不够也看不出来。** 眼睛估不出 base64 有多长。字节数、块数、够不够上限
   全部实时显示，且 `chunk` 从 `limit` 派生，不会出现「够小，1 条消息」
   和「块数 2」同时显示。

## 曲线图：降颜色数，不降分辨率

白底 + 几条线的图颜色本来就没几种，量化到 16~32 色视觉几乎无损，而 PNG 调色板
编码的压缩率极高。分辨率一旦降下去，坐标轴数字就再也救不回来 —— 所以 `--scale`
默认 100，是最后才动的旋钮。

实测，1595x1015 的三曲线对比图（145 KB / 1672 色）：

| 参数 | base64 字符 | |
|---|---|---|
| PNG colors=256 | 67528 | |
| PNG colors=32 | **44572** | 刻度数字仍清晰可读 |
| PNG colors=32 + 灰度 | 51472 | **反而更大**：几条曲线灰度后混在一起，更难量化 |
| JPEG q=80 | 150136 | **比原图还大**：抗锯齿细线 + 白底是 JPEG 最差的场景 |

所以「转灰度」和「JPEG」对曲线图都是陷阱，`advise()` 会主动拦。照片
（示波器拍照那种）反过来，连续色调量化没用，走 JPEG quality。

## 用法

  python img2b64.py                      # 开 GUI，Ctrl+V 粘贴
  python img2b64.py fig.png              # 命令行，自动压到 50 KB 以内
  python img2b64.py fig.png --colors 32 --out fig.txt
  python img2b64.py fig.png --limit 40000 --name FIG1
  python img2b64.py --decode 收到的.txt --out fig.png    # 接收侧还原
  python img2b64.py --selftest

GUI 和命令行共用 `encode()` / `render_text()` 这一层，参数归一化在
`Opts.normalize()` 而不是 `main()` —— `main()` 只是一条路径，不是唯一入口，
把归一化放那儿等于只归一化了一半。
"""
import argparse
import base64
import ctypes
import hashlib
import io
import math
import os
import sys

from PIL import Image

# 中文输出必须显式钉成 UTF-8：GBK 之类的本地化 locale 下 `> file` 和 `-o file`
# 会出不同的字节，那是数据损坏不是显示问题。stderr 一样要钉 —— 统计和建议
# 全走 stderr，只钉 stdout 的话那些中文照样是乱码。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            if (_s.encoding or "").lower().replace("-", "") != "utf8":
                _s.reconfigure(encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------

DEFAULT_LIMIT = 50_000      # 一条消息能带的 base64 字符数上限
CHUNK_HEADROOM = 100        # 每块给 ### 块头留的字符

# chunk 不设独立默认值 —— 它必须从 limit 派生。原来 limit=50000 / chunk=40000
# 各写各的，44572 字符就会同时显示「够小，1 条消息搞定」和「块数 2」，自相矛盾。

# 自动寻优的阶梯。先把颜色数降到底，再动分辨率 —— 曲线图上这个顺序是对的。
#
# 只取 2 的幂：PNG 的位深在 256/16/4 色处阶跃（8→4→2 bit），中间档位
# （192/96/48…）位深不变、只是调色板短一点，实测省不到 1%，白跑一轮编码。
COLOR_LADDER = [256, 128, 64, 32, 16, 8]
SCALE_LADDER = [100, 90, 80, 70, 60, 50, 40, 30]

# 4 色和 2 色是最后的手段，不进主阶梯。多条彩色曲线量化到 2 色会混成同一色，
# 图就废了 —— 宁可先掉分辨率（刻度还能读），也别先掉颜色区分度。
# 走查里 auto_fit(20000) 一路降到 colors=2 才暴露出这个取舍选反了。
COLOR_LAST_RESORT = [4, 2]

# auto_fit 默认留的余量。塞满到 49276/50000 没有意义——粘进聊天框时
# 前后还要带块头和一句说明，贴边等于没留退路。
SAFETY = 0.95


class Opts:
    """一次编码用到的全部参数。

    normalize() 放在这里而不是 main()，是因为 GUI 也要走同一条归一化。
    判据：Opts() 空构造直接喂进 encode() 不能炸。
    """

    def __init__(self, fmt="png", colors=32, scale=100, gray=False,
                 quality=70, limit=DEFAULT_LIMIT, chunk=None,
                 name="IMG"):
        self.fmt = fmt
        self.colors = colors
        self.scale = scale
        self.gray = gray
        self.quality = quality
        self.limit = limit
        self.chunk = chunk
        self.name = name
        self.normalize()

    def normalize(self):
        self.fmt = (self.fmt or "png").lower()
        if self.fmt in ("jpg", "jpeg"):
            self.fmt = "jpeg"
        if self.fmt not in ("png", "jpeg"):
            self.fmt = "png"
        self.colors = max(2, min(256, int(self.colors)))
        self.scale = max(5, min(100, int(self.scale)))
        self.quality = max(5, min(95, int(self.quality)))
        self.limit = max(1000, int(self.limit))
        # chunk 没显式给就跟着 limit 走，保证「够小」和「块数 1」永远一致
        self.chunk = (max(1000, self.limit - CHUNK_HEADROOM)
                      if self.chunk is None else max(1000, int(self.chunk)))
        self.gray = bool(self.gray)
        # 块头里要用，不能带空格
        self.name = (self.name or "IMG").strip().replace(" ", "-") or "IMG"
        return self

    def copy(self, **kw):
        o = Opts(self.fmt, self.colors, self.scale, self.gray,
                 self.quality, self.limit, self.chunk, self.name)
        for k, v in kw.items():
            setattr(o, k, v)
        return o.normalize()


class Result:
    """encode() 的产物。preview 是**解码回来**的图，不是压缩前的中间态。"""

    def __init__(self, data, preview, src_size, out_size, opts):
        self.data = data                    # 压缩后的字节
        self.preview = preview              # PIL Image，从 data 解码回来
        self.src_size = src_size            # (w, h) 原图
        self.out_size = out_size            # (w, h) 压后
        self.opts = opts
        self.b64 = base64.b64encode(data).decode("ascii")
        self.sha8 = hashlib.sha256(data).hexdigest()[:8]

    @property
    def nbytes(self):
        return len(self.data)

    @property
    def nb64(self):
        return len(self.b64)

    def nchunks(self, chunk=None):
        return max(1, math.ceil(self.nb64 / (chunk or self.opts.chunk)))

    def fits(self, limit=None):
        return self.nb64 <= (limit or self.opts.limit)


# ---------------------------------------------------------------------------
# 核心：整件事收口在这里，GUI 和 CLI 都只调这两个
# ---------------------------------------------------------------------------

def encode(img, opts):
    """图片 + 参数 → Result。

    预览用的图是从压缩结果**解码回来**的，所以你在 GUI 里看到的，
    就是对面会收到的那张，一个像素都不差。
    """
    opts.normalize()
    src_size = img.size

    im = img
    if im.mode in ("RGBA", "LA", "P"):
        # 透明底铺白，不然量化出来一片黑
        bg = Image.new("RGB", im.size, (255, 255, 255))
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")

    if opts.scale != 100:
        w = max(1, int(im.width * opts.scale / 100))
        h = max(1, int(im.height * opts.scale / 100))
        im = im.resize((w, h), Image.LANCZOS)

    if opts.gray:
        im = im.convert("L")

    buf = io.BytesIO()
    if opts.fmt == "png":
        # 调色板量化 —— 曲线图的主力旋钮
        q = im.convert("RGB").quantize(colors=opts.colors, method=Image.MEDIANCUT)
        q.save(buf, "PNG", optimize=True)
    else:
        im.save(buf, "JPEG", quality=opts.quality, optimize=True)

    data = buf.getvalue()
    preview = Image.open(io.BytesIO(data))
    preview.load()
    return Result(data, preview, src_size, im.size, opts)


def auto_fit(img, opts, limit=None):
    """沿阶梯找「能塞进 limit 的最好画质」。

    先降颜色数（曲线图上几乎无损），降到底还不够才动分辨率。
    返回 (Result, ok)；ok=False 表示阶梯走到头仍然超限，得分块。
    """
    limit = limit or opts.limit
    target = int(limit * SAFETY)
    best = None
    for scale in SCALE_LADDER:
        ladder = COLOR_LADDER if opts.fmt == "png" else [opts.colors]
        for colors in ladder:
            r = encode(img, opts.copy(scale=scale, colors=colors))
            best = best or r
            if r.nb64 <= target:
                return r, True
        if opts.fmt == "jpeg":
            # JPEG 这条路降的是 quality，不是颜色数
            for qual in (85, 75, 65, 55, 45, 35, 25, 15):
                r = encode(img, opts.copy(scale=scale, quality=qual))
                if r.nb64 <= target:
                    return r, True
                best = r
    # 分辨率也降到底了才动 4/2 色
    if opts.fmt == "png":
        for colors in COLOR_LAST_RESORT:
            for scale in SCALE_LADDER:
                r = encode(img, opts.copy(scale=scale, colors=colors))
                if r.nb64 <= target:
                    return r, True
                best = r
    return best, False


def guess_kind(img):
    """看颜色数猜这是曲线图还是照片 —— 决定该走 PNG 还是 JPEG。

    实测（1595x1015 的 matplotlib 三曲线图，145 KB / 1672 色）：

        PNG colors=32   33.4 KB      <- 该走这条
        PNG colors=32 灰度 38.6 KB   <- 反而更大：三条曲线灰度后混在一起，更难量化
        JPEG q=80      112.6 KB      <- 灾难：抗锯齿细线 + 白底正是 JPEG 最差的场景

    所以对曲线图，「转灰度」和「JPEG」两个选项都是陷阱，工具得主动拦一下。
    """
    try:
        n = len(img.convert("RGB").getcolors(maxcolors=60000) or [])
    except Exception:
        n = 0
    return "plot" if 0 < n < 20000 else "photo"


def advise(img, opts, result=None):
    """给一句针对当前图 + 当前参数的建议，没问题就返回 None。

    result 给了的话还会检查分辨率掉了多少 —— 硬压出一张读不出刻度的图
    是这个工具最容易犯的错，压得下去不等于传得有用。
    """
    kind = guess_kind(img)
    if result is not None and kind == "plot":
        keep = 100.0 * result.out_size[0] / max(1, result.src_size[0])
        if keep < 65:
            return ("分辨率已经掉到 %.0f%%（%dx%d），坐标轴数字多半读不出来了。"
                    "与其硬压，不如把上限调回去用「复制（带分块头）」分 2~3 块传。"
                    % (keep, result.out_size[0], result.out_size[1]))
    if kind == "plot":
        if opts.fmt == "jpeg":
            return "这张看着是曲线图（颜色数少）。JPEG 在抗锯齿细线上会胀到 PNG 的 3 倍以上，改用 PNG。"
        if opts.gray:
            return "这张看着是彩色曲线图。转灰度会让几条曲线混在一起，量化更难，实测反而更大——建议取消灰度。"
    if kind == "plot" and opts.fmt == "png" and opts.colors <= 4:
        return "只剩 %d 色，多条彩色曲线会混成同一色、图例也认不出来。宁可拉低「缩放」保住颜色。" % opts.colors
    if kind == "photo" and opts.fmt == "png":
        return "这张看着是照片/连续色调。调色板量化对它没用，改用 JPEG 调 quality。"
    return None


def render_text(result, chunked=None):
    """Result → 最终要复制走的文本。

    块头每块都带全套元信息（序号/格式/字节/指纹），这样丢一块也看得出来。
    """
    o = result.opts
    n = result.nchunks()
    if chunked is None:
        chunked = n > 1
    if not chunked:
        # 显式说了不要块头就是不要，跟块数无关。
        # 原来写的是 `not chunked and n == 1`，于是 n>1 时静默退回分块，
        # 用户点「复制 base64」拿到的却是带 ### 头的文本（走查抓到）。
        return result.b64

    meta = "%s %dB sha256:%s" % (o.fmt, result.nbytes, result.sha8)
    out = []
    for i in range(n):
        payload = result.b64[i * o.chunk:(i + 1) * o.chunk]
        out.append("### %s %d/%d %s\n%s" % (o.name, i + 1, n, meta, payload))
    return "\n\n".join(out)


def decode_text(text):
    """render_text 的逆操作 —— 接收侧用，也给 selftest 当校验。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    payload = "".join(ln for ln in lines if not ln.startswith("###"))
    return base64.b64decode(payload)


def human(n):
    return "%.1f KB" % (n / 1024.0) if n >= 1024 else "%d B" % n


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def run_gui(initial=None, _probe=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from PIL import ImageGrab, ImageTk

    # 高 DPI 屏上不声明 DPI-aware 的话，整个窗口会被系统整体拉伸，
    # 预览里的图跟着一起糊 —— 而这个工具的全部意义就是判断「刻度数字糊没糊」。
    # 底图先被系统糊一道，就什么都判断不了了。必须在建 Tk() 之前调。
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # Win 8.1+
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()    # Win 7 兜底
            except Exception:
                pass

    root = tk.Tk()
    root.title("img2b64 —— 图片转 base64")
    root.geometry("1180x1000")
    root.minsize(900, 780)      # 再小就装不下底部的统计和按钮了

    state = {"src": None, "result": None, "photo": None}

    # ---- 顶部：来源 ----
    top = ttk.Frame(root, padding=8)
    top.pack(fill="x")
    ttk.Button(top, text="Ctrl+V 粘贴", command=lambda: do_paste()).pack(side="left")
    ttk.Button(top, text="打开文件…", command=lambda: do_open()).pack(side="left", padx=(6, 0))
    src_lbl = ttk.Label(top, text="还没有图片 —— Ctrl+V 粘贴，或点「打开文件」")
    src_lbl.pack(side="left", padx=12)

    # ---- 中部：预览 ----
    # 注意 pack 顺序：底部的控制区和统计区必须先占住自己的自然高度，
    # 预览区再拿剩下的。反过来的话预览的 expand=True 会先吃光空间，
    # 把「自动压到上限内」「复制」按钮和统计栏整个挤到窗口外面——
    # 控件都在、走查也全绿，但用户根本看不见（截图才看出来）。
    bot = ttk.Frame(root, padding=8)
    bot.pack(side="bottom", fill="x")
    box = ttk.LabelFrame(root, text="参数", padding=8)
    box.pack(side="bottom", fill="x", padx=8, pady=4)

    mid = ttk.LabelFrame(root, text="压缩后预览（这就是对面收到的那张）", padding=4)
    mid.pack(side="top", fill="both", expand=True, padx=8, pady=4)

    cwrap = ttk.Frame(mid)
    cwrap.pack(fill="both", expand=True)
    canvas = tk.Canvas(cwrap, bg="#666", highlightthickness=0)
    vbar = ttk.Scrollbar(cwrap, orient="vertical", command=canvas.yview)
    hbar = ttk.Scrollbar(cwrap, orient="horizontal", command=canvas.xview)
    canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    vbar.grid(row=0, column=1, sticky="ns")
    hbar.grid(row=1, column=0, sticky="ew")
    cwrap.rowconfigure(0, weight=1)
    cwrap.columnconfigure(0, weight=1)

    one2one = tk.BooleanVar(value=False)
    ttk.Checkbutton(mid, text="1:1 原像素查看（判断刻度数字糊没糊，必须勾这个看）",
                    variable=one2one, command=lambda: redraw()).pack(anchor="w", pady=(4, 0))

    # ---- 参数 ----
    fmt_v = tk.StringVar(value="png")
    colors_v = tk.IntVar(value=32)
    scale_v = tk.IntVar(value=100)
    gray_v = tk.BooleanVar(value=False)
    qual_v = tk.IntVar(value=70)
    limit_v = tk.IntVar(value=DEFAULT_LIMIT)
    name_v = tk.StringVar(value="IMG")

    r0 = ttk.Frame(box); r0.pack(fill="x", pady=2)
    ttk.Label(r0, text="格式", width=6).pack(side="left")
    for txt, val in (("PNG（曲线图选这个）", "png"), ("JPEG（照片选这个）", "jpeg")):
        ttk.Radiobutton(r0, text=txt, value=val, variable=fmt_v,
                        command=lambda: recompute()).pack(side="left", padx=(0, 10))
    ttk.Checkbutton(r0, text="转灰度", variable=gray_v,
                    command=lambda: recompute()).pack(side="left", padx=10)

    def slider(parent, label, var, lo, hi, suffix=""):
        row = ttk.Frame(parent); row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, width=9).pack(side="left")
        val_lbl = ttk.Label(row, text="", width=14)
        s = ttk.Scale(row, from_=lo, to=hi, orient="horizontal")
        s.set(var.get())          # 先设值，此时还没绑 command
        # 再绑回调。反过来的话 s.set() 会立刻触发 command，而 recompute
        # 在词法上定义在后面 —— 启动时三条 NameError traceback（走查抓到）。
        s.configure(command=lambda e: (var.set(int(float(e))),
                                       val_lbl.config(text=str(var.get()) + suffix),
                                       recompute()))
        s.pack(side="left", fill="x", expand=True, padx=6)
        val_lbl.config(text=str(var.get()) + suffix)
        val_lbl.pack(side="left")
        return s

    slider(box, "颜色数", colors_v, 2, 256, " 色")
    slider(box, "缩放", scale_v, 10, 100, " %")
    slider(box, "JPEG质量", qual_v, 5, 95, "")

    r3 = ttk.Frame(box); r3.pack(fill="x", pady=(6, 2))
    ttk.Label(r3, text="上限", width=6).pack(side="left")
    ttk.Entry(r3, textvariable=limit_v, width=8).pack(side="left")
    ttk.Label(r3, text="字符").pack(side="left", padx=(2, 12))
    ttk.Label(r3, text="名字").pack(side="left")
    ttk.Entry(r3, textvariable=name_v, width=14).pack(side="left", padx=(4, 12))
    ttk.Button(r3, text="★ 自动压到上限内",
               command=lambda: do_auto()).pack(side="left")

    # ---- 统计 + 动作 ----
    stat = tk.Label(bot, text="—", anchor="w", justify="left",
                    font=("Consolas", 10))
    stat.pack(fill="x")
    verdict = tk.Label(bot, text="", anchor="w", font=("", 11, "bold"))
    verdict.pack(fill="x", pady=(2, 0))
    tip = tk.Label(bot, text="", anchor="w", fg="#9a6700", wraplength=1050,
                   justify="left")
    tip.pack(fill="x", pady=(0, 6))

    act = ttk.Frame(bot); act.pack(fill="x")
    ttk.Button(act, text="复制 base64", command=lambda: do_copy(False)).pack(side="left")
    ttk.Button(act, text="复制（带分块头）", command=lambda: do_copy(True)).pack(side="left", padx=6)
    ttk.Button(act, text="存文件…", command=lambda: do_save()).pack(side="left")
    copied = ttk.Label(act, text="")
    copied.pack(side="left", padx=12)

    # ---- 行为 ----
    def cur_opts():
        return Opts(fmt=fmt_v.get(), colors=colors_v.get(), scale=scale_v.get(),
                    gray=gray_v.get(), quality=qual_v.get(),
                    limit=limit_v.get(), name=name_v.get())

    def set_src(img, desc):
        state["src"] = img
        src_lbl.config(text="%s   原图 %dx%d" % (desc, img.width, img.height))
        recompute()

    def do_paste():
        try:
            got = ImageGrab.grabclipboard()
        except Exception as e:
            messagebox.showerror("取剪贴板失败", str(e)); return
        if isinstance(got, list):
            paths = [p for p in got if os.path.isfile(p)]
            if not paths:
                messagebox.showwarning("剪贴板", "剪贴板里是文件列表，但没有能读的文件"); return
            try:
                set_src(Image.open(paths[0]), os.path.basename(paths[0]))
            except Exception as e:
                messagebox.showerror("打不开", str(e))
        elif got is None:
            messagebox.showwarning("剪贴板", "剪贴板里没有图片。\n先截图（Win+Shift+S）再回来 Ctrl+V。")
        else:
            set_src(got, "剪贴板")

    def do_open():
        p = filedialog.askopenfilename(
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff"), ("全部", "*.*")])
        if p:
            try:
                set_src(Image.open(p), os.path.basename(p))
            except Exception as e:
                messagebox.showerror("打不开", str(e))

    def recompute():
        if state["src"] is None:
            return
        try:
            state["result"] = encode(state["src"], cur_opts())
        except Exception as e:
            stat.config(text="编码失败: %s" % e); return
        redraw()
        show_stat()

    def do_auto():
        if state["src"] is None:
            messagebox.showinfo("先给张图", "还没有图片"); return
        r, ok = auto_fit(state["src"], cur_opts(), limit_v.get())
        state["result"] = r
        # 把找到的参数写回控件，让你看得见它选了什么
        colors_v.set(r.opts.colors); scale_v.set(r.opts.scale); qual_v.set(r.opts.quality)
        redraw(); show_stat()
        if not ok:
            messagebox.showinfo("压不进去",
                                "阶梯走到头仍然超限，得分块传。\n用「复制（带分块头）」，一块一条消息。")

    def redraw():
        r = state["result"]
        if r is None:
            return
        im = r.preview.convert("RGB")
        if not one2one.get():
            cw = max(canvas.winfo_width(), 200)
            ch = max(canvas.winfo_height(), 200)
            im = im.copy()
            im.thumbnail((cw, ch), Image.LANCZOS)
        state["photo"] = ImageTk.PhotoImage(im)
        canvas.delete("all")
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        ox = max(0, (cw - im.width) // 2)          # 图比画布小就居中，别缩在左上角
        oy = max(0, (ch - im.height) // 2)
        canvas.create_image(ox, oy, anchor="nw", image=state["photo"])
        canvas.config(scrollregion=(0, 0, max(cw, im.width), max(ch, im.height)))

    def show_stat():
        r = state["result"]
        if r is None:
            return
        n = r.nchunks()
        stat.config(text=(
            "原图 %dx%d   →   压后 %dx%d  %s\n"
            "base64 %s（%d 字符）   上限 %d   块数 %d   sha256:%s"
            % (r.src_size[0], r.src_size[1], r.out_size[0], r.out_size[1],
               human(r.nbytes), human(r.nb64), r.nb64, limit_v.get(), n, r.sha8)))
        if r.nb64 <= limit_v.get():
            verdict.config(text="● 够小，1 条消息搞定", fg="#1a7f37")
        elif n <= 4:
            verdict.config(text="● 超限，要分 %d 块传（用「复制（带分块头）」）" % n, fg="#9a6700")
        else:
            verdict.config(text="● 太大，%d 块。先点「自动压到上限内」" % n, fg="#cf222e")
        msg = advise(state["src"], r.opts, r) if state["src"] is not None else None
        tip.config(text=("⚠ " + msg) if msg else "")

    def do_copy(chunked):
        r = state["result"]
        if r is None:
            messagebox.showinfo("先给张图", "还没有图片"); return
        txt = render_text(r, chunked=chunked)
        root.clipboard_clear()
        root.clipboard_append(txt)
        root.update()
        copied.config(text="已复制 %d 字符%s" % (len(txt), "（%d 块）" % r.nchunks() if chunked else ""))
        root.after(4000, lambda: copied.config(text=""))

    def do_save():
        r = state["result"]
        if r is None:
            return
        p = filedialog.asksaveasfilename(defaultextension=".txt",
                                         initialfile="%s.txt" % name_v.get())
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(render_text(r))
            copied.config(text="已存 " + os.path.basename(p))

    root.bind("<Control-v>", lambda e: do_paste())
    root.bind("<Control-V>", lambda e: do_paste())
    canvas.bind("<Configure>", lambda e: redraw())

    # 走查口子：GUI 改完必须真开窗走一遍整条路 —— 截图只守「画得像不像」，
    # 守不住「这条路走不走得通」。而这些动作全在闭包里，外面够不着，
    # 留一个回调把它们递出去。tests/test_img2b64.py 用的就是它。
    if _probe is not None:
        ns = dict(root=root, canvas=canvas, stat=stat, verdict=verdict, tip=tip,
                  src_lbl=src_lbl, copied=copied, state=state, one2one=one2one,
                  fmt_v=fmt_v, colors_v=colors_v, scale_v=scale_v, gray_v=gray_v,
                  qual_v=qual_v, limit_v=limit_v, name_v=name_v,
                  do_paste=do_paste, do_open=do_open, do_auto=do_auto,
                  do_copy=do_copy, recompute=recompute, redraw=redraw,
                  cur_opts=cur_opts)
        root.after(400, lambda: _probe(ns))

    if initial:
        try:
            set_src(Image.open(initial), os.path.basename(initial))
        except Exception as e:
            messagebox.showerror("打不开", str(e))

    root.mainloop()


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

def _make_curve():
    """合成一张「曲线图」：白底 + 坐标轴 + 两条彩色曲线 + 刻度数字。

    **必须带抗锯齿。** 第一版直接在 900x600 上画线，PIL 不做抗锯齿，
    整张图只有 5 种颜色，量化自然一点收益都没有（256 色 → 16 色只降 2.3%），
    测试全绿但什么都没测到。真实截图（matplotlib / ViVA / 示波器导出）
    全都是抗锯齿的，颜色数在几千。所以这里 3 倍超采样再缩回来。
    """
    from PIL import ImageDraw
    S = 3
    im = Image.new("RGB", (900 * S, 600 * S), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([80 * S, 40 * S, 860 * S, 540 * S], outline=(0, 0, 0), width=S)
    for i in range(11):
        x = (80 + i * 78) * S
        d.line([x, 540 * S, x, 546 * S], fill=(0, 0, 0), width=S)
        d.line([x, 40 * S, x, 540 * S], fill=(200, 200, 200), width=S)   # 网格
    for i in range(6):
        y = (540 - i * 100) * S
        d.line([74 * S, y, 80 * S, y], fill=(0, 0, 0), width=S)
        d.line([80 * S, y, 860 * S, y], fill=(200, 200, 200), width=S)
    p1 = [((80 + i) * S, (540 - int(400 / (1 + math.exp(-(i - 390) / 90.0)))) * S)
          for i in range(780)]
    p2 = [((80 + i) * S, (520 - int(300 * (i / 780.0) ** 1.4)) * S) for i in range(780)]
    d.line(p1, fill=(200, 30, 30), width=2 * S)
    d.line(p2, fill=(30, 80, 200), width=2 * S)
    im = im.resize((900, 600), Image.LANCZOS)          # 超采样缩回 → 抗锯齿
    d2 = ImageDraw.Draw(im)
    for i in range(11):                                 # 文字最后画，保持锐利
        d2.text(((80 + i * 78) - 8, 550), "%.1f" % (i * 0.1), fill=(0, 0, 0))
    for i in range(6):
        d2.text((40, (540 - i * 100) - 6), "%d" % (i * 500), fill=(0, 0, 0))
    return im


def selftest():
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        ok &= bool(cond)
        print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                             ("  " + extra) if extra else ""))

    img = _make_curve()
    print("selftest —— 合成曲线图 %dx%d" % img.size)

    print("\n[1] 空构造的 Opts 直接喂进 encode 不能炸")
    r = encode(img, Opts())
    check("encode(img, Opts()) 跑通", r.nbytes > 0, "%s" % human(r.nbytes))

    print("\n[2] base64 来回一致")
    check("decode(render(r)) == r.data", decode_text(render_text(r)) == r.data)

    print("\n[3] 分块能拼回来（强制 1 KB 一块）")
    rc = encode(img, Opts(chunk=1000))
    txt = render_text(rc, chunked=True)
    nblk = txt.count("###")
    check("分块拼回一致", decode_text(txt) == rc.data, "%d 块" % nblk)
    check("块数 > 1", nblk > 1)

    print("\n[4] 量化确实降到了指定颜色数")
    for c in (64, 16, 4):
        rr = encode(img, Opts(colors=c))
        got = len(rr.preview.convert("RGB").getcolors(maxcolors=100000) or [])
        check("colors=%d → 实际 %d 色" % (c, got), got <= c, human(rr.nbytes))

    print("\n[5] 降颜色数确实变小（曲线图的主力旋钮）")
    big = encode(img, Opts(colors=256)).nb64
    small = encode(img, Opts(colors=16)).nb64
    check("256色 %d → 16色 %d 字符" % (big, small), small < big,
          "降 %.1f%%" % (100.0 * (big - small) / big))

    print("\n[5b] 合成图必须够真实（带抗锯齿），否则上面那条测了个寂寞")
    ncol = len(img.convert("RGB").getcolors(maxcolors=10 ** 7) or [])
    check("颜色数 > 500（真实截图量级）", ncol > 500, "%d 色" % ncol)
    check("256→16 色收益 > 10%", 100.0 * (big - small) / big > 10,
          "实测降 %.1f%%" % (100.0 * (big - small) / big))

    print("\n[5c] 曲线图上，灰度和 JPEG 都是陷阱（advise 要拦住）")
    check("guess_kind 认出是曲线图", guess_kind(img) == "plot")
    j = encode(img, Opts(fmt="jpeg", quality=80)).nb64
    pn = encode(img, Opts(colors=32)).nb64
    check("JPEG q80 确实比 PNG-32 大", j > pn, "%d vs %d 字符" % (j, pn))
    check("advise 拦住 JPEG", advise(img, Opts(fmt="jpeg")) is not None)
    check("advise 拦住灰度", advise(img, Opts(gray=True)) is not None)
    check("advise 对 PNG 彩色不啰嗦", advise(img, Opts()) is None)
    rsmall = encode(img, Opts(scale=40))
    check("advise 拦住「压成读不出刻度的图」",
          advise(img, rsmall.opts, rsmall) is not None)
    rfull = encode(img, Opts())
    check("分辨率没掉时不啰嗦", advise(img, rfull.opts, rfull) is None)

    print("\n[6] 预览是解码回来的图，不是压缩前的中间态")
    rg = encode(img, Opts(scale=50))
    check("preview 尺寸 == 压后尺寸", rg.preview.size == rg.out_size,
          "%s vs %s" % (rg.preview.size, rg.out_size))

    print("\n[7] auto_fit 落在上限内")
    for lim in (50_000, 20_000, 8_000):
        rr, good = auto_fit(img, Opts(), lim)
        check("limit=%d → %d 字符（留了余量）" % (lim, rr.nb64),
              good and rr.nb64 <= lim * SAFETY,
              "colors=%d scale=%d" % (rr.opts.colors, rr.opts.scale))

    print("\n[8] 参数越界被夹住，不是报错")
    o = Opts(colors=9999, scale=-5, quality=200, fmt="tiff")
    check("colors 夹到 256", o.colors == 256)
    check("scale 夹到 5", o.scale == 5)
    check("fmt 兜底成 png", o.fmt == "png")

    print("\n%s" % ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="把图片压到能塞进聊天框的 base64 文本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不给图片路径就开 GUI。曲线图用 PNG + 降颜色数，照片用 JPEG。")
    p.add_argument("image", nargs="?", help="图片路径；省略则开 GUI")
    p.add_argument("--gui", action="store_true", help="即使给了路径也开 GUI")
    p.add_argument("--fmt", default="png", choices=["png", "jpeg", "jpg"])
    p.add_argument("--colors", type=int, default=32, help="PNG 调色板颜色数 (2-256)")
    p.add_argument("--scale", type=int, default=100, help="缩放百分比，默认 100 不缩")
    p.add_argument("--gray", action="store_true", help="转灰度")
    p.add_argument("--quality", type=int, default=70, help="JPEG 质量")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="单条消息 base64 字符上限")
    p.add_argument("--chunk", type=int, default=None,
                   help="分块时每块字符数（默认跟随 --limit）")
    p.add_argument("--name", default="IMG", help="块头里的名字")
    p.add_argument("--no-auto", action="store_true", help="不自动寻优，严格用给定参数")
    p.add_argument("--out", help="写到文件；省略则打到 stdout")
    p.add_argument("--decode", metavar="TXT",
                   help="反过来：把收到的 base64 文本还原成图片（分块的直接喂整段，会自动拼）")
    p.add_argument("--selftest", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.selftest:
        return selftest()

    if args.decode:
        # 接收侧：把收到的文本还原成图片
        with open(args.decode, encoding="utf-8") as f:
            txt = f.read()
        heads = [ln for ln in txt.splitlines() if ln.startswith("###")]
        try:
            data = decode_text(txt)
        except Exception as e:
            print("base64 解不开：%s" % e, file=sys.stderr)
            return 2
        got = hashlib.sha256(data).hexdigest()[:8]

        # 指纹和块数先验，再开图。反过来的话粘漏一块只会抛
        # 「OSError: broken data stream」，看不出是哪儿坏了。
        want = None
        for ln in heads:
            if "sha256:" in ln:
                want = ln.split("sha256:")[1].split()[0]
                break
        if heads:
            n_expect = int(heads[0].split("/")[1].split()[0])
            got_idx = sorted(int(h.split()[2].split("/")[0]) for h in heads)
            if len(heads) != n_expect or got_idx != list(range(1, n_expect + 1)):
                print("块不全：块头说共 %d 块，实际拿到 %s"
                      % (n_expect, got_idx), file=sys.stderr)
                return 2
        if want and want != got:
            print("指纹对不上：块头说 %s，拼出来是 %s。"
                  "多半是漏了一块、顺序错了，或者粘贴时被截断。"
                  % (want, got), file=sys.stderr)
            return 2

        try:
            im = Image.open(io.BytesIO(data)); im.load()
        except Exception as e:
            print("字节拿到了但不是一张能打开的图：%s" % e, file=sys.stderr)
            return 2
        out = args.out or (os.path.splitext(args.decode)[0] + "." + im.format.lower())
        with open(out, "wb") as f:
            f.write(data)
        print("还原 %s  %dx%d %s  %s  sha256:%s%s"
              % (out, im.width, im.height, im.format, human(len(data)), got,
                 "  指纹对得上" if want else ""), file=sys.stderr)
        return 0

    if not args.image or args.gui:
        run_gui(args.image)
        return 0

    img = Image.open(args.image)
    opts = Opts(fmt=args.fmt, colors=args.colors, scale=args.scale, gray=args.gray,
                quality=args.quality, limit=args.limit, chunk=args.chunk, name=args.name)

    if args.no_auto:
        r, ok = encode(img, opts), None
    else:
        r, ok = auto_fit(img, opts, opts.limit)

    txt = render_text(r)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(txt)

    # 统计走 stderr，这样 --out 省略时 stdout 是干净的 base64，可以直接管道
    w0, h0 = r.src_size
    w1, h1 = r.out_size
    print("原图 %dx%d → 压后 %dx%d %s | base64 %s (%d 字符) | 块数 %d | sha256:%s"
          % (w0, h0, w1, h1, human(r.nbytes), human(r.nb64), r.nb64,
             r.nchunks(), r.sha8), file=sys.stderr)
    print("参数 fmt=%s colors=%d scale=%d gray=%s quality=%d"
          % (r.opts.fmt, r.opts.colors, r.opts.scale, r.opts.gray, r.opts.quality),
          file=sys.stderr)
    msg = advise(img, r.opts, r)
    if msg:
        print("!! " + msg, file=sys.stderr)
    if ok is False:
        print("!! 阶梯走到头仍超限，上面是分块文本，一块一条消息", file=sys.stderr)

    if args.out:
        print("已写 %s" % args.out, file=sys.stderr)
    else:
        sys.stdout.write(txt + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
