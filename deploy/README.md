# deploy —— 三区管道操作手册

代码**下行**，数据**上行**，两条路完全不同：

```
代码：家 dev ──git push──▶ GitHub ──git pull──▶ 黄区 Win ──上传──▶ 隔离区
                                                package.ps1     bootstrap.sh / update.sh

数据：隔离区 ──.wv 纯文本贴进聊天框──▶ 能跑大模型的机器
```

上行只有聊天框，所以 `.wv` 有 20 KB 硬上限，且默认不出 base64 blob——
跨边界前要能用眼睛逐字审。

## 两种包

| | full | incremental |
|---|---|---|
| 内容 | wheels + `requirements.lock` + `app/` + `bootstrap.sh` + `update.sh` | 只有 `app_incoming/` + `update.sh` |
| 体积 | 有几个依赖就多大；**现在依赖为空，几十 KB** | 几十 KB |
| 什么时候用 | 首次安装、**依赖变了** | 日常改代码（绝大多数时候） |
| 隔离区上跑 | 解进一个目录后 `bash bootstrap.sh`（就地装） | 解进安装目录后 `bash update.sh` |

> 现在 `requirements.txt` 是空的（`wave_core` / `wave_emit` / `wave_cli` 硬性纯标准库），
> 所以两种包看起来没差别。**但管道现在就按双包建**——哪天要加 numpy 加速
> `wave_metrics_*`，只是往 `requirements.txt` 加一行、重出一次 full 包，
> 而不是回头重做整条部署链。

### 两个 requirements 文件的分工

| 文件 | 进打包流程吗 | 内容 |
|---|---|---|
| `requirements.txt` | **进**。被审计、被哈希、被冻进 `requirements.lock` | 现在是空的 |
| `requirements-optional.txt` | **不进**。`package.py` 不读它 | Pillow（`plot_digitize` 可选加速） |

往 `requirements.txt` 加一行的代价是：重下轮子 + 过 glibc 审计闸 + **必须走 full 包**。
所以「缺了工具照样跑」的东西不放进去，免得把整条链拖下水，也免得隔离区多背几 MB。

## 黄区：打包

```powershell
.\deploy\package.ps1                       # full
.\deploy\package.ps1 -Mode incremental     # 增量
powershell -ExecutionPolicy Bypass -File deploy\package.ps1    # 执行策略挡住时
```

或者直接：

```bash
python deploy/package.py full
python deploy/package.py incremental
```

产物在 `dist/`：`eda_reduce_*.tar.gz` + 同名 `.sha256` + `MANIFEST.full.json`。

## 四个闸门

一条都别省。每一条都对应一个真踩过的坑。

### 1. glibc 审计闸（`audit_wheels.py`）

隔离区是 CentOS7 那一档，glibc 2.17。黄区是 Windows，`pip download` 会兴高采烈地
抓 `manylinux_2_28` 的轮子，到隔离区就是 `GLIBC_2.28 not found`。

审计在**黄区**就拒掉，并打印该把哪个包降到哪个版本。
不然错误要到隔离区才暴露，来回一趟成本极高。

```bash
python deploy/audit_wheels.py dist/_stage_full/wheels
```

### 2. 依赖哈希闸（`update.sh`）

增量包不带轮子。如果你改了 `requirements.txt` 却只推了增量包，
装上去程序能起来但缺依赖——**这种损坏是静默的**，查起来极贵。

所以：
- `package.py incremental` 比对当前 `requirements.txt` 的哈希和上次 full 记录的，不一致就中止；
- `update.sh` 再比对包里的 `requirements_hash` 和已部署 `INSTALL.json` 里的，不一致就中止。

两头都挡，因为中间隔着人工上传，包可能被搞混。

### 3. git 卫生闸

- `.gitattributes` 里 `* text=auto eol=lf`。**没这条，黄区的 `core.autocrlf`
  会把 `.py`/`.sh` 变成 CRLF，到 Linux 上 shebang 带 `\r`，
  报 `bad interpreter: /usr/bin/env python3^M`。**
- 打包读 **committed blob**（`git archive HEAD`），不读 Windows 工作树。
  所以黄区的 autocrlf 设置污染不了包，未提交的改动也不会莫名其妙混进去。
  工作树脏的时候 `package.ps1` 会大声警告——你以为打进去的改动其实没打进去。
- `VERSION export-subst`：隔离区没有 git，`cat <安装目录>/app/VERSION`
  就能看到 commit 和日期。

### 4. 备份 + 回滚（`update.sh`）

换 `app/` 之前先备份到 `.backups/app-<时间戳>`，留最近 3 份。
中间任何一步失败（包括冒烟测试没过）自动滚回去。

`results/` **永不覆盖**，`.venv/` 和 `wheels/` 只建一次、被每次 update 复用。

## 隔离区：安装与更新

包是 **tarbomb**（顶层直接是 `app/` `bootstrap.sh` `update.sh`
`MANIFEST.json` `requirements.lock` 五项，没有统一外层目录），
所以**解进一个你自己建的目录，然后就地装**是最自然的用法：

```bash
# 首次
mkdir ~/eda_reduce && cd ~/eda_reduce
tar xzf /path/to/eda_reduce_full.tar.gz
bash bootstrap.sh                        # 就装在这儿，app/ 已经就位不用拷
```

**装在哪由你定，脚本不替你决定**，不需要 root，不假定任何目录约定，
卸载就是 `rm -rf` 那一个目录。想装别处：

```bash
bash bootstrap.sh ~/tools/wave           # 传参
export EDA_REDUCE_PREFIX=~/tools/wave    # 或者设成常态，update.sh 也认
```

不传参、又不在包目录里时，默认装到 `./eda_reduce`。

```bash
# 日常更新：解到哪儿都行，包括直接解进安装目录
cd ~/eda_reduce
tar xzf /path/to/eda_reduce_incremental.tar.gz
bash update.sh
```

> 增量包的载荷目录叫 **`app_incoming/`**，不叫 `app/`——所以解包本身**碰不到**
> 已装好的那份，备份仍然备的是旧版，回滚点保得住。
> 更新成功后 `app_incoming/` 会自动收掉。
>
> 这个命名是有代价换来的：如果它叫 `app/`，解进安装目录就会在 `update.sh`
> 跑起来之前把旧版盖掉，于是「备份」备的是新的那份，**回滚点静默丢失**——
> 出事时才发现滚不回去。`tests/test_deploy.py` 里用一个 marker 文件把
> 「备份里到底是新是旧」钉死了。
>
> 也可以解到别处：`bash ~/upd/update.sh ~/eda_reduce`。
> `update.sh` 找安装目录的顺序：`$1` → `$EDA_REDUCE_PREFIX` →
> `./eda_reduce` → 当前目录本身（要有 `INSTALL.json` + `app/`）。
> 都找不到会把找过哪些地方列出来，不猜。

**用 `bash xxx.sh` 调，不要 `./xxx.sh`。** 登录 shell 常是 tcsh，
而且上传通道经常把 exec 位掉了。

装完之后（下面用 `$P` 代指你的安装目录）：

```bash
P=./eda_reduce
$P/wave my.csv -o my.wv
$P/wave my.csv --gui                     # 要有 tkinter + X 显示
$P/wave --gui                            # 不给文件也能开，窗口里有「打开 CSV…」
$P/wave my.csv --gui --selftest          # 探针：打印状态后自动退出，不会挂住
cat $P/app/VERSION                       # 看装的是哪个 commit
```

GUI 里调好参数后点 **「复制全文到剪贴板」** 直接粘进聊天框。
X11 的剪贴板归复制它的进程持有，**先粘贴再关窗口**；
怕丢就切到「完整 .wv」视图手动全选，或者 `另存 .wv…`。

开不起来先跑 `--gui --selftest`：报 `no display name and no $DISPLAY`
是 X 没接上（SSH 要 `ssh -X`），不是工具的问题。

### tkinter

**tkinter 装不了 wheel**（它是 CPython 自带的 C 扩展 + Tcl/Tk 运行时），
写进 `requirements.txt` 没用。没有的话只能让管理员上系统包
（`python3-tk` / `tkinter`）。`bootstrap.sh` 会探一下并告诉你有没有。

命令行不依赖它，GUI 才依赖。2026-07-27 在隔离区实测确认可用，
**换机器要重新确认**。

## 送出去之前：本地彩排

```bash
bash deploy/dryrun_manylinux2014.sh dist/eda_reduce_full.tar.gz
```

在 manylinux2014 镜像里 **`--network none` 断网**装一遍，顺带查 CRLF。

审计闸只看轮子的**标签**，标签对不代表真装得上（还有 ABI、掉的 exec 位、CRLF）。
断网彩排是唯一能在送进隔离区之前证明「离线装得上」的办法。
**没彩排过的包不要往隔离区送。**

没有 docker 的机器上脚本会直接说跳过并 exit 2，不会假装通过。

## 回滚

```bash
P=./eda_reduce                # 换成你的安装目录
ls -1dt $P/.backups/app-*     # 最近 3 份
rm -rf $P/app
cp -r $P/.backups/app-<戳> $P/app
ln -sfn $P/results $P/app/results
```

`update.sh` 中途失败会自动做这件事，这里是手动兜底。

## 出错速查

| 症状 | 原因 | 怎么办 |
|---|---|---|
| `bad interpreter: ...^M` | 包里是 CRLF | `.gitattributes` 的 `eol=lf` 没生效，或打包读了工作树。重打包，跑一遍断网彩排 |
| `Permission denied` 跑 `.sh` | 上传通道掉了 exec 位 | 用 `bash xxx.sh` 调 |
| `update.sh` 报「依赖变过了」 | 改了 `requirements.txt` 只推了增量包 | 出 full 包重新 bootstrap |
| `GLIBC_2.xx not found` | 轮子太新 | 黄区跑 `audit_wheels.py` 看该降到哪个版本；正常情况打包时就该被拦下 |
| `--gui` 说没有 tkinter | 系统缺 Tcl/Tk | 装不了 wheel，找管理员上系统包；命令行照常可用 |
| `venv` 建不起来 | 缺 `python3-venv` | 核心是纯标准库，直接用系统 python3 跑 `app/tools/wave_reduce.py` |
