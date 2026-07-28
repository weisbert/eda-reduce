#!/usr/bin/env bash
# 隔离区首次安装（full 包）。最省事的用法就是**就地装**：
#
#     mkdir ~/eda_reduce && cd ~/eda_reduce
#     tar xzf .../eda_reduce_full.tar.gz    # 包是 tarbomb，顶层就是 app/ 等五项
#     bash bootstrap.sh                     # 就装在这儿，app/ 已经就位不用拷
#
# 也可以装到别处：
#
#     bash bootstrap.sh ~/tools/wave        # 传参
#     EDA_REDUCE_PREFIX=~/tools/wave bash bootstrap.sh
#
# 不传参且当前目录不是包目录时，默认装到 ./eda_reduce。
# **不需要 root，不假定任何目录约定**，卸载就是 rm -rf 那一个目录。
#
# **用 `bash xxx.sh` 调，不要 `./xxx.sh`** —— 登录 shell 常是 tcsh，
# 而且上传通道经常把 exec 位掉了。
#
# 装完的目录结构（全在 PREFIX 底下，卸载就是 rm -rf 它）：
#   PREFIX/.venv/            只建一次，之后每次 update 复用
#   PREFIX/wheels/           只放一次，之后每次 update 复用
#   PREFIX/app/              每次 update 整个换掉
#   PREFIX/results/          **永不覆盖**，update 会重新软链回来
#   PREFIX/.backups/         最近 3 份 app 备份，回滚用
#   PREFIX/INSTALL.json      已部署的 MANIFEST，依赖哈希闸拿它做基准
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
# 人站在包目录里不传参，意思显然是「就装这儿」，不是「在这儿再套一层」
if [ -z "${1:-}" ] && [ -z "${EDA_REDUCE_PREFIX:-}" ] \
   && [ "$(pwd -P)" = "$HERE" ]; then
    PREFIX="$HERE"
else
    PREFIX="${1:-${EDA_REDUCE_PREFIX:-$PWD/eda_reduce}}"
fi
mkdir -p "$PREFIX"
PREFIX="$(cd "$PREFIX" && pwd)"          # 转成绝对路径，后面全都不含糊

INPLACE=0
if [ "$PREFIX" = "$HERE" ]; then
    INPLACE=1                            # 包解在哪就装在哪，app/ 已经就位
else
    # 装到包目录**下面**去会自己吃自己：rm -rf $PREFIX/app 会删掉源，
    # cp 就没得拷了。而且外层一删，results/ 跟着没。
    case "$PREFIX" in
        "$HERE"/*)
            echo "错误：安装目录 $PREFIX 在包目录 $HERE 里面（又不等于它）。"
            echo "      要就地装就在包目录里直接 bash bootstrap.sh；"
            echo "      要装别处就给一个包目录外面的路径。"
            exit 1 ;;
    esac
fi

echo "== eda-reduce 首次安装 =="
echo "   包目录 : $HERE"
echo "   安装到 : $PREFIX$([ "$INPLACE" = "1" ] && echo "   （就地）")"

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

if [ "$INPLACE" = "1" ]; then
    echo "[3/5] app/ 就地使用（包解在安装目录里，不用拷）…"
else
    echo "[3/5] 铺 app/ …"
    rm -rf "$PREFIX/app"
    cp -r "$HERE/app" "$PREFIX/app"
fi
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
