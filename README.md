# drawio-reduce

把 draw.io 图（模拟电路原理图 / 系统架构图）压成一段能直接粘贴给大模型的紧凑文本。

给那些**只能传文本、不能传文件**的场景用：把 30 KB 的 `.drawio` 压成 3 KB 的 `.rd`，模型据此重建出网表。

```
tools/drawio_reduce.py   .drawio -> .rd 压缩器（纯标准库，Python 3.8+，无依赖）
docs/reduce-spec.md      .rd 格式规范 + 设计约定（分工线、黑名单、坐标解算）
docs/ckt-format.md       .ckt 概念网表格式 —— 模型回给你的东西长什么样
examples/demo.drawio     样例：教科书 5 管 OTA + 一小段架构图
examples/demo.rd         压缩结果（1.9 KB，同时充当回归测试基准）
examples/demo.ckt        从 .rd 重建出的网表 + 全部推断标注
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

## License

MIT
