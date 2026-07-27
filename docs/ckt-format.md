# `.ckt` 概念网表格式 v1

模型重建电路结构后回给你的标准载体。设计目标：

- **省字节**：约 40–70 B/器件。60 管的模块 ≈ 3–4 KB（同一张图的 drawio XML 约 2 KB/器件）。
- **无歧义**：连接用「引脚=网络名」显式写死，不依赖画图坐标、不依赖器件顺序。
- **带意图**：网表只有拓扑，`.intent / .dc / .spec` 这些头部指令才是我做建模时真正缺的信息。
- **人机通用**：手敲得出来，模型也能从 `.rd` 重建出来。

---

## 1. 文件骨架

```
.block  <模块名>                     ← 必需，一个文件可以有多个 .block
.intent <这个模块是干什么的，一句话>   ← 强烈建议
.supply vdd=1.8 vss=0
.process 180nm CMOS / thick-ox 3.3V
.port   inp inn out vbias vdd vss    ← 对外端口，顺序即为例化时的顺序
.spec   gain>=40dB gbw>=50MHz cl=2p
.note   任意自由文本，可多行

<器件行> ...

.dc     tail=100u n1=1.55 out=0.9    ← 已知的静态工作点/偏置电流
.end
```

以 `#` 开头的行、以及行内 `#` 之后的部分都是注释。空行随意。

## 2. 器件行

```
<实例名>  <类型>  <引脚>=<网络> ...   [参数 k=v ...]   # 注释
```

一行一个器件，引脚顺序无所谓（因为是具名的）。例：

```
M1  nmos  d=n1  g=inp  s=tail  b=vss   w/l=40u/0.5u  m=4   # 输入对
R1  res   1=out 2=vfb                  r=20k
CL  cap   1=out 2=vss                  c=2p
```

### 类型与引脚表

| 类型 | 引脚 | 备注 |
|---|---|---|
| `nmos` `pmos` | `d g s b` | `b` 省略默认接体（nmos→vss, pmos→vdd） |
| `npn` `pnp` | `c b e` | |
| `res` `cap` `ind` | `1 2` | |
| `diode` | `a c` | |
| `sw` | `1 2` + `ctl=` | |
| `vsrc` `isrc` | `p n` | 理想源/偏置源 |
| `amp` | `inp inn out` | 已封装的运放/比较器，不展开 |
| `blk` | 自由 | 子模块例化，见下 |

不在表里的类型也可以用，只要引脚名自解释即可（例如 `xfmr p1=.. p2=.. s1=.. s2=..`）。

### 常用参数

`w/l=40u/0.5u`、`m=4`（并联数）、`r=` `c=` `l=`、`i=`（支路电流）、`gm=`、`type=lvt|hvt|native|io`。
写不确定的值就写 `?`，我会当成待定参数。

## 3. 网络命名约定

| 名字 | 含义 |
|---|---|
| `vdd` `vss` `gnd` `vbat` | 全局电源/地，不必在 `.port` 里重复声明 |
| `inp` `inn` `out` `outp` `outn` | 端口 |
| `tail` `n1` `n2` ... | 内部节点，随便起，能表意最好（`cascode_p`、`gate_m3`） |

**同名即同网**。不需要画连线，写同一个名字就是连在一起。

## 4. 层次化

```
.block ota5t
.port inp inn out vbias
M1 nmos d=n1 g=inp s=tail
...
.end

.block top
.port vin vout
X1  blk=ota5t  inp=vin  inn=vfb  out=vout  vbias=vb
R1  res 1=vout 2=vfb
R2  res 1=vfb  2=vss
.end
```

## 5. 完整示例

见 `examples/demo.ckt`（含末尾的推断标注段）。

---

> 早期版本这里有一节 draw.io 画图约定，已废弃——现在按你的习惯画就行，
> 结构由 tools/drawio_reduce.py 压缩后交给模型重建。见 docs/reduce-spec.md。
