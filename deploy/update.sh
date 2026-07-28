#!/usr/bin/env bash
# 隔离区增量更新（incremental 包）：只换 app/ 源码，复用 .venv + wheels。
#
#     cd ~/eda_reduce && tar xzf eda_reduce_incremental.tar.gz && bash update.sh
#
# **解到哪儿都行，包括直接解进安装目录。** 增量包的载荷目录叫 app_incoming/，
# 不叫 app/，所以解包本身不会碰到已装好的那份 —— 备份还是备的旧版，
# 回滚点保得住。
#
# 找安装目录的顺序：$1 > $EDA_REDUCE_PREFIX > ./eda_reduce > 当前目录本身
# （要有 INSTALL.json + app/）。都找不到会把找过哪些地方列出来，不猜。
#
# **用 `bash xxx.sh` 调，不要 `./xxx.sh`**（登录 shell 常是 tcsh，
# 上传通道可能掉 exec 位）。
#
# 两道闸：
#   依赖哈希闸  包里的 requirements_hash 和已部署的对不上就**中止**，要求走 full。
#               防「改了依赖却只推增量包」——那种损坏是静默的，
#               程序能起来但行为不对，查起来极贵。
#   备份回滚    换之前备份 app/（留最近 3 份），任何一步失败自动滚回去。
#
# results/ 永不覆盖。.venv/ 和 wheels/ 不碰。
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
KEEP=3

echo "== eda-reduce 增量更新 =="
[ -f "$HERE/MANIFEST.json" ] || { echo "错误：缺 MANIFEST.json"; exit 1; }

# 找安装目录：显式参数 > 环境变量 > ./eda_reduce > 当前目录本身
PREFIX="${1:-${EDA_REDUCE_PREFIX:-}}"
if [ -z "$PREFIX" ]; then
    if [ -f "$PWD/eda_reduce/INSTALL.json" ]; then
        PREFIX="$PWD/eda_reduce"
    elif [ -f "$PWD/INSTALL.json" ] && [ -d "$PWD/app" ]; then
        PREFIX="$PWD"                    # 人就站在装好的那个目录里
    else
        PREFIX="$PWD/eda_reduce"
    fi
fi
if [ ! -d "$PREFIX/app" ]; then
    echo "错误：$PREFIX 下没有已安装的 app/。"
    echo "      找过：\$1=${1:-（没给）}  \$EDA_REDUCE_PREFIX=${EDA_REDUCE_PREFIX:-（没设）}"
    echo "            \$PWD/eda_reduce=$PWD/eda_reduce"
    echo "            \$PWD=$PWD（要有 INSTALL.json + app/ 才算）"
    echo "      装在别处就把路径传进来：bash $0 <你的安装目录>"
    echo "      从没装过就先用 full 包跑 bootstrap.sh"
    exit 1
fi
PREFIX="$(cd "$PREFIX" && pwd)"

# 载荷目录。新包是 app_incoming/（不会和已装好的 app/ 撞名，所以解进
# 安装目录也安全）；老包是 app/，兼容一下。
SRC="$HERE/app_incoming"
if [ ! -d "$SRC" ]; then
    SRC="$HERE/app"
    if [ "$SRC" = "$PREFIX/app" ]; then
        echo "错误：这是个老格式的增量包（载荷目录叫 app/），又解在了安装目录里。"
        echo "      app/ 已经被盖掉了，没法再备份出旧版。"
        echo "      要么用新版打的包（载荷目录是 app_incoming/），"
        echo "      要么把这个包解到别处再跑：bash <包>/update.sh \"$PREFIX\""
        echo "      已经被盖的话，从 $PREFIX/.backups/ 里挑一份滚回去。"
        exit 1
    fi
fi
[ -d "$SRC" ] || { echo "错误：包里找不到载荷目录（app_incoming/ 或 app/）"; exit 1; }

_get() { grep -o "\"$2\"[^,]*" "$1" | head -1 | sed 's/.*: *"//; s/"//'; }

new_hash="$(_get "$HERE/MANIFEST.json" requirements_hash)"
dep_hash="$(_get "$PREFIX/INSTALL.json" requirements_hash 2>/dev/null || true)"
bld="$(_get "$HERE/MANIFEST.json" git_sha)"
bdt="$(_get "$HERE/MANIFEST.json" built_utc)"
dty="$(_get "$HERE/MANIFEST.json" worktree_dirty || true)"

echo "   包的 build     : ${bld:0:9}  ($bdt)   <-- 核一下是不是你要的那个"
echo "   包的 req-hash  : ${new_hash:0:12}"
echo "   已部署 req-hash: ${dep_hash:0:12}"
if [ "${dty:-false}" = "true" ]; then
    echo "   !! 打包时黄区工作树是脏的，包里是 HEAD 的内容"
fi
if [ "$new_hash" != "$dep_hash" ]; then
    echo "中止：依赖自已部署的 venv 建好之后变过了。"
    echo "      增量包不带轮子，装上去会缺依赖。请走 full 包 + bootstrap.sh。"
    exit 2
fi

STAMP="$(date +%Y%m%dT%H%M%S 2>/dev/null || echo manual)"
BK="$PREFIX/.backups/app-$STAMP"
mkdir -p "$PREFIX/.backups"

rollback() {
    echo "!! 出错了，正在回滚 …"
    rm -rf "$PREFIX/app"
    if [ -d "$BK" ]; then
        cp -r "$BK" "$PREFIX/app"
        ln -sfn "$PREFIX/results" "$PREFIX/app/results" 2>/dev/null || true
        echo "   已滚回 $BK"
    else
        echo "   没有备份可滚（备份步骤本身就失败了）"
    fi
    exit 3
}
trap rollback ERR

# 更新前后各记一次 commit：光印新版本没用，人要看的是「有没有变」。
# 真实困惑：更新完之后「红区的 python 看起来还像是老的」，而当时没有任何
# 一条命令能回答这个问题 —— 只能靠输出里有没有某条新 note 去推断。
OLDV="$(sed -n 's/^commit  *//p' "$PREFIX/app/VERSION" 2>/dev/null | cut -c1-9)"
[ -n "$OLDV" ] || OLDV="unknown"

echo "[1/4] 备份当前 app/ -> $BK"
cp -r "$PREFIX/app" "$BK"

echo "[2/4] 换 app/（.venv / wheels / results 都不动）…"
rm -rf "$PREFIX/app"
cp -r "$SRC" "$PREFIX/app"          # 用 cp 不用 mv：失败了还能重来
mkdir -p "$PREFIX/results"
ln -sfn "$PREFIX/results" "$PREFIX/app/results"
# 启动器每次一起刷新，否则它自己的修复永远传不到已部署的机器上
for L in update; do
    [ -f "$PREFIX/app/deploy/$L" ] && {
        cp "$PREFIX/app/deploy/$L" "$PREFIX/$L"
        chmod +x "$PREFIX/$L" 2>/dev/null || true; }
done

echo "[3/4] 冒烟测试 …"
PY="$PREFIX/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
"$PY" "$PREFIX/app/tools/wave_reduce.py" --list-kinds
"$PY" "$PREFIX/app/tools/wave_reduce.py" \
    "$PREFIX/app/examples/demo_tran.csv" -o "$PREFIX/.smoke.wv" >/dev/null
head -2 "$PREFIX/.smoke.wv"
rm -f "$PREFIX/.smoke.wv"

trap - ERR
echo "[4/4] 记录 + 清理旧备份（留最近 $KEEP 份）…"
cp "$HERE/MANIFEST.json" "$PREFIX/INSTALL.json"
# 解在安装目录里的话，成功之后把载荷目录收掉，别留一份重复的源码占地方
if [ "$SRC" = "$PREFIX/app_incoming" ]; then
    rm -rf "$SRC"
fi
ls -1dt "$PREFIX"/.backups/app-* 2>/dev/null | tail -n +$((KEEP + 1)) \
    | while read -r d; do rm -rf "$d"; done

echo
NEWV="$(sed -n 's/^commit  *//p' "$PREFIX/app/VERSION" 2>/dev/null | cut -c1-9)"
[ -n "$NEWV" ] || NEWV="unknown"
echo "更新完成。venv 没动。"
echo "版本：$OLDV  ->  $NEWV"
if [ "$OLDV" = "$NEWV" ]; then
    echo
    echo "!! 注意：版本没变。你八成传的还是**上一次那个包** ——"
    echo "   包名每次都叫 eda_reduce_incremental.tar.gz，看不出新旧。"
    echo "   回黄区重新打一个再传，或者核对 sha256。"
fi
echo "随时可以自己查：$PREFIX/app/tools/wave_reduce.py --version"
