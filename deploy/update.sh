#!/usr/bin/env bash
# 隔离区增量更新（incremental 包）：只换 app/ 源码，复用 .venv + wheels。
#
#     bash update.sh [PREFIX]             # 默认 /opt/eda_reduce
#
# **用 `bash update.sh` 调，不要 `./update.sh`**（登录 shell 常是 tcsh，
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
PREFIX="${1:-/opt/eda_reduce}"
KEEP=3

echo "== eda-reduce 增量更新 =="
[ -f "$HERE/MANIFEST.json" ] || { echo "错误：缺 MANIFEST.json"; exit 1; }
[ -d "$PREFIX/app" ] || { echo "错误：$PREFIX 没装过 —— 先用 full 包跑 bootstrap.sh"; exit 1; }

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

echo "[1/4] 备份当前 app/ -> $BK"
cp -r "$PREFIX/app" "$BK"

echo "[2/4] 换 app/（.venv / wheels / results 都不动）…"
rm -rf "$PREFIX/app"
cp -r "$HERE/app" "$PREFIX/app"
mkdir -p "$PREFIX/results"
ln -sfn "$PREFIX/results" "$PREFIX/app/results"

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
ls -1dt "$PREFIX"/.backups/app-* 2>/dev/null | tail -n +$((KEEP + 1)) \
    | while read -r d; do rm -rf "$d"; done

echo
echo "更新完成。app/ 已到 build ${bld:0:9}，venv 没动。"
echo "版本：$(cat "$PREFIX/app/VERSION" 2>/dev/null || echo unknown)"
