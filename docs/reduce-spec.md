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
V <id> <S#> <x>,<y>,<w>,<h>  ["label"]  [{k=v}]     图元
T <id> <S#> <x>,<y>,<w>,<h>  ["label"]  [{k=v}]     纯文本框
J <id>      <cx>,<cy>                                waypoint 结点圆点
W <id> <E#> <端点> > <端点> [> ...]  ["label"] [|"边label"@rel] [{k=v}]
```

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

**丢**（纯渲染）：`html` `shadow` `jettySize` `orthogonalLoop` `pointerEvents` `resizable` `rotatable` `snapToPoint` `perimeter` `sketch` `fillStyle` `aspect` `align` `verticalAlign` `verticalLabelPosition` `labelPosition` `whiteSpace` `arcSize` `glass` `comic` `edgeStyle` `startSize` `endSize` `points` `movable` `deletable` `editable` `connectable` `noLabel` `autosize` `fontFamily` `fontSource` `outlineConnect` `backgroundOutline`

**取默认值时丢**：`exitDx` `exitDy` `entryDx` `entryDy` `exitPerimeter` `entryPerimeter` `flipH` `flipV` `dashed` `rounded`

**被坐标吸收后丢**（只在边上）：`exitX` `exitY` `entryX` `entryY` 及其 Dx/Dy/Perimeter

**其余一律保留**，其中这些是明确有语义的：`shape` `fillColor` `strokeColor` `gradientColor` `fontColor` `fontStyle` `strokeWidth` `dashed` `dashPattern` `opacity` `flipH` `flipV` `direction` `rotation` `el*` `endArrow` `startArrow` `endFill` `startFill`

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

`examples/demo.drawio`：11,830 → 1,937 字节，**6.1 倍**，零个未解端点。

压缩比随图的规模上升——重复的样式串是每 cell 定值开销，而 `.rd` 的行长基本不变。
在一张 15 器件 / 23 结点 / 37 连线的真实模拟电路图上实测 **8.2 倍**（30.4 KB → 3.7 KB）；
外推 150 器件约 18–20 KB，落在常见的 80 KB 文本上限内。
超了用 `--bbox x1,y1,x2,y2` 按区域切，一次分析一个模块。

`examples/demo.rd` 一并提交，改动工具后 diff 它即可当回归测试。
