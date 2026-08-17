# `.rd` 格式 & `drawio_reduce.py` 设计约定

## 0. 第一性原理

> **reduce 的唯一目标是「下游能从输出重建出正确的连接关系」，不是「输出本身就是个网表」。**

由此推出分工线：

| | 谁做 | 为什么 |
|---|---|---|
| 坐标解算、样式去重、文本清洗 | **脚本** | 纯确定性，量大，人/模型手算必错 |
| 判 d/g/s、合并节点、猜走线路径、关联浮动文本、起网络名 | **大模型** | 需要电路常识；**脚本猜错了没人看得出来，模型猜错了人对着图能看出来** |

第三条推论：**保留策略是黑名单**。只丢已知纯渲染用途的样式 key，未知 key 一律保留——一个没见过的 key 更可能是有意义的形状变体或自定义模板参数，而不是装饰。

## 1. 行格式

```
P "<页名>" <宽>x<高>  [{k=v}]                        页面
V <id> <S#> <x>,<y>,<w>,<h>  ["label"]  [{k=v}]     图元
T <id> <S#> <x>,<y>,<w>,<h>  ["label"]  [{k=v}]     纯文本框
J <id>      <cx>,<cy>                                waypoint 结点圆点
W <id> <E#> <端点> > <端点> [> ...]  ["label"] [|"边label"@pos [S#]] [{k=v}]
```

`P` 行：页面尺寸 + 其余非默认的 `mxGraphModel` 属性（`background` / `math` / …）。
导 PNG 时不带 `--crop` 就是按页面尺寸出图，所以这行是画布的一部分，不是元数据。

边 label 的 `@pos` 是 `x[,y[,ox,oy]]`，尾零省略：`x` 是沿线的相对位置（-1..1），
`y` 是垂直偏移，`ox,oy` 是拖动产生的像素 offset。只带 `x` 的话，
用户拖过的标签会弹回线上。后面那个可选的 `S#` 只在标签自己有非默认样式时出现。

`J` 只用于**默认 20×20 + 默认样式**的 waypoint；尺寸或样式改过的退回普通 `V` 行。

标签里 `\*` `\\` `\"` 是转义：`*…*` 是加粗标记（见 §7），`"` 是 `.rd` 的定界符。

端点四种形态：

| 形态 | 含义 |
|---|---|
| `8:250,350` | 接在 cell 8 上，且已解算出画布绝对坐标 |
| `@261.9,170` | 悬空端点，或路径上的显式折点 |
| `8:?` | 接在 cell 8 上但没有固定约束点（drawio 运行时算 perimeter 交点） |
| `8:~475,260` | `~` = 该点未做 perimeter 投影，可能差几像素 |

`{k=v}` 是 `Ctrl+M`（Edit Data）加在图元上的自定义属性，原样带出。

## 2. 样式字典

残留样式在整张图里高度重复，去重后按 `S#`（顶点）/ `E#`（边）引用，legend 里带出现次数：

```
S4   x1   electrical.transistors.nmos flipH=1
S5   x2   electrical.signal_sources.vss2 flipV=1 fontSize=18
S8   x1   fillColor=#dae8fc strokeColor=#6c8ebf
E2   x14  endArrow=none endFill=0
```

副作用：**电源域一眼可见**——用颜色区分电源域时，legend 里每个色组自成一行并带计数，不用去数 hex 重复。

> 建议在画布角落画一个颜色图例文本框（"蓝=VDD_1V8 / 绿=VDD_3V3"），它会作为普通 `T` 行带出来，色→电源域的映射就自动传过去了。

## 3. 保留 / 丢弃

闸门只有一条：**这个 key 变了，画出来的图会不会变？** 不会变的才丢。

会变的一律留，代价是每个**去重后的样式**几十字符（不是每个 cell）——
`.rd` 的样式字典让这笔开销跟图的大小无关，所以不值得为省它丢掉还原不回来的信息。

**丢**（纯编辑器/交互元数据，改了画面不变）：`html` `pointerEvents` `resizable`
`rotatable` `movable` `deletable` `editable` `connectable` `autosize` `snapToPoint`
`aspect` `outlineConnect` `backgroundOutline` `expand` `recursiveResize` `container`

**取默认值时丢**（纯开关，缺省即关闭）：`shadow` `sketch` `glass` `comic` `noLabel`
`fillStyle=solid` `points=[]`；以及 `exitDx` `exitDy` `entryDx` `entryDy`
`exitPerimeter` `entryPerimeter` `flipH` `flipV` `dashed` 取 0 时

**被坐标吸收后丢**（只在边上）：`exitX` `exitY` `entryX` `entryY` 及其 Dx/Dy/Perimeter

**无标签的 cell 上丢排版 key**：`align` `verticalAlign` `labelPosition`
`verticalLabelPosition` `whiteSpace` `noLabel` `spacing*` `labelBackgroundColor`
`labelBorderColor` `textOpacity`。电路图里绝大多数图元（管子、电容、结点）都没标签，
这一条把「保住排版信息」的代价按住了：`demo.drawio` 上总共只多 336 字节。

**其余一律保留**，其中这些是明确有语义的：`shape` `fillColor` `strokeColor`
`gradientColor` `fontColor` `fontStyle` `strokeWidth` `dashed` `dashPattern`
`opacity` `flipH` `flipV` `direction` `rotation` `el*` `endArrow` `startArrow`
`endFill` `startFill`

### 三个「默认值」不能按字面猜的地方

都是渲染实测出来的，不是读文档读出来的（`tests/test_drawio.py::TestPreservedInfo`
每条配一个断言）：

| key | 想当然 | 实测 |
|---|---|---|
| `edgeStyle` | 缺省就是正交 | **缺省是直连**。正交边不存折点，丢了这个就只能连成斜线 |
| `rounded`（边上）| 缺省 0（直角）| **缺省是圆角**。所以 `rounded=0` 是非默认值，不能当零丢 |
| `jettySize` | `auto` 就是缺省 | **缺省是常数 10**，`auto` 是按箭头现算的，正交边第一段差十几像素 |

同一个道理让 `align` / `verticalAlign` / `*LabelPosition` / `whiteSpace` 也不能按
默认值丢：它们的默认值不是常数，drawio 的**内置具名样式**自带一套——`text;` 的
`align` 默认是 `left` 而不是 `center`。

### 具名样式必须排在最前

样式串里没有 `=` 的词（`text` `ellipse` `swimlane` …）是 drawio 的内置具名样式，
mxGraph **从左往右合并**，所以它会顶掉排在它前面的同名 key。
`canon_style` 因此把这些词放在所有 `k=v` 之前、且保持原有先后；
按字母排序会写出 `align=center text` 这种被自己顶掉的串（标签会整体左移）。

带空格的值（`dashPattern=8 8`、`fontFamily=Times New Roman`）在样式字典里加引号，
因为那一栏是按空格分词的。

架构图专属：**箭头方向**（`endArrow=none` 与 `classic` 语义完全不同）、**虚线**（常表示可选/控制路径）、**线宽**（总线 vs 单线）、**边上的 label**（总线名）、**加粗**（`<b>` 转成 `*…*` 保留）。

## 4. 坐标解算

复刻 `mxGraph.getConnectionPoint` 的非 perimeter 分支，顺序与源码一致：

```
direction 旋转 bounds (north/south 交换宽高)
  -> 按归一化 (exitX, exitY) 取点，加 (exitDx, exitDy)
  -> flipH: x = 2*cx - x ；flipV: y = 2*cy - y     ← 绕图元中心镜像
  -> 按 rotation + direction 角度绕中心旋转
```

`flipH`/`flipV` 是**绕图元中心镜像**，不是交换连接点索引——这一条直接来自 mxGraph 源码 `mxGraph.getConnectionPoint()` 的 `point.x = 2 * bounds.getCenterX() - point.x`，且发生在 `constraint.perimeter == false` 分支内、通用旋转之前。`examples/demo.drawio` 把 flipH / flipV / direction=south 三条路径都跑到了。

`perimeter=centerPerimeter` 的图元（waypoint 圆点）连接点恒为中心，无约束时直接给中心坐标——这是读样式，不是猜。

文本清洗：去 HTML 标签与 `$$…$$`；`<b>/<i>` 转 `*…*` / `/…/` 保留强调；`<sub>/<sup>/<span>/<font>` 直接去标签不留空格（`V<sub>ss</sub>` → `Vss`）。

## 5. 不做的事（明确列出，避免以后手痒加上）

- 不判引脚名
- 不合并节点（端点差 10~12px 算不算同网 = 电路知识）
- 不重建正交走线的实际路径
- 不关联浮动文本框和图元
- 不起网络名
- 不裁掉任何"看起来没用"的图元

## 6. 实测

`examples/demo.drawio`：11,830 → 2,273 字节，**5.2 倍**，零个未解端点。

压缩比随图的规模上升——重复的样式串是每 cell 定值开销，而 `.rd` 的行长基本不变。
在一张 15 器件 / 23 结点 / 37 连线的真实模拟电路图上实测 **8.2 倍**（30.4 KB → 3.7 KB）；
外推 150 器件约 18–20 KB，落在常见的 80 KB 文本上限内。
超了用 `--bbox x1,y1,x2,y2` 按区域切，一次分析一个模块。

`examples/demo.rd` 一并提交，改动工具后 diff 它即可当回归测试。

## 7. 反方向：`drawio_expand.py`

`.rd` → `.drawio`，跟 reduce 严格互为逆。判据不是「XML 长得像」而是
**两边各出一张 PNG 逐像素比**（`tests/test_drawio.py::TestRenderedPixels`，
本机装了 draw.io 才跑）。

日常回归靠一条更便宜的不变式——**`.rd` 是定点**：

```
reduce(expand(reduce(x))) == reduce(x)
```

一句话同时守住「reduce 丢了什么」和「expand 猜错了什么」：任何一位信息在路上掉了
或被补错，第二遍 reduce 就对不上。它管不住的只有「两边一致地认错同一个 drawio
默认值」——那一类只有真渲染能抓，上面 §3 那三个坑就是这么抓出来的。

expand 侧要**反着**做的两件事，都不能省：

- 端点绝对坐标 → `exitX`/`exitY`：必须倒着过一遍 rotation → flip → direction，
  不能直接 `(x-bx)/bw`。flipH 的图元上直接除会差一整个宽度（`exitX=1` 变 `exitX=0`，
  点落到对面那条边上）。
- `~` 标记：有 `~` = 原图 `exitPerimeter` 用的默认值 1；没有 `~` = 原图显式写了 0，
  要写回去。

反解出来的 `exitX` 会贴到 0/¼/⅓/½ 这些常见值上——坐标在 `.rd` 里量化到 0.1，
除回去带零头，零头本身看不出来，但写进文件就再也认不出「这里原本是正中间」了。

### 三处**不可逆**（reduce 侧有意丢的，别指望 expand 补回来）

| | 为什么故意丢 |
|---|---|
| 下标/上标 | `V<sub>ss</sub>` → `Vss`。下游要的是 `Vss` 这个**名字**；留标记就得让 `.ckt` 的网络名跟着变 |
| 斜体 | `<i>x</i>` → `/x/` 只是单向标记。`clk/2` `VDD/VSS` 这类名字太常见，给 `/` 转义的噪声比换回斜体的收益大 |
| cell 的 z 序 | `.rd` 把图元和连线分成两段列（可读性），还原出来连线一律在图元之上 |

加粗是可逆的：`<b>` ↔ `*…*`，正文里本来就有的 `*` 在 reduce 侧转义成 `\*`，
所以 `2*C` 不会被读成加粗。

group / 容器的父子坐标折算两边都不支持（reduce 侧就没做，见 §5）。
