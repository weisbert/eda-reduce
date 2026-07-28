#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wave_demod.py — 把载波解调掉，只传调制。

**存在的理由是信息率不等于采样率。** 2 µs 里 2515 个 5 GHz 周期，没有 2515 份
独立信息：载波是重复的，真正在变的是包络、频率、相位。折线把同一件事重复画了
2515 遍，实测 218 KB；解调之后 26 KB，而且回答的正是「起振包络长什么样」
「频率被牵引了多少」这两个问题。

四件东西打进**同一份** `.wv`（一键复制是硬要求，不许拆文件）：

    [SHAPE]     env_hi / env_lo / f_inst —— 逐周期的上下包络和瞬时频率
    [CYCLES]    几个代表性周期的**原始样点**，全时间分辨率
    [METRICS]   跑在解调后的信号上（起振时刻、包络极值、settle…）
    # demod:    载波中位频率、周期数、逐周期残差 —— 头部一行说清可信度

三条设计约束，每条都有理由：

1. **不发明数据。** 非振荡区间照样出包络（上下沿相等 = 信号自己），
   所以起振前那段直流台阶不会丢；那些窗口的 `f_inst` 是 0，
   意思是「这里没有振荡」，不是「测出来是 0」。
2. **异常周期必须留原始样点。** 代表性周期挑在极值处（包络最大/最小、
   频率偏离最大、残差最大），这是把频域那条「spur 强制保留」搬到时域：
   解调会把畸变抹平，而畸变正是唯一有价值的东西。
3. **自检照旧。** 逐周期拿「该周期的幅度+周期」反算一条正弦跟原始样点比，
   报最大残差。残差大 = 这段不是正弦，解调的前提不成立 —— 说出来，不假设。

依赖：纯标准库。缺了这个文件，`wave_core + wave_emit + wave_cli` 照样跑
（只是没有 `--demod`）—— 逃生舱那条线不能被这个功能拖下水。

Python 3.6+。
"""

import math
from array import array

import wave_core as core

MIN_CYCLES = 20           # 少于这么多周期就别解调，逐点画更清楚
N_REPRESENT = 6           # 默认留几个代表性周期
CYCLE_BUDGET_FRAC = 0.30  # 代表性周期最多吃掉这么多预算
MIN_CYCLE_PTS = 4         # 一个周期少于这么多原始样点，形状本来就没了
PERIOD_OUTLIER = 2.0      # 周期偏离中位这么多倍就不是载波周期，是过渡
MAX_FILL = 200            # 一个空档最多补这么多窗口
FREQ_MAX_PTS = 400        # 瞬时频率最多这么多点，多了就按整数个周期平均


class Cycle(object):
    """一个载波周期。t0/t1 是插值出来的过零时刻，不是栅格点。"""

    __slots__ = ("t0", "t1", "i0", "i1", "hi", "lo", "resid", "why")

    def __init__(self, t0, t1, i0, i1, hi, lo, resid=0.0):
        self.t0, self.t1, self.i0, self.i1 = t0, t1, i0, i1
        self.hi, self.lo, self.resid = hi, lo, resid
        self.why = ""

    @property
    def period(self):
        return self.t1 - self.t0

    @property
    def amp(self):
        return 0.5 * (self.hi - self.lo)

    @property
    def mid(self):
        return 0.5 * (self.hi + self.lo)

    @property
    def at(self):
        return 0.5 * (self.t0 + self.t1)


def crossings(x, y, level, hyst):
    """上升沿穿过 level 的时刻，**二次插值**定位。

    为什么不能只用线性插值（这条是实测逼出来的）：

    - 按栅格点取 -> 周期被步长量化，5 GHz 载波配 10 ps 栅格就是 5% 的假抖动。
    - 线性插值 -> 弦的零点跟正弦的零点差一个 `(ω·dt)²/24` 量级的项，
      而这个差**随过零落在栅格里的位置周期性走动**。20 样点/周期时那个拍约
      8 个周期，表现成瞬时频率一高一低地跳 ±200 kHz。更坑的是它躲不过平均：
      跨 4 个周期测正好把 8 周期的拍**混叠**成逐点交替，看着像真的在抖。
    - **二次插值反而更差**（实测 2.6e-4 vs 线性 1.3e-4）：正弦在过零处曲率为零，
      线性本来就已经是三阶精度，硬塞一个单边的二次项是在加噪。差的那一项是**三次**的。
    - 四点三次插值把它消掉：实测相对误差 1.3e-4 -> 2.8e-6，**48 倍**。
      从线性解出发走两步牛顿，修正量只有 dt 的千分之几，不用挑根。

    滞回防的是噪声在 level 附近反复穿越。
    """
    out = []
    armed = True
    n = len(y)
    for i in range(1, n):
        if armed and y[i - 1] < level <= y[i]:
            dy = y[i] - y[i - 1]
            w = (level - y[i - 1]) / dy if dy else 0.0
            t = x[i - 1] + w * (x[i] - x[i - 1])
            if 2 <= i < n - 1:
                t = _newton_cubic([x[k] for k in (i - 2, i - 1, i, i + 1)],
                                  [y[k] for k in (i - 2, i - 1, i, i + 1)],
                                  level, t, abs(x[i] - x[i - 1]))
            out.append(t)
            armed = False
        elif not armed and y[i] < level - hyst:
            armed = True
    return out


def _newton_cubic(ts, ys, level, t, span):
    """过四点的三次 Lagrange 多项式上解 q(t) = level。从线性解走两步牛顿。"""
    for _ in range(2):
        q = dq = 0.0
        for i in range(4):
            den = 1.0
            for j in range(4):
                if j != i:
                    den *= (ts[i] - ts[j])
            if den == 0.0:
                return t
            num = 1.0
            for j in range(4):
                if j != i:
                    num *= (t - ts[j])
            d = 0.0
            for j in range(4):
                if j == i:
                    continue
                p = 1.0
                for k in range(4):
                    if k != i and k != j:
                        p *= (t - ts[k])
                d += p
            q += ys[i] * num / den
            dq += ys[i] * d / den
        if dq == 0.0:
            return t
        step = (q - level) / dq
        if abs(step) > span:              # 牛顿跑飞了就退回线性解，不硬撑
            return t
        t -= step
    return t


def _residual(x, y, i0, i1, c):
    """拿「该周期的幅度 + 周期」反算一条正弦，跟原始样点比。-> 相对残差。

    不做拟合、不解优化：相位由过零时刻定死，幅度中点由 min/max 定死。
    所以这个数是**可判定**的——它说的是「这一段有多不像正弦」，
    而不是「我拟合得多好」。
    """
    if c.amp <= 0 or c.period <= 0 or i1 - i0 < MIN_CYCLE_PTS:
        return 0.0
    w = 2.0 * math.pi / c.period
    worst = 0.0
    for i in range(i0, i1):
        fit = c.mid + c.amp * math.sin(w * (x[i] - c.t0))
        d = abs(y[i] - fit)
        if d > worst:
            worst = d
    return worst / c.amp


def find_cycles(tr, si=0):
    """-> (cycles, level)。切不出周期就返回 ([], level)。"""
    s = tr.signals[si]
    x, y = tr.x, s.y
    if s.rng <= 0 or len(x) < 8:
        return [], 0.0
    level = 0.5 * (s.vmin + s.vmax)
    hyst = max(0.05 * s.rng, core.NOISE_K * s.noise)
    if hyst >= 0.5 * s.rng:
        return [], level
    cr = crossings(x, y, level, hyst)
    if len(cr) < 3:
        return [], level
    raw = []
    for k in range(len(cr) - 1):
        t0, t1 = cr[k], cr[k + 1]
        i0 = _idx_after(x, t0)
        i1 = _idx_after(x, t1)
        if i1 <= i0:
            continue
        seg = y[i0:i1]
        raw.append(Cycle(t0, t1, i0, i1, max(seg), min(seg)))
    if not raw:
        return [], level
    # 周期偏离中位太远的**不是载波周期**，是过渡：一个直流台阶穿过中线一次，
    # 就会跟下一个真周期凑出一个 100 ns 的假「周期」——残差 100%、频率 4.7 MHz，
    # 把中位载频、频率量程和代表周期的挑选全带偏。丢掉，让它变成空档。
    per = sorted(c.period for c in raw)
    tmed = per[len(per) // 2]
    out = []
    for c in raw:
        if not (tmed / PERIOD_OUTLIER <= c.period <= tmed * PERIOD_OUTLIER):
            continue
        c.resid = _residual(x, y, c.i0, c.i1, c)
        out.append(c)
    return out, level


def _idx_after(x, t):
    import bisect
    return bisect.bisect_left(x, t)


def _windows(tr, cycles):
    """整条时间轴切成窗口：有周期的地方用周期边界，没有的地方按中位周期补。

    补窗口是为了**不丢起振之前那段故事**（真实文件里 1.3 µs 处有个直流台阶）。
    那些窗口的上下包络就是信号自己，`f_inst` 记 0 = 这里没有振荡。
    """
    x = tr.x
    tmed = sorted(c.period for c in cycles)[len(cycles) // 2]
    bounds = [(c.t0, c.t1, c) for c in cycles]
    out = []
    prev = x[0]
    for t0, t1, c in bounds:
        if t0 - prev > 1.5 * tmed:
            out.extend(_fill(prev, t0, tmed))
        out.append((t0, t1, c))
        prev = t1
    if x[-1] - prev > 1.5 * tmed:
        out.extend(_fill(prev, x[-1], tmed))
    return out


def _fill(a, b, w):
    """空档按中位周期补窗口，但**封顶**。

    不封顶的话，1.5 µs 的起振前死区会按 199 ps 的载波周期补出 7500 个窗口 ——
    那段是直流，200 个点绰绰有余，剩下 7300 个纯粹是让人误以为「解调出了
    8994 个包络点」。RDP 后来确实会把它们压掉，但代价白花，数字还骗人。
    """
    n = max(1, min(MAX_FILL, int(math.ceil((b - a) / w))))
    step = (b - a) / n
    return [(a + i * step, a + (i + 1) * step, None) for i in range(n)]


def _derived(tr, xs, cols, index=0):
    out = core.Trace(tr.xname, index=index)
    out.source = tr.source
    out.xunit, out.xunit_src = tr.xunit, tr.xunit_src
    out.kind, out.kind_src = tr.kind, tr.kind_src
    out.window = tr.window
    out.x = array("d", xs)
    for nm, unit, src, col in cols:
        d = core.Signal(nm, unit, src)
        d.y = array("d", col)
        out.signals.append(d)
    return out


def demod(tr, si=0, min_cycles=MIN_CYCLES):
    """-> ([派生 Trace, ...], cycles)。切不出足够周期就返回 ([], cycles)。

    出**两块**而不是一块，理由是量程：

    - `env` 覆盖整条时间轴（空档处上下沿相等 = 信号自己），所以起振**之前**
      那段直流台阶不会丢。这块是「整体故事」。
    - `f_inst` 只覆盖有振荡的那一段。混在一起的话，死区那些「0 Hz」会把量程
      从「±30 MHz 的牵引」撑成「0..5 GHz」，eps 跟着粗 100 倍，牵引就看不见了；
      METRICS 里还会冒出 4.7e16 Hz/s 这种没意义的 slew。

    两块仍然在**同一份** .wv 文档里（emit 把多条 trace 串成一份），一键复制不变。
    """
    cycles, _ = find_cycles(tr, si)
    if len(cycles) < min_cycles:
        return [], cycles
    s = tr.signals[si]
    x, y = tr.x, s.y
    wins = _windows(tr, cycles)

    xs, hi, lo = [], [], []
    for t0, t1, c in wins:
        if c is not None:
            xs.append(c.at)
            hi.append(c.hi)
            lo.append(c.lo)
            continue
        i0, i1 = _idx_after(x, t0), _idx_after(x, t1)
        seg = y[i0:i1] if i1 > i0 else [y[min(i0, len(y) - 1)]]
        xs.append(0.5 * (t0 + t1))
        hi.append(max(seg))
        lo.append(min(seg))

    env = _derived(tr, xs, [
        ("env_hi(%s)" % s.name, s.unit, s.unit_src, hi),
        ("env_lo(%s)" % s.name, s.unit, s.unit_src, lo)], index=tr.index)
    out = [env]
    fx, fy, m, sd = _freq_trace(cycles)
    if len(fx) >= 2:
        f = _derived(tr, fx, [("f_inst(%s)" % s.name, "Hz", "declared", fy)],
                     index=tr.index + 1)
        if m > 1:
            f.note("频率是**跨 %d 个周期**测的（f = %d / (t[k+%d] - t[k])），"
                   "不是逐周期测完再平均：逐周期的插值误差约 %s（1σ）且带系统性拍，"
                   "跨 %d 个周期测直接把它除到约 %s"
                   % (m, m, m, core.eng_str(sd, "Hz", 3), m,
                      core.eng_str(sd / m, "Hz", 3)))
        out.append(f)
    for t in out:
        for n in tr.notes:
            t.note(n)
        for n in tr.warns:
            t.warn(n)
    env.note(summary(cycles, s, len(wins)))
    if len(out) > 1:
        out[1].note("这一块是**瞬时频率**（每个载波周期一个点，过零时刻插值定位），"
                    "只覆盖有振荡的区间；上一块是包络，覆盖整条时间轴")
    return out, cycles


def _freq_trace(cycles, max_pts=FREQ_MAX_PTS):
    """跨 M 个周期测频：`f = M / (t[k+M] - t[k])`。-> (x, y, M, 逐周期 1σ)

    **不是「逐周期测完再平均」。** 两件事差得很远：

    - 逐周期测的误差主要来自过零点的线性插值，而插值误差随「过零落在栅格哪个
      位置」周期性走动。实测 20 样点/周期时它是一个约 8 个周期的**系统性拍**，
      表现成 f 一高一低地跳（29.79 / 30.20 / 29.80 / 30.19 …）。
      取平均只把随机部分降到 1/√M，那个拍降不掉——而且平均窗口还会跟它混叠。
    - 跨 M 个周期测，两端各一个插值误差，除以 M：误差直接降到 1/M，
      系统性的那部分也一起除掉。

    副作用是那条假的一高一低还会把 `count_cycles` 骗成「这条曲线在振荡」，
    于是频率块上冒出一条「只有 2.9 点/周期」的假 WARN。根因治掉，警告自己就没了。
    """
    n = len(cycles)
    t = [c.t0 for c in cycles] + [cycles[-1].t1]
    per = [t[i + 1] - t[i] for i in range(n)]
    d = sorted(abs(per[i + 1] - per[i]) for i in range(n - 1)) if n > 1 else [0.0]
    tm = sorted(per)[n // 2]
    sd = 0.0
    if tm > 0 and d:
        sd = d[len(d) // 2] * 1.4826 / math.sqrt(2.0) / (tm * tm)   # 周期 -> 频率
    m = max(1, int(math.ceil(n / float(max_pts))))
    ox, oy = [], []
    for i in range(0, n - m + 1, m):
        p = _ls_period(t[i:i + m + 1])
        if p > 0:
            ox.append(0.5 * (t[i] + t[i + m]))
            oy.append(1.0 / p)
    return ox, oy, m, sd


def _ls_period(ts):
    """一段过零时刻 -> 周期，**最小二乘拟合 t_k = t0 + k·T**，不是两端相减。

    两端相减有个隐蔽的坑：相邻两段共用中间那个过零点，它的误差在前一段算作
    「末端」、在后一段算作「起点」，符号相反 —— 于是**误差被结构性地反相关**，
    画出来就是一条一高一低的锯齿，看着像真的在抖，其实是切法造成的。
    最小二乘用上段内每一个过零点，共用点只占 1/(m+1) 权重，锯齿就没了。
    """
    n = len(ts)
    if n < 2:
        return 0.0
    kbar = 0.5 * (n - 1)
    tbar = sum(ts) / n
    num = den = 0.0
    for k in range(n):
        dk = k - kbar
        num += dk * (ts[k] - tbar)
        den += dk * dk
    return num / den if den else 0.0


def summary(cycles, sig, n_win):
    per = sorted(c.period for c in cycles)
    f = [1.0 / p for p in per if p > 0]
    res = max(c.resid for c in cycles)
    worst = max(cycles, key=lambda c: c.resid)
    return ("解调：%d 个周期 -> %d 个包络点；载波中位 %s（%s .. %s）；"
            "逐周期残差最大 %.1f%% @ %s。**SHAPE 里画的是包络不是波形**，"
            "原始波形看 CYCLES 段"
            % (len(cycles), n_win,
               core.eng_str(f[len(f) // 2] if f else 0.0, "Hz", 6),
               core.eng_str(min(f) if f else 0.0, "Hz", 5),
               core.eng_str(max(f) if f else 0.0, "Hz", 5),
               100.0 * res, core.eng_str(worst.at, "s", 5)))


def pick_representative(cycles, n=N_REPRESENT):
    """挑代表性周期：极值处，不是均匀撒。

    均匀撒会漏掉唯一有价值的那几个 —— 解调把包络抹平了，
    畸变、频率跳变、残差爆表的地方才是要看原始样点的地方。
    同一条规则在频域叫「spur 强制保留」，理由一模一样。
    """
    if not cycles:
        return []
    f = sorted(1.0 / c.period for c in cycles if c.period > 0)
    fmed = f[len(f) // 2] if f else 0.0
    picks = []

    def add(c, why):
        """同一个周期同时是好几处极值时，理由要**叠加**而不是丢掉。

        起振第一个周期往往同时是「残差最大」（幅度小，相对残差自然大），
        直接去重会把后一条理由吞掉，读的人就不知道它还是残差冠军。
        """
        if c is None:
            return
        for p in picks:
            if p is c:
                if why not in p.why:
                    p.why += " + " + why
                return
        c.why = why
        picks.append(c)

    imax = max(range(len(cycles)), key=lambda i: cycles[i].amp)
    add(cycles[imax], "包络最大")
    add(cycles[0], "起振第一个周期")
    # 「包络最小」要在**峰之后**找：起振第一个周期天然是全局最小，
    # 直接取 min 会跟它撞成同一个，于是 squegging 的谷底一个都挑不到 ——
    # 而谷底恰恰是这整件事里最该看原始波形的地方
    tail = cycles[imax + 1:]
    if len(tail) > 2:
        add(min(tail, key=lambda c: c.amp), "起振后包络最小（谷底）")
    add(max(cycles, key=lambda c: c.resid), "残差最大（最不像正弦）")
    if fmed > 0:
        add(max(cycles, key=lambda c: abs(1.0 / c.period - fmed)
                if c.period > 0 else 0.0), "频率偏离最大")
    # 还有余额就按时间等距补，让人能看到形状随时间怎么变
    if len(picks) < n and len(cycles) > n:
        step = len(cycles) // (n - len(picks) + 1)
        for k in range(1, n - len(picks) + 1):
            add(cycles[min(len(cycles) - 1, k * step)], "时间等距采样")
    picks.sort(key=lambda c: c.at)
    return picks[:n]


def cycles_block(tr, si, picks, colspec, xunit, budget=None):
    """-> [str]。`[CYCLES]` 段：代表性周期的**原始样点**，不抽点。

    t 相对各自周期起点，所以几个周期能直接叠着比形状。
    y 的单位/量化跟 SHAPE 那一列完全一致（同一个 ColSpec），不用换算两次。
    """
    if not picks:
        return []
    s = tr.signals[si]
    x, y = tr.x, s.y
    tq = _tq(picks)
    # 这一段必须**自带列声明**：上面那些列是 env_hi/env_lo（派生量），
    # 这里是原始信号，量程不同，词头和 offset 就可能不同。让读的人去套上面的
    # 声明，迟早套错一个数量级。
    out = ["[CYCLES] t_rel %s" % colspec.label,
           "# 代表性周期的**原始样点**（未抽点）；t_rel 相对各周期起点，单位 %s"
           % tq[1],
           "# %-6s %-16s [%-7s] offset %-11s   (量化 %s)"
           % (colspec.label, s.name, colspec.unit_out, colspec.off_txt,
              colspec.q_txt)]
    used = 0
    for c in picks:
        head = ("# @ %s  幅度 %s  周期 %s  残差 %.1f%%  (%s)"
                % (core.eng_str(c.at, xunit, 6),
                   core.eng_str(c.amp, s.unit, 4),
                   core.eng_str(c.period, xunit, 4),
                   100.0 * c.resid, c.why or "-"))
        rows = []
        for i in range(c.i0, c.i1):
            rows.append("%s %s" % (core.strip_num((x[i] - c.t0) / tq[0], 3),
                                   colspec.txt(y[i])))
        blk = [head] + rows
        n = sum(len(b) + 1 for b in blk)
        if budget is not None and used + n > budget:
            out.append("# （预算只够放 %d 个代表周期，还有 %d 个没放）"
                       % (len([o for o in out if o.startswith("# @")]),
                          len(picks) - len([o for o in out
                                            if o.startswith("# @")])))
            break
        used += n
        out.extend(blk)
    return out


def apply(tr, tol, budget=None, n_cycles=N_REPRESENT, min_cycles=MIN_CYCLES,
          kind=None, xscale=None):
    """解调 + 挂上 `[CYCLES]`。-> [trace, ...]；没生效就返回 `[原 trace]`。

    **命令行和 GUI 走同一条路**。上一轮 `--xrange` / `--max-cand` 是分别接进两边的，
    结果 `--demod` 只接了命令行、GUI 静默忽略——同一个功能有两个入口就迟早分叉。
    """
    out, cycles = demod(tr, 0, min_cycles=min_cycles)
    if not out:
        tr.note("--demod 没生效：只切出 %d 个载波周期（要 >= %d）。"
                "这条信号可能不是准正弦，或者时间分辨率不够撑起周期"
                % (len(cycles), min_cycles))
        return [tr]
    for t in out:
        core.analyze(t, kind=kind, xscale=xscale)
    picks = pick_representative(cycles, n_cycles)
    if picks:
        core.set_eps(tr, tol)
        cs = core.make_colspec(tr, tol)[0]
        cs.label = "c_raw"
        cap = int((budget or 0) * CYCLE_BUDGET_FRAC) or None
        out[0].extra.append(cycles_block(tr, 0, picks, cs, tr.xunit, cap))
        out[0].picks = pick_data(tr, 0, picks)
    return out


def pick_data(tr, si, picks):
    """代表性周期 -> 画得出来的纯数据。

    直接把样点抄出来，而不是存下标：显示用的是**派生** trace（包络），
    下标是**原始** trace 的，两边对不上。几个周期几十个点，抄一份最省心。
    """
    x, y = tr.x, tr.signals[si].y
    out = []
    for c in picks:
        out.append({
            "at": c.at, "why": c.why, "amp": c.amp, "period": c.period,
            "resid": c.resid,
            "t": [x[i] - c.t0 for i in range(c.i0, c.i1)],
            "y": [y[i] for i in range(c.i0, c.i1)],
        })
    return out


def _tq(picks):
    """代表周期的时间刻度：拿中位周期定词头，让 t_rel 是 0..200 这种好读的数。"""
    per = sorted(c.period for c in picks)
    p = per[len(per) // 2] if per else 1.0
    e = core.eng_exp(p)
    return 10.0 ** e, (core.PREFIX.get(e, "") + "s")
