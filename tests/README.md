# tests

```bash
python -m unittest discover -s tests            # 全套，约 7 秒
python -m unittest discover -s tests -v         # 看每一条
EDA_REDUCE_GUI_TEST=1 python -m unittest discover -s tests   # 加上会弹窗的 GUI 自检
```

用 **unittest**，不引入 pytest —— 隔离区机器上装不了东西，
测试必须能用系统 python3 直接跑。

## 每个文件守什么

| 文件 | 守的东西 |
|---|---|
| `test_parse.py` | 布局 A/B、脏数据、单位不猜、log/lin 判定、dt 坍缩 |
| `test_reduce.py` | **自检诚实**（独立实现对拍）、点数预算、强制点、量化步长、肩点、噪声底 |
| `test_metrics.py` | 测量对真值；**spur 保护（LOCKED）**；适用性判断 |
| `test_format.py` | `.wv` 格式契约、**不写形容词**、**纯标准库**、20 KB 预算 |
| `test_digitize.py` | 截图数字化闭环（渲染→数字化→比对，误差落在像素精度内） |
| `test_regression.py` | `examples/demo_*.wv` 逐字节基线 + 生成器确定性 |
| `test_gui.py` | GUI 无人值守自检（默认跳过）+ 不碰 Tk 的纯计算部分 |

## 几条不太一样的地方

**容差都写了理由。** 凡是写不出理由的容差，说明那个测量本身没想清楚。
例如时钟抖动那条写的是「注入 3 ps/沿，dt 是 15 ps，全靠阈值穿越插值；
不插值的话这一项会差 10% 以上」——这条测试守的就是插值那行代码。

**自检有独立实现对拍。** `test_reduce.naive_recon_error` 是故意写笨的：
不复用 `wave_core` 的段游标，每个点二分。慢，但和被测代码没有共同的错误。
`.wv` 头第二行是读的人第一个该看的东西，它要是报小了，整个格式就不可信。

**spur 那一组有反证。** `test_without_protection_spurs_are_lost` 关掉 metrics
再跑一遍，证明三根 spur 会丢掉两根。它存在的意义是让「保护规则值多少」有个数——
以后谁想简化抽点逻辑，能看见代价。

**「说出来了没有」也是断言。** 脏数据被处理了要进 notes、
容差带比噪声窄要说测不了、压不进预算要声明、截断了事件要报总数。
这些跟数值正确性同等重要：静默的处理是这个项目最怕的东西。

## 更新回归基线

基线**变了不一定是错**——改进了输出当然会变。测试的作用是让变化无法悄悄发生。
确认 diff 是你想要的之后：

```bash
python tools/wave_reduce.py examples/demo_tran.csv -o examples/demo_tran.wv
python tools/wave_reduce.py examples/demo_ac.csv   -o examples/demo_ac.wv
python tools/wave_reduce.py examples/demo_spec.csv -o examples/demo_spec.wv
```

改了样例生成器就得连样例一起重来（它们是确定性的，所以重来必须逐字节一致）：

```bash
python examples/gen_demo_wave.py
python examples/gen_demo_plot.py
```

## 真实波形不进这里

`examples/` 只有合成样例。真实波形一律留在工作区仓，闸门是物理隔离，
`.gitignore` 里那几条只是纵深防御。
