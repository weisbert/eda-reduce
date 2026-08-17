# eda-reduce

把 EDA 工具的产出压成一段能直接粘贴给大模型的紧凑文本。

给那些**只能传文本、不能传文件**的场景用——隔离网络、无外网的仿真机器、
只有一个聊天框的通道。目标是把模型分析电路真正需要的信息搬出来，
而不是把文件压小。

```
原理图  my.drawio  ──drawio_reduce──▶  my.rd  (1/5 ~ 1/8)   ──▶  模型重建出网表 .ckt
                   ◀──drawio_expand──                           改完再画回去
波形    my.csv     ──wave_reduce────▶  my.wv  (≤20 KB)      ──▶  模型定位 debug 问题
截图    plot.png   ──plot_digitize──▶  my.wv                ──▶  （导不出数据时的兜底）
```

## 工具

| | 状态 | 干什么 |
|---|---|---|
| `tools/drawio_reduce.py` | **可用** | `.drawio` → `.rd`。纯标准库，Python 3.8+，无依赖 |
| `tools/drawio_expand.py` | **可用** | `.rd` → `.drawio`，reduce 的逆。连线走法、标签位置逐像素还原 |
| `tools/wave_reduce.py` | **可用** | Cadence 波形 CSV → `.wv`（≤20 KB），带 Tkinter GUI 预览。见 [`docs/wave-spec.md`](docs/wave-spec.md) |
| `tools/plot_digitize.py` | **可用** | 波形截图 → CSV/`.wv`。导不出数据时的兜底 |

## 依赖：没有。不用 `pip install`

```bash
git clone https://github.com/weisbert/eda-reduce.git && cd eda-reduce
python tools/wave_reduce.py examples/demo_tran.csv     # 就这样，不用装任何东西
```

Python 3.6+，**全部纯标准库**。仓库里那个 `deploy/requirements.txt` 是空的
（只有注释）——部署管道按双包模型建好了，只是现在没东西可装，
哪天加了 numpy 往里写一行就行，不用回头重做部署链。

两个**可选**的东西，没有也照常跑：

| | 缺了会怎样 | 怎么补 |
|---|---|---|
| `Pillow` | `plot_digitize` 退回自带的 PNG 解码器（纯标准库 zlib，约 60 行）。结果一样，大图慢一些 | `pip install Pillow`，纯 python 之外的机器上也可以不装 |
| `tkinter` | `--gui` 用不了，命令行全部照常 | **装不了 wheel**（CPython 自带的 C 扩展 + Tcl/Tk 运行时），只能上系统包 `python3-tk` / `tkinter` |

这条「核心不依赖任何东西」是**硬约束**不是巧合：`wave_core.py` + `wave_emit.py`
+ `wave_cli.py` 三个文件 scp 到任何机器就能跑，是部署链坏掉时的逃生舱。
`tests/test_format.py` 里有 AST 扫描在守这条线。

```
docs/rd-spec.md       .rd 格式规范 + 设计约定（分工线、黑名单、坐标解算、反方向）
docs/ckt-format.md    .ckt 概念网表格式 —— 模型回给你的东西长什么样
docs/wave-spec.md     .wv 格式 + wave_reduce 设计约定
docs/wave-decisions.md  spec 没定、实现时拍板的细节（附理由）
examples/demo.drawio  样例：教科书 5 管 OTA + 一小段架构图
examples/demo.rd      压缩结果（2.3 KB，同时充当回归测试基准）
examples/demo.ckt     从 .rd 重建出的网表 + 全部推断标注
examples/gen_demo_wave.py  合成波形生成器 —— 真值已知，所以测量对不对是可判定的
examples/demo_*.csv   合成波形样例（瞬态 / AC / 谱 / 布局B / 脏数据）
examples/demo_*.wv    压缩结果，同时充当回归基准
deploy/               隔离区双包部署管道（见 deploy/README.md）
                      日常更新就一条命令：tar 传进安装目录，bash update
tests/                unittest 全套（见 tests/README.md）
```

## 用法

```bash
python tools/drawio_reduce.py my.drawio -o my.rd
python tools/drawio_reduce.py my.drawio --bbox 400,300,800,700   # 只导出一个区域
python tools/drawio_expand.py my.rd -o back.drawio  # 反过来：改完 .rd 再画回去

python tools/wave_reduce.py my.csv -o my.wv        # 默认压到 20 KB 以内
python tools/wave_reduce.py my.csv --gui           # 看这次压缩丢了什么、能不能粘出去
                                                   # 顶上「出口台」两条判据：装得下 / 够得准；
                                                   # 卡住时给的是**能点的按钮**（开解调 /
                                                   # 只搬这一段 / 预算改到 N KB），不用回命令行
                                                   # 左边暖橙那条 = 改要粘走的文本；
                                                   # 画布上方灰条 = 只改这张图。位置即语义
                                                   # 滚轮/框选缩放，右键拖平移，双击或 0 复位
                                                   # 鼠标扫过给「原值 / 存进 .wv 的值 / 差几倍容差」
python tools/wave_reduce.py my.csv --demod         # 振荡波形：传调制不传载波
                                                   # 包络 + 瞬时频率 + 几个代表性周期，
                                                   # 全在同一份 txt 里。实测 218 KB -> 22 KB
python tools/wave_reduce.py my.csv --xrange 1.6u:1.62u   # 只导这一段
                                                   # 振荡波形必须这么看：预算摊在
                                                   # 2500 个周期上是 1.6 点/周期，
                                                   # 摊在 100 个周期上是 10 点/周期
python tools/wave_reduce.py my.csv --budget 51200  # 通道宽就多带点
python tools/wave_reduce.py my.csv --budget 0      # 不限，先看完整的长什么样
export EDA_REDUCE_BUDGET=32k                       # 定成你那条通道的常态
python tools/plot_digitize.py shot.png --xaxis 0,300n --yaxis 0.7,0.87 \
    --trace '#e01b24=vdd_pll' -o dig.csv           # 截图兜底
```

```
本地                                              对话里
──────────────────────────────────────────────────────────
画 my.drawio
   ↓  drawio_reduce.py
my.rd  (原始体积的 1/5 ~ 1/8)
   ↓  粘贴全文  ─────────────────────────────────→  重建 .ckt
                                                    ↓
   对着图核 .ckt 末尾的「推断标注」段  ←────────────  标注出所有猜测
   ↓  drawio_expand.py                              （或者直接改 .rd 再贴回来）
改过的 my.drawio
```

## `.wv` 长什么样

三段。`[METRICS]` / `[EVENTS]` 是**全精度的事实**（脚本在全分辨率数据上量的，
丢了不可逆），`[SHAPE]` 是**降精度的形状**。头部第二行起是**自检**——
有 `# WARN:` 先看它（输入本身撑不住时才有），然后是 `# recon:`——
输出声明自己的不确定度，等价于 `.ckt` 末尾那段推断标注。

```
# WV1  tran  demo_tran  4 sig  time 0 .. 300 ns  2545 -> 596 pts (4.3x)
# recon: max|err| 116 uV (13.35% of range) @ time=74.37 ns  rms 22.9 uV  [worst V(vref)]
# c1   V(vdd_pll)  [mV] offset 800 mV  range -85.3..+59.6  (量化 100 uV)  err 1.46%
...
[METRICS]
c1    min 714.693 mV @ 40.9918 ns   max 859.579 mV @ 59.4121 ns   pp 144.886 mV
c1    settle(+-1%) 159.9 ns (band 8 mV)   settle(+-0.1%) n/a (带宽 800 uV < 6x 噪声底)
c1    glitch1 -3.564 mV @ 119.972 ns (width 210 ps, 23.0x 噪声底, 窗口 640 ps)
c2    period 6.40004 ns   jitter_rms 3.84155 ps   jitter_pp 15.6873 ps (N=46 cycles)
[EVENTS]
119.972 ns    c1   GLITCH      -3.564 mV, 23.0x 噪声底, width 210 ps
[SHAPE] time c1 c2 c3 c4
148.275 2.6 1200 1.5335 406
```

实测：134 KB 的瞬态 CSV → 20.4 KB；95 KB 的谱 → 15 KB；32 KB 的 AC → 2 KB。

### 20 KB 是**这条通道**的宽度，不是普适真理

换个通道（能贴附件、字数上限不同、换个模型）这个数就该跟着变，所以三个地方都能改：

```bash
python tools/wave_reduce.py my.csv --budget 51200   # 这一次
export EDA_REDUCE_BUDGET=32k                        # 你的常态（支持 32k / 32kb / 32768）
```

GUI 里有输入框 + **「自动压到预算」**按钮，二分点数、结果和命令行同一个数
（`tests/test_gui.py` 在守这个一致性——两边给不出同一个数的话，
GUI 里调好的参数拿到命令行就不作数了）。压好之后 **「复制全文到剪贴板」**
一步粘进聊天框，不用先存文件。

压不进去时**不会偷偷截断全精度测量**：`[METRICS]`/`[EVENTS]` 和强制保留点
（spur 峰及其包络、极值、事件）是丢了不可逆的东西，宁可超也要在输出里
声明超了多少、为什么下不来、建议怎么办。

## 波形那条分工线不一样

`drawio_reduce` 的铁律是「脚本只做确定性变换，语义判断留给模型」。波形反过来：

> **脚本看得见全分辨率数据，模型永远看不见。**
> 峰值的精确位置、周期抖动、settling time、spur 的 dBc——需要全分辨率才算得出，
> **脚本不算就永远丢了**，模型拿到 400 个点之后再聪明也算不回来。

所以分工线往下挪一格：**脚本负责「测量」，模型负责「诊断」。脚本绝不写形容词。**
输出里不出现「轻微过冲」「看起来稳定」这类词，只有数。
（`tests/test_format.py` 里有个词表在守这一条。）

同一条原则的另一面是**不确定就声明不确定度，不要猜一个**：

- 容差带比噪声底还窄 → 写 `n/a (带宽 800 uV < 6x 噪声底 155 uV，测不了)`
- 到窗口结束还没进带 → 写 `n/a (未 settle)`，不报个假数
- 斜率 → 必须带测量窗口（dt 坍缩区间里噪声除以 2 ps 能大三个数量级）
- 单位从列名推的 → 打个 `?`；读不出来 → 写 `unknown`，不猜
- 压不进 20 KB → 在输出里说超了多少、为什么下不来
- 截图数字化 → 头里强制打精度上限，某列有多个候选就报「无法判定」

## 为什么不直接发 XML

drawio 的 XML 里 90% 是重复的样式串。一张 15 器件的模拟电路图约 30 KB，**2 KB/器件**——
`edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;` 这一串会出现几十次，
waypoint 圆点那串 190 字符的样式同样如此。按这个密度 150 管就是 300 KB，任何文本通道都塞不下。

`.rd` 把这些去重成一张样式字典，剩下每个图元/每根线一行。同一张图压到 3.7 KB。

## 核心约定

**脚本只做确定性变换，一切语义判断留给模型。**

| | 谁做 |
|---|---|
| 坐标解算（flipH/flipV/direction/rotation）、样式去重、文本清洗 | 脚本 |
| 判 d/g/s、合并节点、猜正交走线路径、关联浮动文本、起网络名 | 模型 |

理由是不对称的：**脚本猜错了信息已经丢了，没人看得出来；模型猜错了，它会把猜测显式列出来，人对着图能看出来。**

所以 `.ckt` 末尾永远有一段推断标注，列三类东西：waypoint 圆点被判定落在哪条线上、哪些差十几像素的端点被当成同一个网、哪个浮动文本框被贴给了哪个器件。**那段才是要核的地方**，网表本体反而不容易错。

同理，保留策略是**黑名单**：只丢已知纯渲染用途的样式 key，未知 key 一律保留——没见过的 key 更可能是有意义的形状变体，而不是装饰。

> 波形工具那条分工线**不一样**：脚本看得见全分辨率数据而模型永远看不见，
> 所以需要全分辨率才能算出的**测量**（峰值、抖动、settling、spur dBc）必须脚本做，
> 不做就永远丢了。脚本负责测量，模型负责诊断。详见 [`docs/wave-spec.md`](docs/wave-spec.md)。

## 画图不需要遵守任何约定

按习惯画就行。颜色（常用来区分电源域）、箭头方向、虚线、线宽、加粗、边上的标签全都保留。

两个可选的、零成本的增益：

- **画布角落放一个颜色图例文本框**（"蓝=VDD_1V8 / 绿=VDD_3V3"），色→电源域的映射自动传过去
- 参数想记录又不想占版面时，选中图元按 **`Ctrl+M`**（Edit Data）加具名属性，画布上不显示，`.rd` 里原样带出。想让参数在画布上可见就继续用浮动文本框

## 支持的 drawio 特性

压缩存储（base64+deflate）、多页、页面属性（尺寸/背景/math）、`<object>` 自定义属性、
`flipH`/`flipV`/`direction`/`rotation`、自环边、悬空端点、`<Array as="points">` 显式折点、
waypoint 结点圆点、边标签（含拖动过的位置和它自己的样式）、
HTML 富文本标签（`<b>` 转 `*…*` 保留强调，`V<sub>ss</sub>` → `Vss`）。

不支持 group / 容器的父子坐标折算（暂未实现；图元的嵌套关系可由 bbox 包含推断）。

## 来回一趟丢什么

`drawio_expand.py` 是 `drawio_reduce.py` 的逆。验收判据不是「XML 长得像」，
而是**原图和还原图各出一张 PNG 逐像素比**——`examples/demo.drawio` 走一圈回来，
除下面这三样之外零差异（连线走法、拐角、标签位置全在这个断言里）：

| 丢的 | 为什么是**有意**丢的 |
|---|---|
| 下标/上标 | `V<sub>ss</sub>` → `Vss`。下游要的是 `Vss` 这个**名字** |
| 斜体 | `clk/2` `VDD/VSS` 这类名字太常见，给 `/` 转义的噪声比换回斜体的收益大 |
| cell 的 z 序 | `.rd` 把图元和连线分成两段列，还原出来连线一律在图元之上 |

加粗、对齐方式、正交/直连、拐角圆不圆、边标签拖到哪，全都能回来。
细则和「三个不能按字面猜的 drawio 默认值」见 [`docs/rd-spec.md`](docs/rd-spec.md) §3 / §7。

## 数据不进这个仓库

`examples/` 里只有合成的脱敏样例。真实电路文件、真实波形一律留在这个仓库**之外**——
闸门是物理隔离，`.gitignore` 里那条 `private/` 只是纵深防御。

## License

MIT
