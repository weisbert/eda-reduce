# eda-reduce

把 EDA 工具的产出压成一段能直接粘贴给大模型的紧凑文本。

给那些**只能传文本、不能传文件**的场景用——隔离网络、无外网的仿真机器、
只有一个聊天框的通道。目标是把模型分析电路真正需要的信息搬出来，
而不是把文件压小。

```
原理图  my.drawio  ──drawio_reduce──▶  my.rd  (1/6 ~ 1/10)  ──▶  模型重建出网表 .ckt
波形    my.csv     ──wave_reduce────▶  my.wv  (≤20 KB)      ──▶  模型定位 debug 问题
截图    plot.png   ──plot_digitize──▶  my.wv                ──▶  （导不出数据时的兜底）
```

## 工具

| | 状态 | 干什么 |
|---|---|---|
| `tools/drawio_reduce.py` | **可用** | `.drawio` → `.rd`。纯标准库，Python 3.8+，无依赖 |
| `tools/wave_reduce.py` | 计划中 | Cadence 波形 CSV → `.wv`，带 Tkinter GUI 预览。见 [`docs/wave-spec.md`](docs/wave-spec.md) |
| `tools/plot_digitize.py` | 计划中 | 波形截图 → `.wv`。导不出数据时的兜底 |

```
docs/rd-spec.md       .rd 格式规范 + 设计约定（分工线、黑名单、坐标解算）
docs/ckt-format.md    .ckt 概念网表格式 —— 模型回给你的东西长什么样
docs/wave-spec.md     .wv 格式 + wave_reduce 设计约定（计划，未实现）
examples/demo.drawio  样例：教科书 5 管 OTA + 一小段架构图
examples/demo.rd      压缩结果（1.9 KB，同时充当回归测试基准）
examples/demo.ckt     从 .rd 重建出的网表 + 全部推断标注
```

## 用法

```bash
python tools/drawio_reduce.py my.drawio -o my.rd
python tools/drawio_reduce.py my.drawio --bbox 400,300,800,700   # 只导出一个区域
```

```
本地                                              对话里
──────────────────────────────────────────────────────────
画 my.drawio
   ↓  drawio_reduce.py
my.rd  (原始体积的 1/6 ~ 1/10)
   ↓  粘贴全文  ─────────────────────────────────→  重建 .ckt
                                                    ↓
   对着图核 .ckt 末尾的「推断标注」段  ←────────────  标注出所有猜测
```

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

压缩存储（base64+deflate）、多页、`<object>` 自定义属性、`flipH`/`flipV`/`direction`/`rotation`、
自环边、悬空端点、`<Array as="points">` 显式折点、waypoint 结点圆点、边标签、
HTML 富文本标签（`<b>` 转 `*…*` 保留强调，`V<sub>ss</sub>` → `Vss`）。

不支持 group / 容器的父子坐标折算（暂未实现；图元的嵌套关系可由 bbox 包含推断）。

## 数据不进这个仓库

`examples/` 里只有合成的脱敏样例。真实电路文件、真实波形一律留在这个仓库**之外**——
闸门是物理隔离，`.gitignore` 里那条 `private/` 只是纵深防御。

## License

MIT
