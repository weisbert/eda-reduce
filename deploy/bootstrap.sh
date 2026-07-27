#!/usr/bin/env bash
# 隔离区首次安装（full 包）。在解开的包目录里跑：
#
#     bash bootstrap.sh [PREFIX]          # 默认 /opt/eda_reduce
#
# **用 `bash bootstrap.sh` 调，不要 `./bootstrap.sh`** —— 登录 shell 常是 tcsh，
# 而且上传通道经常把 exec 位掉了。
#
# 装完的目录结构：
#   PREFIX/.venv/            只建一次，之后每次 update 复用
#   PREFIX/wheels/           只放一次，之后每次 update 复用
#   PREFIX/app/              每次 update 整个换掉
#   PREFIX/results/          **永不覆盖**，update 会重新软链回来
#   PREFIX/.backups/         最近 3 份 app 备份，回滚用
#   PREFIX/INSTALL.json      已部署的 MANIFEST，依赖哈希闸拿它做基准
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${1:-/opt/eda_reduce}"

echo "== eda-reduce 首次安装 =="
echo "   包目录 : $HERE"
echo "   安装到 : $PREFIX"

[ -f "$HERE/MANIFEST.json" ] || { echo "错误：这不像一个 full 包（缺 MANIFEST.json）"; exit 1; }
mode="$(grep -o '"mode"[^,]*' "$HERE/MANIFEST.json" | sed 's/.*: *"//; s/"//')"
[ "$mode" = "full" ] || { echo "错误：这是 $mode 包，首次安装要用 full 包"; exit 1; }

# --- 找 python3。隔离区通常只有系统 python3，别去猜别的
PY=""
for c in python3.11 python3.10 python3.9 python3.8 python3.7 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done
[ -n "$PY" ] || { echo "错误：找不到 python3"; exit 1; }
echo "   python : $PY  ($("$PY" -V 2>&1))"

# tkinter 不能靠 wheel 补装，只能靠系统包。装不上不致命 —— 命令行照常可用
if "$PY" -c 'import tkinter' >/dev/null 2>&1; then
    echo "   tkinter: 有（--gui 可用）"
else
    echo "   tkinter: 没有 —— GUI 用不了，命令行照常。"
    echo "            tkinter 装不了 wheel，只能让管理员上系统包（python3-tk / tkinter）。"
fi

mkdir -p "$PREFIX"

# --- venv + wheels 只建一次
if [ -d "$PREFIX/.venv" ]; then
    echo "[1/5] 已有 .venv，复用（不重建）"
else
    echo "[1/5] 建 .venv …"
    "$PY" -m venv "$PREFIX/.venv" || {
        echo "      venv 建不起来（有些发行版要 python3-venv）；"
        echo "      wave_reduce 核心是纯标准库，可以直接用系统 python3 跑，"
        echo "      只是没法装第三方依赖。继续。"; }
fi

echo "[2/5] 放 wheels/ …"
mkdir -p "$PREFIX/wheels"
if ls "$HERE"/wheels/*.whl >/dev/null 2>&1; then
    cp -n "$HERE"/wheels/*.whl "$PREFIX/wheels/" || true
    if [ -x "$PREFIX/.venv/bin/pip" ]; then
        # --no-index：**绝不许联网**。隔离区没有网，联网重试只会挂很久然后失败
        "$PREFIX/.venv/bin/pip" install --no-index \
            --find-links "$PREFIX/wheels" -r "$HERE/requirements.lock"
    fi
else
    echo "      包里没有轮子（依赖为空）—— 纯标准库，跳过 pip"
fi

echo "[3/5] 铺 app/ …"
rm -rf "$PREFIX/app"
cp -r "$HERE/app" "$PREFIX/app"
mkdir -p "$PREFIX/results" "$PREFIX/.backups"
ln -sfn "$PREFIX/results" "$PREFIX/app/results"

cat > "$PREFIX/wave" <<'EOF'
#!/usr/bin/env bash
# wave_reduce 启动器。优先用 venv 的 python，没有就用系统的（核心纯标准库）。
P="$(cd "$(dirname "$0")" && pwd)"
PY="$P/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" "$P/app/tools/wave_reduce.py" "$@"
EOF
chmod +x "$PREFIX/wave" 2>/dev/null || true

echo "[4/5] 冒烟测试 …"
PY_RUN="$PREFIX/.venv/bin/python"; [ -x "$PY_RUN" ] || PY_RUN="$PY"
"$PY_RUN" "$PREFIX/app/tools/wave_reduce.py" --list-kinds
"$PY_RUN" "$PREFIX/app/tools/wave_reduce.py" \
    "$PREFIX/app/examples/demo_tran.csv" -o "$PREFIX/.smoke.wv" >/dev/null
head -2 "$PREFIX/.smoke.wv"
rm -f "$PREFIX/.smoke.wv"

echo "[5/5] 记录 INSTALL.json …"
cp "$HERE/MANIFEST.json" "$PREFIX/INSTALL.json"
echo
echo "装好了。版本：$(cat "$PREFIX/app/VERSION" 2>/dev/null || echo unknown)"
echo "用法： $PREFIX/wave my.csv -o my.wv"
echo "      $PREFIX/wave my.csv --gui        # 有 tkinter 才行"
echo "以后改代码只推增量包，跑 bash update.sh $PREFIX"
