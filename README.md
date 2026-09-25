# blastdb_download.py

**一致、可校验、可续跑的 BLAST 预格式化数据库镜像工具。**

**中文** 

`update_blastdb.pl` 的现代化替代品：单文件、纯 Python 标准库、可选 aria2c 并行加速，
并且**首要目标是保证下载下来的每个数据库都是一套"同一次构建"的完整索引**，
而不是新旧混装的残次品——后者不会报错，只会让比对结果悄悄出错。

```
blastdb_download.py        # 主程序（单文件，无第三方依赖）
docs/FAQ.md                # 常见问题（症状 → 原因 → 处理）
docs/DESIGN.md             # 设计说明：协议、状态文件、不变量、扩展点
docs/OPERATIONS.md         # 运维手册：容量估算、监控、排错、迁移
docs/NCBI_database_download.md  # 背景：原始笔记 + 实测数据 + 检查方法
```

### 文档索引

| 想知道什么 | 看哪份 |
|---|---|
| 常见问题的快速答案（为什么报错、怎么继续、要不要装某工具） | [docs/FAQ.md](docs/FAQ.md) |
| 容量/时间估算、cron/systemd、监控、失败处置、从旧流程迁移 | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| 内部协议（`revkey`、校验矩阵、状态文件格式、不变量、怎么加数据源） | [docs/DESIGN.md](docs/DESIGN.md) |
| `update_blastdb.pl` 的笔记、实测事实、`blastdbcheck`/`blastdbcmd` 检查方法 | [docs/NCBI_database_download.md](docs/NCBI_database_download.md) |

---

## 目录

- [特性](#特性)
- [快速开始](#快速开始)
- [常用场景](#常用场景)
- [数据源怎么选（实测对比）](#数据源怎么选实测对比)
- [最稳健的下载流程（推荐照抄）](#最稳健的下载流程推荐照抄)
- [工作原理](#工作原理)
- [命令参考](#命令参考)
- [参数参考](#参数参考)
- [配置文件](#配置文件)
- [注意事项与排错](#注意事项与排错)
- [校验已有镜像](#校验已有镜像)
- [任务中断后续跑](#任务中断后续跑)
- [与 update_blastdb.pl 对照](#与-update_blastdbpl-对照)
- [第三方软件依赖（哪些必须装）](#第三方软件依赖哪些必须装)
- [测试](#测试)
- [更新日志](#更新日志)
- [协议与出处](#协议与出处)
- [附录：为什么必须这样做](#附录为什么必须这样做)

---

## 特性

| | |
|---|---|
| **集合级一致性** | 以**整个数据库（全部卷）**为一致性单位，而不是单文件。下载前、下载后、安装前各校验一次，绝不安装混装集合 |
| **不依赖元数据的内部判据** | 直接读每个卷 `.nin`/`.pin` 头部内嵌的**构建时间戳与卷序号**，并与 `<db>.njs`、`<db>-nucl-metadata.json` 交叉校验。不信任任何一方单独的说法 |
| **云端快照只认权威指针** | 云端只用 `latest-dir` 指向的目录，并校验其 manifest 存在、清单中每个文件都在快照里（实测桶里确实存在"目录名更新但 manifest 仍 404"的半成品） |
| **逐文件权威校验** | NCBI 用 `.md5` 边车，GCS 用对象 `md5Hash`；**S3 分片 ETag 不作为 md5**（如实降级并在状态文件中标注） |
| **原子落地** | 新快照目录 + **原子替换 `current` 软链接**，读端永不见半更新；回滚是一次软链接切换；多快照并存、硬链接复用几乎不额外占盘 |
| **增量更新** | 逐文件比对 md5，未变文件直接硬链接，`nt`/`nr` 只下载真正变动的卷 |
| **三层断点续跑** | 分片状态 + staging 复用 + 快照硬链接；**staging 按修订指纹隔离**，旧断点不可能污染新版本 |
| **并行下载** | aria2c 存在则用（含 `checksum=md5=` 下载内校验）；不存在则用内置分片并发下载器（Range + `pwrite`，可续传） |
| **可离线复核** | 状态文件记录每个文件被证明过的 md5、证明来源、来源归档、头尾内容指纹；`verify` 无需联网 |
| **可检查别人的镜像** | `inspect` 直接对**任意**目录（包括 `update_blastdb.pl` 或手写 aria2c 循环下载的目录）报告构建指纹，不需要状态文件、不需要 BLAST |
| **并发保护** | 同一镜像根目录的写操作加文件锁，另一个进程会明确报错而不是互相破坏 |
| **日志系统** | 所有诊断在 stderr、数据在 stdout（`> log` 不会吞日志）；`--log-file` 额外写带时间戳的完整日志，`-q` 只静音控制台不影响日志文件 |
| **进度反馈** | 百分比 / 已传字节 / 速度 / ETA / 文件计数；**无终端**（cron、队列、`tail -f`）时按 `--progress-interval` 周期性输出 |
| **分级重试** | 单文件失败按**原因**处理：超时/连接重置/403 限流/5xx → 自动降并发重试（`--file-retries`）；无空间/配额/权限/404 → 立刻停并说明，不做无谓重试 |
| **续跑与清理** | 三层续跑（分片状态 / staging 复用 / 解压回执），并在检出"不配套"的旧 staging 时**保留可用文件、删除不配套的** |
| **环境自检** | `doctor` 报告可用空间、**用户配额**（`quota -uvs`、`lfs quota`、`xfs_quota`）与可选外部工具（`aria2c`/`gsutil`/`aws`/`blastdbcmd`…）及安装方式；空间检查取"df 与配额中较小者" |
| **机器可读** | `--json` 输出结构化结果，便于接入流水线 |

---

## 快速开始

需要 Python 3.9+（推荐 3.11+ 以支持 TOML 配置），aria2c 可选。

```bash
# 1) 看源上有什么（名字 / 表格 / 人类可读）
python3 blastdb_download.py showall --format pretty

# 2) 下载（默认 GCP 不可变快照源；云端会自动附带 taxdb）
python3 blastdb_download.py -r /data/blastdb -j 8 -x 4 download nt core_nt

# 3) 指给 BLAST 用（current 是原子切换的软链接）
export BLASTDB=/data/blastdb/current
blastn -db nt -query q.fa -out out.txt
```

先做一次环境自检（空间、配额、可选工具，一条命令看清为什么可能失败）：

```bash
python3 blastdb_download.py doctor
python3 blastdb_download.py doctor --json | jq '{filesystem, quota, effective_free}'
```

无人值守的长任务建议加日志文件（`-q` 只静音控制台，日志文件仍完整）：

```bash
python3 blastdb_download.py -r /data/blastdb -s ncbi -j 8 \
        --log-file /var/log/blastdb.log --progress-interval 30 \
        download nt
tail -f /var/log/blastdb.log     # 另开一个终端跟踪进度
```

想看要下什么、占多少空间而不真的下载：加 `--dry-run`。

```bash
python3 blastdb_download.py -r /data/blastdb --dry-run download nt
```

要最新的数据（而不是滞后约一个月的云端快照）：加 `-s ncbi`。

```bash
python3 blastdb_download.py -r /data/blastdb -s ncbi -j 8 download nt
```

> 全局参数放在子命令**前或后都行**：`download nt --jobs 8` 与 `--jobs 8 download nt` 等价。

---

## 常用场景

**日常增量更新（cron）**

```bash
30 3 * * *  blast  /usr/bin/python3 /opt/blastdb_download.py \
  -r /data/blastdb -s ncbi -j 8 --keep-snapshots 2 \
  download nt core_nt taxdb >> /var/log/blastdb.log 2>&1
```

**用区域/内网镜像，避免直连 NCBI**

```bash
python3 blastdb_download.py -r /data/blastdb -s ncbi \
    --ncbi-base https://mirror.example.org --ncbi-dir /blast/db download nt
```

**限速 / 只用内置下载器 / 指定 aria2c**

```bash
python3 blastdb_download.py -r /data/blastdb --limit-rate 50M download nt
python3 blastdb_download.py -r /data/blastdb --aria2c none download nt
python3 blastdb_download.py -r /data/blastdb --aria2c /usr/local/bin/aria2c download nt
```

**收紧或放宽"复用已装文件"的校验强度**

```bash
# 默认 probe：大小 + 头尾各 64 KiB 内容指纹（毫秒级，能发现截断/首尾损坏）
python3 blastdb_download.py -r /data/blastdb --reuse-verify probe download nt
# 全量 md5：最严，代价是每次都要把整棵树哈希一遍
python3 blastdb_download.py -r /data/blastdb --reuse-verify md5 download nt
```

**下载途中官网恰好在发新版**

```bash
# 默认：丢弃本次 staging，等待后重试（最多 --torn-retries 次）
python3 blastdb_download.py -r /data/blastdb -s ncbi download nt
# 宁可失败也不等（退出码 4，且不会留下任何快照）
python3 blastdb_download.py -r /data/blastdb -s ncbi --on-torn fail download nt
# 换成不可变的云端快照
python3 blastdb_download.py -r /data/blastdb -s gcp download nt
```

**忽略已装版本，强制重下一遍**

```bash
python3 blastdb_download.py -r /data/blastdb download --force nt
```

**复核 / 定向修复 / 回滚**

```bash
python3 blastdb_download.py -r /data/blastdb verify
python3 blastdb_download.py -r /data/blastdb verify --quick            # 只查头尾指纹
python3 blastdb_download.py -r /data/blastdb verify --against-remote   # 顺便看上游有没有新版
python3 blastdb_download.py -r /data/blastdb repair nt                 # 只重下坏文件
python3 blastdb_download.py -r /data/blastdb list
python3 blastdb_download.py -r /data/blastdb rollback --list
python3 blastdb_download.py -r /data/blastdb rollback --to 1
python3 blastdb_download.py -r /data/blastdb gc --keep 2
```

**关于 `taxdb`（物种名 `%S` 显示 `N/A` 就是缺它）**

三个源都能取到 taxonomy，**不需要为了它专门指定 `-s ncbi`**：

```bash
# 默认（GCP）也会自动带上 taxdb，无需手写
python3 blastdb_download.py -r /data/blastdb download nt

# 显式写出来也可以（幂等，不会重复下载）
python3 blastdb_download.py -r /data/blastdb download nt taxdb

# NCBI 源同样默认自动附加（约 62 MiB 压缩），不需要就关掉
python3 blastdb_download.py -r /data/blastdb -s ncbi download nt
python3 blastdb_download.py -r /data/blastdb -s ncbi --no-taxdb download nt
```

| 源 | taxonomy 从哪来 |
|---|---|
| GCP / AWS | `taxdb` 是清单里的一等库（3 个裸对象：`taxdb.btd`、`taxdb.bti`、`taxonomy4blast.sqlite3`，带 `md5Hash` 可逐个校验）；工具默认附加 |
| NCBI | 单独发布 `taxdb.tar.gz`（实测 62.34 MiB 压缩 / 293.9 MiB 解压，2026-09-16 更新）；**实测单卷库的归档里也自带同一份载荷**，多卷库是否也自带没有官方说明——因此默认仍然单独下载一份（代价 0.006% of nt），重复副本在安装时按确定性规则消解 |

**接续"以前用 aria2c 循环下到一半"的任务**

```bash
# 先看现场（旧脚本的产物在这里，不在本工具的 staging 里）
du -sh /data/public/databases/NT
python3 blastdb_download.py -r /data/public/databases/NT/other doctor

# 旧目录里还有完整归档 / 没下完的卷 -> 直接接管，不重下
python3 blastdb_download.py -r /data/public/databases/NT/other \
        --adopt /data/public/databases/NT --adopt-partial \
        -s ncbi -j 8 --log-file /var/log/nt.log download nt

# 归档已被 rm、只剩解压出来的索引 -> 整套完整时可接管
python3 blastdb_download.py -r /data/public/databases/NT/other \
        --adopt /data/public/databases/NT --adopt-extracted \
        -s ncbi -j 8 download nt
```

先 `--dry-run` 看还剩多少要下（不落盘）：

```bash
python3 blastdb_download.py -r /data/public/databases/NT/other \
        --adopt /data/public/databases/NT --adopt-partial --dry-run download nt
```

**生成一份配置文件模板**

```bash
python3 blastdb_download.py -r /data/blastdb config --init
python3 blastdb_download.py config --template          # 只打印，不写盘
```

---

## 数据源怎么选（实测对比）

三个源都能拿到同一批库（含 `taxdb`），差别在**新鲜度**、**校验强度**和**空间/CPU 开销**。
下面数字都是 2026-09-24 现场核对的。

| | `-s ncbi` | `-s gcp`（默认） | `-s aws` |
|---|---|---|---|
| **数据新鲜度** | **最新**：`nt` 发布于 2026-09-15，目录内文件更新到 09-22 | 快照约每月一次：`latest-dir` = `2026-07-21-01-05-02`（滞后约 1–2 个月） | 与 GCP 同一个快照（`2026-07-21-01-05-02`） |
| **目录性质** | **原地可变**：发布期间逐个替换文件 | 不可变快照目录 + `latest-dir` 指针 | 同 GCP |
| **布局** | `*.tar.gz` + 每个文件一个 `.md5` 边车 | 裸索引文件（**不需要解压**） | 同 GCP |
| **逐文件权威校验** | ✓ `.md5` 边车（真 md5） | ✓ 对象 `md5Hash`（真 md5） | ⚠ 大对象 ETag 是**分片哈希**，不是 md5 → 退化为"大小 + 内嵌构建指纹 + 集合校验"，`verify` 会如实标注 |
| **需下载字节（`nt`）** | ≈ 932 GiB（压缩归档） | ≈ 1.0 TiB（裸文件，略多） | 同 GCP |
| **峰值磁盘（`nt`）** | 载荷 1.07 TiB **+ 一个归档**（0.73–4.78 GiB）；`--keep-archives` 时约 1.9 TiB | 载荷本身（≈1.0 TiB），无解压开销 | 同 GCP |
| **CPU/时间开销** | 需解压 383 个归档 | **无** | 无 |
| **主要风险** | 发布窗口内可能遇到前后不一致（工具会重试或中止，**绝不装混装**） | 数据滞后；**快照只保留约 3 个（≈1 个月）** | 校验强度低；快照保留期同样约 1 个月 |
| **适用** | 要最新数据 | **生产默认**：要最强确定性、省空间、省 CPU | 有 AWS 内网/凭证优势，且能接受校验降级 |

### 下载引擎（与数据源无关）

用不用 aria2c **只取决于 PATH 上有没有它**，三个源走同一套逻辑：

```bash
--aria2c auto      # 默认：shutil.which("aria2c") 找到就用，找不到就用内置下载器
--aria2c none      # 强制内置下载器
--aria2c /path/to/aria2c   # 指定二进制（路径无效会立刻报错，不会等到联网之后）
```

`doctor` 会直接告诉你本机探测结果（`aria2c  /usr/bin/aria2c (aria2 version 1.37.0)` 或 `not installed`）。

用 aria2c 时（以 GCP 源为例）的具体行为：

- 每个文件在 aria2 的输入文件里都带 **`checksum=md5=<GCS 对象 md5Hash>`** → **aria2 在下载内就校验官方 md5**，我下完后再独立校验一次（双保险）；
- 按 `-x/--connections` 与 `-k/--min-split-size` 分片多连接（GCP 支持 Range；小于 64 MiB 的文件不切）；
- `.aria2` 控制文件落在**按修订指纹隔离**的 staging 目录里，所以断点续传不可能串到别的版本；
- aria2 校验失败会把坏文件留在磁盘上，工具会**删除**它（否则下次续传会把坏数据带进快照）；
- 进度：终端下用 aria2 自己的 readout；无终端（cron/日志）时由后台监控线程按 staging 体积上报。

**没有 aria2c 也不损失正确性**：内置下载器同样支持分片并发（Range）、按偏移写入、断点续传与 md5 校验，
差别主要在吞吐细节与进度显示。要强制内置：`--aria2c none`。

**怎么选**

- **只想"稳"** → 用默认 `-s gcp`：不可变快照 + 逐文件真 md5 + 免解压，是三条路里最确定的。
- **要"新"** → `-s ncbi`：工具的一致性协议本来就是为这个"可变目录"设计的（下载前后各读一次修订指纹、
  校验内嵌构建时间戳），代价是撞上发布窗口时可能重试或中止，等一会儿再跑即可。
- **`-s aws`** → 只在 AWS 通道明显更快或有凭证要求时用；注意它对大文件没有 md5 可校验。
- **两者兼得** → 先用 `-s gcp` 建一份**不可变基线**（马上可用、可全量校验），之后再按需 `-s ncbi` 追新；
  两次运行的未变动文件会**硬链接复用**，不会重复占盘。
- **不要在同一个 `--root` 里混着用不同源**去装同一个库：技术上可行（`current` 会把未变动的库带过来），
  但跨源的 `taxdb` 与归档自带的 taxonomy 可能来自不同构建，工具会按"构建时间取新"决定并告警。

## 最稳健的下载流程（推荐照抄）

```bash
ROOT=/data/public/databases
LOG=/var/log/blastdb
mkdir -p "$LOG"

# 0) 环境自检：可用空间、用户配额、可选外部工具（含 quota 是否安装）
blastdb_download.py -r "$ROOT" doctor

# 1) 空跑：打印计划、每个库的磁盘估算、可用空间、自动附加的 taxdb（不落盘）
blastdb_download.py -r "$ROOT" -s gcp --dry-run download nt taxdb

# 2) 正式下载：不可变快照源 + 完整日志 + 空间下限 + 分级重试 + 保留可回滚快照
nohup blastdb_download.py -r "$ROOT" -s gcp \
      -j 8 --connections 4 --min-split-size 64M \
      --min-free 50G --file-retries 3 --keep-snapshots 2 \
      --log-file "$LOG/nt.log" \
      download nt taxdb &

tail -f "$LOG/nt.log"          # 另开一个终端跟踪（含百分比/速度/ETA/文件计数）

# 3) 完成后全量复核（1 TiB 大约十几分钟）
blastdb_download.py -r "$ROOT" verify

# 4) 指给 BLAST
export BLASTDB="$ROOT/current"
blastdbcmd -db nt -info
```

每个参数为你买到的稳健性：

| 参数 | 作用 |
|---|---|
| `doctor` | 先知道空间/配额/工具够不够，避免下到一半才发现（并行文件系统上配额常远小于 `df`） |
| `--dry-run` | 打印计划、磁盘估算、可用空间、会自动附加哪些库；只读、不消耗空间 |
| `-s gcp` | 不可变快照 + 真 `md5Hash` + 免解压（三条路里最确定的一条） |
| `--log-file` | 带时间戳的完整日志：计划、进度、失败原因分组、重试记录都在里面（`-q` 也照记） |
| `--min-free 50G` | 可用空间跌破下限立即停（**已下内容全部保留**），而不是把盘写满再报一堆失败 |
| `--file-retries 3` | 瞬时故障（超时/连接重置/403 限流/5xx）自动整批重试，遇限流还会自动降并发 |
| `--keep-snapshots 2` | 保留上一份快照，`rollback` 可秒级回退 |
| `verify` | 全量 md5 + 内嵌构建指纹 + 完整性交叉校验；可随时重跑，纯离线 |
| `nohup`/`tmux`/systemd | 断连不影响；即使中途 Ctrl-C，重跑同一条命令即续 |

**长任务注意点**

- **云快照保留期约 1 个月**（实测 AWS 现存 `07-10 / 07-14 / 07-21`，GCS 现存 `07-21 / 09-19 / 09-22`）。
  1 TiB 的 `nt` 若跨过保留期，后续文件会 404：工具会明确报错并给出建议，重跑会落到新快照、
  抢救仍然一致的卷、其余重下。
- **磁盘峰值**：NCBI 提取模式 = 载荷 + 一个归档；`--keep-archives` 会变成 ≈ 载荷 + 全部归档（`nt` ≈ 1.9 TiB）。
- **随时可安全中断**：`current` 永不会指向半更新状态；已下完的文件与已完成的分片都在
  `<root>/.staging`，重跑续跑（详见[任务中断后续跑](#任务中断后续跑)）。
- **定时增量更新**见[常用场景](#常用场景)里的 cron 示例；建议每次跑完顺手 `verify --quick`。

---

## 工作原理

```
revision(db) := { (文件名, 大小, 权威 md5, 远端对象标识) ... }
revkey(db)   := sha1(来源 | 快照 | 库名 | revision(db))
```

1. **解析来源快照**：云端读 `latest-dir`（唯一权威指针）并校验 manifest；NCBI 读实时 manifest，把每个 `.md5` 边车作为权威 md5。
2. **能复用就不下载**：与已安装版本逐文件比对，一致的文件直接硬链接进新快照。
3. **下载进按修订指纹隔离的 staging**：`<root>/.staging/<db>/<revkey>/`，因此 aria2 的 `.aria2` 断点与内置下载器的分片状态**不可能**被续到另一个版本上。
4. **逐文件校验**：权威 md5；失败文件会被删除（aria2 校验失败会把坏文件留在磁盘上）。
5. **复核远端修订未变**：变了说明传输期间官网在发新版 → 丢弃 staging 重试或中止。
6. **集合级校验**：卷构建时间戳必须一致、卷序号必须 `0..N-1` 连续、文件名卷号必须等于内嵌卷号、`.njs` 与 `-metadata.json` 必须与卷指纹吻合、载荷文件名必须属于该库、`bytes-total` 必须等于磁盘上的实际字节数。
7. **原子落地**：构建新快照目录 → 原子替换 `current` 软链接。失败时 `current` 原封不动。

---

## 命令参考

| 命令 | 说明 |
|---|---|
| `showall` | 列出源上的数据库及其大小 / 更新时间 / 卷数 |
| `download`（别名 `update`） | 下载或更新数据库，落地为新快照并原子切换 |
| `verify` | 复核已安装快照（md5 + 集合指纹 + 可选远端比对） |
| `repair` | 只重新下载校验不通过的文件，其余硬链接复用 |
| `inspect` | **离线**报告任意目录的构建指纹（不需要状态文件，不需要 BLAST） |
| `staging` | 报告中断/旧运行在 `.staging` 里留下了什么、有多少可用；加 `--against-remote` 会告诉你"能否原样续跑"以及"旧修订里还有多少个文件能复用" |
| `doctor` | 报告可用空间、用户配额、以及本机有哪些可选外部工具（`aria2c`/`quota`/`gsutil`/`aws`/`blastdbcmd`…）与安装方式 |
| `list` | 列出本地快照，标注当前指向 |
| `gc` | 清理旧快照与 staging 残留 |
| `rollback` | 把 `current` 指回旧快照 |
| `config` | 打印生效配置，或生成配置模板 |

各子命令专属参数：

| 子命令 | 专属参数 |
|---|---|
| `showall` | `--format name\|tsv\|pretty\|json` |
| `download` | `--force`、`--takeover`、`--no-check-md5` |
| `verify` | `--snapshot NAME`、`--quick`、`--no-md5`、`--against-remote` |
| `repair` | `--snapshot NAME`、`--quick` |
| `inspect` | `--dir DIR` |
| `gc` | `--keep N` |
| `rollback` | `--to N`、`--snapshot NAME`、`--list` |
| `config` | `--init`、`--template`、`--path FILE`、`--force` |

退出码：`0` 成功 · `1` 用法/运行错误 · `2` 命令行参数错误（argparse，例如把子命令专属参数放错位置）· `3` 校验失败（已拒绝安装）· `4` 检测到更新竞态并中止 · `130` 被 Ctrl-C 中断。

> 全局参数写在子命令**前或后都行**；子命令专属参数（如 `download --force`、`verify --quick`）必须写在对应子命令之后。
> 放错位置时错误信息会直接点出它属于哪个子命令，例如：
> `hint: `--force` is an option of the `download` sub-command; put it after `download`

---

## 参数参考

下面所有全局参数都可以写在子命令**之前或之后**。

**位置与来源**

| 参数 | 说明 |
|---|---|
| `-r, --root DIR` | 镜像根目录，存放各快照与 `current`（默认 `./blastdb`） |
| `-c, --config FILE` | 指定配置文件（独占，不再查找其它位置） |
| `-s, --source {gcp,aws,ncbi,auto}` | 数据源，默认 `gcp`。`ncbi` 最新；`auto` 优先 `ncbi` |
| `--ncbi-dir PATH` | NCBI 目录，默认 `/blast/db`（也可指 `/blast/db/v5`） |
| `--ncbi-base URL` | NCBI 树/镜像的 origin，默认 `https://ftp.ncbi.nlm.nih.gov`。**指定后载荷 URL 一并重定向** |
| `--ncbi-url {auto,mirror,manifest}` | `mirror`：从 `--ncbi-base`+`--ncbi-dir` 取文件；`manifest`：用清单里发布的 URL（`ftp://`→`https://`）。默认 `auto` |
| `--insecure` | 不校验 TLS 证书（仅用于自建镜像的调试） |

**传输**

| 参数 | 说明 |
|---|---|
| `-j, --jobs N` | 总并发连接数，默认 `max(1, min(8, 核数/2))` |
| `-x, --connections N` | 单文件分片连接数，默认 4 |
| `-k, --min-split-size SIZE` | 分片最小粒度，默认 64M；大于它的文件才会被切分 |
| `--limit-rate SIZE` | 总下载限速，如 `50M`。**两种下载器都生效**（内置下载器用共享令牌桶） |
| `--timeout SEC` | 连接/读取超时，默认 60 |
| `--tries N` | 每个请求的尝试次数，默认 5 |
| `--aria2c PATH\|auto\|none` | aria2c 可执行文件；`none` 强制使用内置下载器 |
| `--no-probe-sizes` | 不通过 HEAD 探测未知对象大小 |

**一致性策略**

| 参数 | 说明 |
|---|---|
| `--on-torn {retry,fail}` | 传输期间官网换版：重试（默认）或直接中止 |
| `--torn-retries N` | 重试次数上限，默认 3 |
| `--torn-wait SEC` | 每次重试前的等待，默认 60 |
| `--reuse-verify {probe,md5,size}` | 复用已装文件前的校验强度，默认 `probe`（大小 + 头尾各 64 KiB 指纹） |
| `--no-check-md5` | 安装前跳过整棵树的 md5 复算（`download`） |
| `--smoke-test {auto,always,never}` | 用本地 `blastdbcmd -info` 冒烟测试；默认 `auto` **仅告警不阻断**（见[注意事项](#注意事项与排错)） |

**内容与空间**

| 参数 | 说明 |
|---|---|
| `--taxdb` / `--no-taxdb` | 是否自动附加 `taxdb`（**默认附加**，源不限：云端把 taxonomy 当独立库；NCBI 也单独发布 `taxdb.tar.gz`，实测部分归档虽自带该载荷，但"缺 taxdb"是静默故障——物种名只显示 `N/A`。代价约 62 MiB，用 `--no-taxdb` 可关） |
| `--metadata-json` / `--no-metadata-json` | 是否额外获取 `<db>-nucl-metadata.json`（默认获取，它是一条重要的完整性判据） |
| `--keep-archives` | 在快照中保留已验证的 `.tar.gz`（默认解压后删除，仅保留 `.md5` 证据） |
| `--no-dedupe-taxonomy` | 即便同名同大小已存在也重新解压归档成员（默认去重） |
| `--keep-snapshots N` | 保留的快照数量，默认 2（`current` 永远保留） |
| `--no-disk-check` | 跳过下载前的可用空间检查 |
| `--min-free SIZE` | 下载过程中**可用空间**（已扣除用户配额）低于此值时立即停止（默认 2 GiB）。已下完的文件保留，重跑即续 |
| `--file-retries N` | 单文件失败但**原因可重试**时，整批重试的次数（默认 3）；可重试原因会自动降低并发 |
| `--progress-interval SEC` | 周期性进度行的间隔，**默认 3600 秒**；此外**每完成一个文件都会写一行** |
| `--log-file FILE` | 把诊断写入该文件（带时间戳）。**指定后控制台自动只留警告与错误**，进度与信息只进文件 → `nohup.out` 基本为空 |
| `--no-log-file` | 不做默认日志（见下） |
| `--console auto\|full\|errors\|off` | 控制台输出策略：`auto`（默认，有日志文件时只留警告/错误）、`full`（同屏镜像）、`errors`（只留错误）、`off`（完全静默） |
| `--adopt DIR` | 也从 DIR 里找已下载的文件（可重复）。**完整文件**按权威 md5 校验后硬链接进 staging，不再重下 |
| `--adopt-partial` | 连**没下完**的文件也接管：有 aria2 控制文件就带着续传信息一起接管，否则要求是"无空洞的顺序前缀"才接管 |
| `--adopt-extracted` | 连**已解压的载荷**也接管，但仅当它们构成一次完整构建时（tar.gz 布局成员无官方 md5，只能做强证据校验） |

**输出**

| 参数 | 说明 |
|---|---|
| `--dry-run` | 只打印计划，不传输、不写快照 |
| `--json` | 在 stdout 输出机器可读结果 |
| `-q, --quiet` | 关闭 stderr 诊断（stdout 的数据输出不受影响） |
| `-v, --verbose` | 增加诊断；可重复。`-vv` 达到 debug 级 |

---

## 配置文件

**工具不会自动生成配置文件。** 不配置也能运行（全部走内置默认值）；只有需要长期改默认行为时才建一份。

```bash
# 生成一份"全部注释掉"的模板，写到 <root>/blastdb-download.toml
python3 blastdb_download.py -r /data/blastdb config --init

# 写到用户级位置
python3 blastdb_download.py -r /data/blastdb config \
        --init --path ~/.config/blastdb-download/config.toml

# 已存在时不覆盖，需 --force；只查看内容用 --template
python3 blastdb_download.py config --template
```

模板中每一项都是注释，因此生成它**不会改变任何生效值**；它同时是这份文档之外的完整参数说明。

查找顺序（**先匹配者胜，越具体优先级越高**）：

```
命令行参数
  > --config FILE
  > <root>/blastdb-download.toml            # 镜像局部（最具体）
  > $XDG_CONFIG_HOME/blastdb-download/config.toml
  > ~/.blastdb-download.toml
  > 内置默认值
```

排查"为什么某个值没生效"：

```bash
python3 blastdb_download.py -r /data/blastdb config | jq '{config_file, config_search_path, jobs}'
```

`config_file` 为 `null` 表示没有加载任何配置文件；`config_search_path` 是完整查找链。

仅写需要改的项：

```toml
[general]
source = "ncbi"              # 想长期走 NCBI 就写这里
jobs = 8
keep_snapshots = 2
reuse_verify = "probe"       # probe | md5 | size
limit_rate = "50M"
aria2c = "/usr/bin/aria2c"   # 或 "auto" / "none"
aria2c_extra_args = ["--disk-cache=256M"]   # 透传给 aria2c 的任意参数
```

---

## 注意事项与排错

**1. `blastdb_download.py download nt > log` 抓不到日志。**
诊断信息（进度、警告、错误）全部在 **stderr**，stdout 只放数据（`showall`、`--dry-run`、`--json` 的输出）。想要完整日志：

```bash
python3 blastdb_download.py ... download nt > nt.out 2> nt.err
python3 blastdb_download.py ... download nt 2>&1 | tee nt.log
```

**1.5 `nohup.out` 里为什么有两份日志？**

因为 `--log-file` 以前只是**额外**加一个文件，控制台照旧。现在语义改成：

- **任何会改动镜像的命令**（`download`/`repair`/`gc`/`rollback`）都**默认写 `<root>/log`**，
  并在启动时打印一行 `logging to <path> (use --log-file to move it, --no-log-file to disable)`；
- **一旦有日志文件**（默认的或 `--log-file` 指定的），控制台自动进入 `--console auto`：
  只有**警告与错误**留在屏幕上，进度、计划、每文件完成行只进文件 —— 所以 `nohup blastdb_download.py ... > nohup.out` 的
  `nohup.out` **基本为空**（除非出现告警）；
- 想彻底安静（连错误也不上屏）：`--console off`；想两者都要：`--console full`；
- 只想看屏幕不想要文件：`--no-log-file`（`showall`/`verify`/`doctor` 等只读命令本来就不写日志文件）。

**日志里的粒度**

```bash
# 每个文件完成一行（info 级，始终记录）
2026-09-24 13:02:27 [3/12] 16S_ribosomal_RNA.nnd  218.82 KiB in 2s (415.34 KiB/s)
# 周期性进度行，默认每 60 分钟一行
2026-09-24 13:02:28 16S_ribosomal_RNA:   4.6% 832.71 KiB/17.58 MiB 415.31 KiB/s files 3/12 eta 41s
```

`--progress-interval` 可调（例如 `600` 就是 10 分钟一行）；aria2c 模式下每文件完成行由后台跟踪 aria2 自己的
`[NOTICE] Download complete:` 日志得到，不需要额外开关。

**2. `blastdbcmd` / `blastdbcheck` 报 `mdb_env_open: MDB_INVALID: File is not an LMDB file`。**
这是**本地 BLAST 构建与 NCBI LMDB 文件不兼容**，与下载正确性无关：NCBI 官方 tar.gz 解压后同样报错，而本地 `makeblastdb` 自建的库正常。此时 `--smoke-test auto` 只会给出告警而不阻断（要它成为硬失败用 `--smoke-test always`），权威判据是 md5 证据与 `inspect`/`verify` 的内部指纹。换个能读 NCBI LMDB 的 BLAST 版本即可恢复正常。

**3. `%S`（物种名）显示 `N/A`。**
`blastdbcmd -outfmt "%S"` 的物种名来自 `taxdb`，而 `taxdb` 必须和数据库**在同一个 `BLASTDB` 目录**里。云端源默认会一起装 `taxdb`；NCBI 源的归档里自带。若你用了 `--no-taxdb` 或单独覆盖了 `taxdb`，请确认目标目录里有 `taxdb.btd` / `taxdb.bti` / `taxonomy4blast.sqlite3`。

**4. 云端快照比 NCBI 实时目录旧。**
实测 `latest-dir` 停在 `2026-07-21`，而 NCBI 实时目录已有 `2026-09-22` 的数据。要最新数据用 `-s ncbi`；要绝对不可变的一致性用 `-s gcp`（代价是滞后）。二者的选择不影响本工具的一致性保证，只影响数据新鲜度。

**4.5 为什么每库的 `<db>-nucl-metadata.json` 每次都会重下？**

因为 NCBI **不给它发布 md5**。而"大小相同"不是身份证明：两个修订的这个 JSON 完全可能等长而内容不同——
早期的实现就因此在撕裂重试时把**旧修订的元数据当成新的复用**，随后元数据与卷指纹日期不符，
本该成功的重试被整体拒绝（数据没被污染，但白跑一趟）。

现在的规则很简单：**可复用的前提是有权威 md5**（NCBI `.md5` 边车 / GCS `md5Hash`）。
没有 md5 的文件（只有这个 ~500 B 的元数据 JSON）一律重新下载——代价可以忽略，换来的是"复用的每一个字节都被证明过"。

**4.6 `--adopt-partial` 会写你原来的文件（这是有意为之）。**

接管"没下完的文件"时用的是**硬链接 + 从末尾续传**，所以：

- 你那份半成品会被**就地补完**（这通常正是你想要的）；如果本次运行半途放弃，它仍是一个前缀完整的半成品，不会有坏数据；
- 校验不通过时工具删掉的是 **staging 里的链接名**，你的原文件仍在；
- 跨文件系统（adopt 目录与 `--root` 不在同一挂载点）会退化为复制：aria2 分片下载留下的**稀疏空洞在复制后可能变成真实占用的零**（Python 的快速复制不保证保留空洞）。这种情况建议把 adopt 目录与 root 放在同一文件系统，或直接让工具重下。

**5. S3 源没有真 md5。**
S3 大对象的 ETag 是分片哈希（形如 `"...-358"`），不是 md5，因此会退化为"大小 + 内部构建指纹 + 集合校验"，状态文件里 `verification` 字段会如实标为 `mixed`。需要强校验请用 `-s gcp`。

**6. `--reuse-verify probe`（默认）发现不了文件正中间的损坏。**
它读大小 + 头尾各 64 KiB，足以捕获截断、追加、首尾损坏（毫秒级开销）。要全量强校验用 `--reuse-verify md5`，或定期跑 `verify`（默认全 md5）。

**7. 中断之后不要立刻跑 `gc`。**
`gc` 会清空整个 `.staging`，等于放弃断点。成功安装后 staging 本来就会被清空，因为快照树自身就是续跑点。

**8. Ctrl-C 不是瞬时的。**
Python 主线程阻塞在线程池等待上，信号会在**当前分片/请求结束或超时后**生效（默认 socket 超时 60s，可用 `--timeout` 调小）。中断后 stderr 会告诉你续跑位置。

**9. 同一个镜像根目录不要并发运行写命令。**
`download` / `repair` / `gc` / `rollback` 会取 `<root>/.lock` 文件锁，第二个进程会直接报错退出而不是互相破坏。只读命令（`list` / `verify` / `showall` / `inspect` / `config`）不加锁，可以在下载期间使用。若你另外用 `flock` 包裹 cron，也不会冲突。

**10. `current` 必须是一个软链接。**
若 `<root>/current` 已经是真实目录（例如从 `update_blastdb.pl` 迁移过来），工具会拒绝运行并给出版本化目录的迁移提示；确认可改名后再用 `--takeover` 让它把旧目录改名为 `.legacy-current-<时间戳>`。

**10.5 为什么"空间够"却失败了：配额。**

`df` 报的是文件系统整体可用量，并行文件系统（Lustre/GPFS/NFS）或容器叠层上，
**用户配额往往远小于它**。本工具因此：

- `doctor` 会尝试 `quota -uvs <user>`、`lfs quota -u <user>`、`xfs_quota report -u <user>`，
  报告实际配额与剩余量；
- 前置空间检查与 `--min-free` 看门狗都取 **df 与配额中较小的那个**；
- `quota` 命令没装时明确提示（`apt install quota` / `yum install quota`），并说明"在并行文件系统上配额可能远小于上面的可用空间"。

```bash
python3 blastdb_download.py doctor            # 看配额工具是否存在、配额多少
lfs quota -u $USER /data/public               # 或直接查（Lustre）
quota -uvs $USER                              # 或直接查（有 quota 包时）
```

**11. 出现 `N/M file(s) failed to download` 时怎么继续。**

先看报告里的**原因分组**——它已经给出每一种原因的数量与代表文件，例如：

```
ERROR: 325/384 file(s) failed to download.
ERROR:     318 x no space left on the filesystem
ERROR:           e.g. nt.015.tar.gz, nt.016.tar.gz, nt.017.tar.gz (+315 more)
ERROR:       7 x HTTP 403 forbidden (often rate limiting)
ERROR:   free space on /data/blastdb: 1.20 GiB
ERROR:   nothing was installed; the verified files and completed chunks are kept under
         /data/blastdb/.staging
ERROR:   to continue, fix the cause above and re-run the same command; already
         downloaded files are not fetched twice
ERROR:   space hints: --min-free 50G lowers the abort threshold, --limit-rate 50M ...
```

常见的几种原因与对策：

| 原因 | 说明 | 处理 |
|---|---|---|
| `no space left on the filesystem` / `filesystem quota exceeded` | `nt` 解压后约 1 TiB，`nt` 单卷 `tar.gz` 就有 2.6–5.1 GB。也可能是**用户配额**远小于 `df` 显示的可用量（先用 `doctor` 确认） | 腾空间；或换 `-s gcp`（下裸索引文件，**不需要解压**，峰值只有载荷本身）；`--min-free 50G` 让它在更早的位置停止；`df -h` 与配额命令（`lfs quota` / `quota -s` / `xfs_quota`）都要看 |
| `HTTP 403 forbidden` / `connection reset` / `timeout` / `5xx` | 并发过高被限流或网络抖动 | **会自动重试**：整批最多 `--file-retries` 次（默认 3），遇到 403/重置还会自动降低并发；也可手工 `-j 4 -x 2` / `--limit-rate 50M` |
| `HTTP 404 not found` | 源正在发版（清单先于文件），或镜像不同步 | 等一会儿再跑，或 `-s gcp` 用不可变快照 |
| `host name resolution failed` / `TLS certificate problem` | 代理/证书问题 | 检查 `HTTPS_PROXY`；自建镜像调试可临时 `--insecure` |
| `permission denied` | 目录属主/权限不对 | 修权限；注意 `current` 是软链接，不要用会解引用的拷贝方式 |

**失败按原因分流**：可重试的（超时、连接重置、403 限流、5xx、校验和不符、原因不明）会自动重试；
不可重试的（无空间、配额、权限、404）会立刻停止并说明，避免把时间浪费在注定失败的重试上。
日志里会写明 `attempt k/N` 与本次重试的原因。

**关键点：`nothing was installed`。** 失败不会污染 `current`；已下完的文件与已完成的分片都在 `<root>/.staging/<db>/<revkey>/` 里，
修好原因后**重跑同一条命令**即可，它们不会重下（已解压过的归档凭**解压回执**复用；换了修订/来源导致目录名变化时，
还会从旧 staging 目录里**抢救出 md5 仍然一致的文件**，然后删掉不配套的部分）。想先确认还剩多少工作量：

```bash
du -sh /data/blastdb/.staging          # 已经下到多少
python3 blastdb_download.py -r /data/blastdb --dry-run download nt   # 还需多少（不消耗空间）
```

若只是想把"下完但没装成"的库补完，`repair` 也可用（它只重下校验不过的文件）。

**12. 磁盘空间。**
`nt` 解压后约有 `bytes-total` 量级（≈1 TiB）。工具会在下载前按元数据估算并检查余量（要求 5% 余量 + 1 GiB 保留），也可用 `--no-disk-check` 关闭。多快照并存靠硬链接，只有真正变动的文件才额外占盘，`--keep-snapshots` 控制保留数量。
峰值占用的模型是：**载荷 + 一个归档**（每个归档解压完立刻释放）。要装成"归档也留着"用 `--keep-archives`，
那时峰值变成 载荷 + 全部归档（`nt` 会从约 1.0 TiB 涨到约 1.9 TiB），`--dry-run` 会把这个估算直接打印出来。
下载过程中如果可用空间跌破 `--min-free`（默认 2 GiB），工具会**立即停止**并在报告里说明，而不是跑到一半炸掉。

**13. Windows 只是"能跑"，不是主目标。**
内置下载器的分片写入在 Windows 上走 `lseek`+`write` 回退路径（性能略低，正确性相同）；`os.link` 硬链接在无权限时自动回退为复制；而 **`current` 软链接需要在 Windows 上开启"开发者模式"（Developer Mode）或以管理员身份运行**，否则会明确报错。建议在 Linux/macOS/WSL 上运行；Windows 上的关键路径建议配 `--aria2c`。

**14. 别把下载的数据或运行产物提交进仓库。** 见文末 `.gitignore` 建议。

---

## 校验已有镜像

`inspect` 不需要状态文件、不需要 BLAST、不联网，直接对**任意目录**（包括用 `update_blastdb.pl` 或手写 aria2c 循环下载的目录）报告内部构建指纹：

```bash
python3 blastdb_download.py inspect nt --dir /data/public/databases/NT
python3 blastdb_download.py --json inspect nt --dir /data/public/databases/NT
```

输出示例（正常）：

```
nt  (/data/public/databases/NT)
  files              : 3452
  volume indexes     : 345 (ordinals 0..344)
  embedded build date: 2026-07-19T03:10 (all volumes agree)
  verdict            : OK
```

输出示例（混装——这正是"用 aria2c 循环下载"最常见的后果）：

```
nt  (/data/public/databases/NT)
  volume indexes     : 345 (ordinals 0..344)
  embedded build date: MIXED -> 2026-07-19T03:10: nt.000.nin, nt.001.nin ...
                              2026-09-16T13:46: nt.344.nin
  verdict            : FAILED
      MIXED BUILD TIMESTAMPS across volumes - this file set is torn
```

它同时能发现：缺卷（序号不连续）、文件名卷号与内嵌卷号不符、缺少/多余的载荷文件、文件被截断（`bytes-total` 不符）、载荷与其 metadata 来自不同构建。

与 BLAST 自带检查的分工：

| 检查方式 | 能发现 | 需要什么 |
|---|---|---|
| `blastdb_download.py inspect` | 混装、缺卷、缺文件、截断、载荷与元数据不一致 | 只要 Python |
| `blastdb_download.py verify` | 上述 + 每个文件的 md5 是否与下载时证明的一致 | 状态文件（本工具装的镜像） |
| `blastdbcmd -db X -entry ACC -outfmt "%a %o %T %S"` | 单点抽查：登录号、OID、TaxID、物种名是否对得上 | 可用的 BLAST + `taxdb` |
| `blastdbcheck -db X -dbtype nucl -verbosity 2 -random 200` | ISAM 索引抽样、卷结构、可选 TaxID 检查 | 可用的 BLAST（本机的 conda 2.17.0 读不了 NCBI LMDB，会直接 FAILURE） |

`blastdbcheck` 常用参数（`blastdbcheck -h` 所列）：`-db`、`-dbtype`、`-dir`、`-recursive`、`-verbosity 0..4`、`-full`、`-stride`、`-random`、`-ends`、`-no_isam`、`-legacy`、`-must_have_taxids`。

---

## 任务中断后续跑

支持，三层生效：无论 Ctrl-C、`kill -9` 还是断电，重跑同一条命令即可继续。

| 层 | 场景 | 机制 |
|---|---|---|
| ① 文件内断点 | 单个大文件传到一半 | 分片状态文件 + 按偏移 `pwrite`，只补缺失分片；单流传输从已落盘前缀继续（`Range: bytes=<已有长度>-`）。aria2c 场景由它自身的续传机制（`.aria2` 控制文件）承担 |
| ② staging 复用 | 上次跑到一半被杀，部分文件已完整 | 重启时对 staging 内同名文件做 md5 校验，通过即复用；已下载但未解压的归档按"成员是否已在表里"补齐 |
| ③ 快照硬链接 | 日常增量更新 | 与已装版本比对权威 md5，一致则硬链接进新快照 |

安全性的关键：**staging 目录名包含修订指纹**，所以中断期间上游换版只会落到另一个目录，旧断点无法被续到新版本上；分片先落盘并 `fsync` 再记账，断电也不会出现"状态文件说完成了、数据没落盘"；最终 md5 校验兜底，最坏情况是某个文件整体重下。

实测（`TestResume`，按字节计数而非看日志）：400 KiB 在 300 KiB 处被 `SIGKILL`，重跑**只补剩余 ~100 KiB**；分片模式下挂起其中一个 range 请求，另外 3 个已完成分片重跑时**未被重下**；预置完整归档到 staging 后，重跑对上游的载荷请求数为 **0**。

---

## 与 update_blastdb.pl 对照

| | `update_blastdb.pl` | `blastdb_download.py` |
|---|---|---|
| 一致性单位 | 单文件 | **整个数据库（卷集合）** |
| 跨卷混版检测 | 无 | **内嵌构建时间戳 + 卷序号 + `.njs`/metadata 交叉校验** |
| 官网正在更新 | 可能装入混装集合 | 检测到即重试或中止，绝不安装 |
| 云端校验 | 无 | GCS `md5Hash` 逐文件校验；S3 无 md5 时如实降级 |
| 数据新鲜度判断 | mtime / `Last-Modified` | 权威 md5 + 本地状态文件 + 内容指纹 |
| 断点续传 | 靠 curl 重下整个文件 | 三层续跑，且按修订指纹隔离 |
| 安装方式 | 原地覆盖 | 新快照 + 原子切换 `current`，可秒级回滚 |
| 增量更新 | 逐文件比 mtime | 逐文件比 md5 并硬链接复用 |
| 共享载荷冲突 | 后解压覆盖先解压（隐式） | 按确定性规则取新并明确告警 |
| 检查已有镜像 | 无 | `inspect`（离线、无需 BLAST） |
| BLAST 用法 | `BLASTDB=<dir>` | `BLASTDB=<root>/current` |
| 依赖 | Perl + Net::FTP + curl + JSON::PP | Python 标准库；aria2c 可选 |

---

## 第三方软件依赖（哪些必须装）

**必须安装的只有 Python。** 本工具是单文件、**零第三方 Python 包**（`import` 全是标准库），
不需要 `pip install` 任何东西；除 Python 之外没有"必须装"的外部程序：

| 依赖 | 是否必须 | 用途 / 缺失后果 |
|---|---|---|
| **Python ≥ 3.9** | ✅ **必须** | 运行本体（3.11+ 才能读取 TOML 配置；`config --init` 在 3.9/3.10 也能生成模板） |
| **Python 标准库** | ✅ **必须**（自带） | 网络（urllib）、解压（tarfile/gzip）、校验（hashlib）、并发（threading/concurrent.futures）——**不需要 pip** |
| **支持符号链接的可写文件系统** | ✅ **必须** | `current` 是原子切换的软链接。硬链接是**强烈建议**：没有它快照回退为复制（正确性不变，但多占一份空间）。Windows 见[注意事项](#注意事项与排错) |
| **到源站点的网络** | ✅ **必须** | NCBI `ftp.ncbi.nlm.nih.gov` / GCS `storage.googleapis.com` / S3 `s3.amazonaws.com`，**全部匿名 HTTPS**，无需任何云端 SDK 或凭证 |

以下全部**可选**（缺失不影响正确性，只影响便利性；`doctor` 会告诉你本机有哪些、怎么装）：

| 可选工具 | 装了能得到什么 | 没装时的行为 |
|---|---|---|
| **aria2c** | 多连接分片、下载内 md5 校验、`.aria2` 断点文件（TB 级任务建议装） | 自动改用**内置下载器**：同样分片并发 + Range 续传 + md5 校验 + 空间看门狗，只是连接数与进度显示略有差异。用 `--aria2c none` 可强制内置 |
| **quota**（Debian/Ubuntu `apt install quota`，RHEL `yum install quota`） | 读取**用户配额**并纳入空间判断 | 只看 `df` 的可用量。**并行文件系统/容器上配额常远小于 `df`**，这时强烈建议装，否则可能下到一半才发现超额 |
| **lfs**（Lustre 客户端）/ **xfs_quota**（xfsprogs） | 对应文件系统上的配额读取 | 同上（回退到 `df`） |
| **df**（coreutils） | `doctor` 显示文件系统类型/容量 | 空间检查回退到 Python 的 `shutil.disk_usage`，仅报告信息变少 |
| **blastdbcmd**（BLAST+） | `--smoke-test` 装前冒烟测试、手工抽查（`%a/%o/%T/%S`） | 自动跳过冒烟测试（本来就是"仅告警不阻断"，见[注意事项](#注意事项与排错)第 2 条） |

**明确指出：以下工具本工具从不调用**（`doctor` 里也会如实标注 "never invoked by this tool"）：

| 工具 | 说明 |
|---|---|
| **gsutil / gcloud / aws** | 不需要。云端访问走匿名 HTTPS，官方 CLI 对本工具没有作用（`doctor` 只把它们列出来以免误会） |
| **tar / gzip 命令** | 不需要。归档在进程内用 `tarfile` 解压（不调外部 tar），因此也没有 `tar` 参数注入之类的问题 |
| **curl / wget** | 不需要。所有 HTTP 请求由 Python 标准库完成 |
| **blastdbcheck** | 工具不调用它；文档只是建议你在需要额外 ISAM 抽样时手工运行 |

`.gitignore` 建议：

```gitignore
__pycache__/
*.pyc

# 镜像数据与运行产物
blastdb/
*.blparts.json
*.aria2
current
```

---

## 测试

```bash
python3 test_blastdb_download.py -v      # 65 个用例，约 2 分钟
```

测试用本地假 NCBI 源 + 真实 `.nin` 头部结构，覆盖：

- 下载 → 校验 → 二次运行零下载（幂等）；多分片下载
- **下载途中官网换版**：`--on-torn fail` 退出码 4 且不产生任何快照；`--on-torn retry` 收敛到新版本；持续抖动时重试耗尽退出码 4
- **卷构建时间不一致 / 卷序号缺失 / 载荷与 metadata 不同构建 / 清单列了源上不存在的文件** → 一律拒绝安装
- **本地文件损坏或截断**：`verify --quick` 与 `verify` 都能发现；`repair` 只重下坏文件且不把坏文件带进新快照
- 快照原子切换、`rollback`、`gc --keep 2`
- **CLI 契约**：文档里的每个参数都存在且真正生效（能拦住"参数写到了没人读的 dest"这类静默失效）、参数放在子命令前后均可、并发运行被拒绝
- **断点续跑**：见上一节
- **失败报告**：原因分组（HTTP 404 / 无空间 / 限流 …）、`e.g.` 代表文件、`nothing was installed`、续跑提示；空间下限触发时立即停止且已下文件保留
- **分级重试**：可重试原因（403/重置/超时/校验和）会重试并最终成功；不可重试原因（404）**不重试**、立即失败；重试耗尽时报告尝试次数；`failure_policy` 的分类逐条断言
- **跨修订抢救**：revkey 变化后仍能从旧 staging 目录复用 md5 一致的文件，且旧目录被清掉
- **日志与进度**：`--log-file` 在 `-q` 下仍记录完整日志（含时间戳、进度、失败与重试）；无终端时按间隔输出进度行
- **环境自检**：`doctor` 报告文件系统/配额/工具，`--json` 可解析；云快照的复核**不再全量重列**对象（用桩件断言）；S3 XML 列表按前缀取且分片 ETag 不当 md5
- **部分批次恢复**：先成功几卷再失败（就是 `nt` 的真实形态），修好后重跑**只下缺的卷**（按字节计数验证）；被中断在解压阶段的场景凭"解压回执"复用已解压卷
- 归档解压后即释放（`--keep-archives` 时保留）
- **inspect**：正常 / 混装 / 缺文件 / 截断 / 多文件 / 未知库
- 配置模板与内置默认值逐键比对、`config --init` 生成的模板是惰性的且会被自动加载

---

## 更新日志

完整记录见 [CHANGELOG.md](CHANGELOG.md)（Keep a Changelog 格式）。摘要：

| 版本 | 要点 |
|---|---|
| **1.2.0** | 审计轮：无权威校验依据的文件不再被"仅凭存在"当作已验证；NCBI 的 per-db metadata JSON 用 `Content-Length` 作依据；**Ctrl-C/中止立即停止**（不再把队列里的分片跑完）；解压结果优先于接管副本；失败报告与配额一致；移除死代码 |
| **1.1.0** | 现场反馈轮：修 `-k/--min-split-size` **完全失效**；修云端按前缀列对象**漏掉 `taxonomy4blast.sqlite3`**；修**错误没写进 `--log-file`**；`--log-file`/无终端进度；**按原因分级重试**；`--min-free` 空间下限；`doctor`（空间/配额/外部工具）；`staging`；`--adopt*`（接管旧 aria2c 循环的现场）；归档解压后即释放（峰值 2 TiB → 1 TiB）；解压回执 |
| **1.0.0** | 首版：集合级一致性（revkey 前后复核 + 内嵌构建指纹 + 元数据交叉校验）、原子切换 `current`、三层断点续跑、`inspect` 离线指纹检查 |

## 协议与出处

本工具以 **MIT** 协议发布（见 `LICENSE`）。它不是 NCBI `update_blastdb.pl` 的翻译版，而是**独立实现**：
其行为、云端 `latest-dir` 语义、`blastdb-metadata-1-1.json` 清单格式，以及 `.nin`/`.pin` 卷索引头部结构，均通过观察 NCBI 公开接口与公开数据推导得到，未复制上游源码。

上游参照物 `update_blastdb.pl` 由 NCBI 以 *NCBI Public Domain Notice*（SPDX: `NCBI-PD`）发布，作者 Christiam Camacho。
若本工具对你的研究有帮助，请一并引用 BLAST+：Camacho C, et al. *BLAST+: architecture and applications.* BMC Bioinformatics. 2009;10:421.

本项目与 NCBI/NLM 无隶属关系，不代表 NCBI 官方立场；BLAST、NCBI 为其各自所有者的商标。
下载得到的 BLAST 数据库由 NCBI 提供，NCBI 声明对其站点数据不作使用与分发限制
（<https://www.ncbi.nlm.nih.gov/home/about/policies/>），但库内可能包含第三方数据，使用前请自行核对相应条款。

---

## 附录：为什么必须这样做

### A.1 NCBI 在线目录是"原地可变"的

`https://ftp.ncbi.nlm.nih.gov/blast/db/` 在发布新版时逐个替换文件。`nt` 有 383 卷、`nr` 有 176 卷，
更新窗口内完全可能 `nt.000.tar.gz` 已是新版而 `nt.050.tar.gz` 还是旧版。

`update_blastdb.pl` 只做单文件 md5：每个文件单独看都合法，但**集合是撕裂的**。BLAST 不会报错，
只会给出错误的比对结果。手写 `for i in {000..376}; do aria2c ...; done` 的循环更危险：它连
"开始下载前"的版本点都没有固定，前后差异可以跨越一整天。

### A.2 云端桶是"快照目录 + 指针"，只有指针可信

```
latest-dir               -> 2026-07-21-01-05-02      ← 唯一权威指针
2026-07-21-01-05-02/     ← 完整快照（10168 个对象，裸索引文件，不是 tar.gz）
2026-09-19-01-05-02/     ← 有 blastdb-metadata-1-1.json
2026-09-22-01-05-02/     ← 目录名更新，但 manifest 仍 404（正在灌数）
```

实测（2026-09-23）：`latest-dir` 仍指向 `2026-07-21-01-05-02`，而桶里已存在 `2026-09-22-01-05-02/`，
该目录**缺少 manifest**。任何"选名字最新的目录"的逻辑都会下到一套残缺的库。

另外，云端是裸索引文件：**S3 的 ETag 对大对象是分片哈希**（形如 `"...-358"`），不是 md5；
GCS 的 `md5Hash` 才是真 md5。

### A.3 每个卷内部都带"构建指纹"

卷索引文件 `.nin`（核酸）/ `.pin`（蛋白）的头部是定长结构：

```
u32 version(5) | u32 dbtype(0=核酸,1=蛋白) | u32 卷序号 | 标题 | 卷基名 | 构建时间戳
```

实测（通过 HTTP Range 读取云端对象前 512 字节）：

| 文件 | 内嵌卷序号 | 内嵌构建时间 |
|---|---|---|
| `nt.000.nin` / `nt.001.nin` / `nt.002.nin` / `nt.175.nin` | 0 / 1 / 2 / 175 | 全部 `Jul 19, 2026  3:10 AM` |
| `core_nt.00.nin` / `core_nt.01.nin` | 0 / 1 | 全部 `Jul 18, 2026  1:17 AM` |
| `nr.000.pin` / `nr.001.pin` | 0 / 1 | 全部 `Jul 14, 2026 12:42 AM` |

并且 `<db>.njs` 与 `<db>-nucl-metadata.json` 中的 ISO 时间与之一致（例如
`last-updated: 2026-07-19T03:10:00` 对应 `Jul 19, 2026  3:10 AM`）。

> **推论**：两个卷属于同一次构建 ⇔ 它们的内嵌构建时间戳相同。
> 这个判断完全在本地完成，不依赖任何一方元数据的自述，因此即使下载期间官网发了新版也骗不过它。

### A.4 载荷元数据是完整的文件清单

`<db>-nucl-metadata.json` 除了时间戳，还给出 `files` 列表、`number-of-volumes` 与 `bytes-total`。
实测这三项与磁盘上的载荷**完全吻合**（列出的文件集合相同，字节总数精确相等），因此它构成一条
离线完整性判据：缺文件、多文件、被截断都会立刻暴露——不需要网络，也不需要算一个 md5。
