#!/usr/bin/env bash
# 本地彩排：在 manylinux2014（glibc 2.17）镜像里**断网**装一遍 full 包。
#
#     bash deploy/dryrun_manylinux2014.sh dist/eda_reduce_full.tar.gz
#
# 为什么要这一步：黄区的 glibc 审计闸只看轮子的**标签**，标签对不代表真装得上
# （还有 ABI、还有传输过程中掉的 exec 位、还有 CRLF）。在真镜像里断网装一遍，
# 是唯一能在送进隔离区之前证明「离线装得上」的办法。一次来回的成本极高。
#
# --network none 是关键：不断网的话 pip 会偷偷从 PyPI 补包，
# 彩排就变成了假阳性。
set -eu

TAR="${1:-dist/eda_reduce_full.tar.gz}"
IMG="${IMG:-quay.io/pypa/manylinux2014_x86_64}"

[ -f "$TAR" ] || { echo "找不到包：$TAR（先跑 python deploy/package.py full）"; exit 1; }
command -v docker >/dev/null 2>&1 || {
    echo "这台机器没有 docker，跳过彩排。"
    echo "**没彩排过的包不要往隔离区送** —— 找一台有 docker 的机器跑这个脚本。"
    exit 2; }

ABS="$(cd "$(dirname "$TAR")" && pwd)/$(basename "$TAR")"
echo "== 断网彩排 =="
echo "   镜像 : $IMG"
echo "   包   : $ABS"

docker run --rm --network none -v "$ABS:/in/pkg.tar.gz:ro" "$IMG" bash -euxc '
    mkdir -p /opt/x && cd /opt/x && tar xzf /in/pkg.tar.gz

    # CRLF 闸：黄区是 Windows，.sh 带 \r 到这里就是 bad interpreter
    if grep -rlU $"\r" . >/dev/null 2>&1; then
        echo "*** 包里有 CRLF 文件 —— .gitattributes 的 eol=lf 没生效，"
        echo "    或者打包读了工作树而不是 committed blob ***"
        grep -rlU $"\r" . | head
        exit 1
    fi

    bash bootstrap.sh                         # 就地装（最常见的用法）
    ./wave app/examples/demo_ac.csv -o /tmp/a.wv
    head -3 /tmp/a.wv
    echo "断网离线安装 + 端到端 OK"
'
echo
echo "彩排通过 —— 这个包可以往隔离区送了。"
