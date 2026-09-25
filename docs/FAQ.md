# 常见问题（FAQ）

按"症状 → 原因 → 处理"整理。每个答案都给出可执行的命令或指向手册的具体章节。
使用手册：[`../README.md`](../README.md)；
运维细节：[`OPERATIONS.md`](OPERATIONS.md)；内部原理：[`DESIGN.md`](DESIGN.md)。

## 目录

- [A. 选型与基础](#a-选型与基础)
- [B. 命令行与参数](#b-命令行与参数)
- [C. 日志与输出](#c-日志与输出)
- [D. 失败、中断与续跑](#d-失败中断与续跑)
- [E. 空间与配额](#e-空间与配额)
- [F. 校验与配合 BLAST 使用](#f-校验与配合-blast-使用)
- [G. 运维、迁移与回滚](#g-运维迁移与回滚)

---

## A. 选型与基础

### A1. `-s gcp`、`-s ncbi`、`-s aws` 该选哪个？

| 你的诉求 | 选 |
|---|---|
| 最省心、最强确定性、省磁盘省 CPU | **默认 `-s gcp`**（不可变快照、逐文件真 `md5Hash`、**不需要解压**） |
| 要最新数据（快照可能滞后 1–2 个月） | `-s ncbi`（工具的一致性协议就是为这个"可变目录"设计的） |
| AWS 通道明显更快 / 有凭证要求 | `-s aws`，但要接受**校验强度降级**（大对象 ETag 是分片哈希，不是 md5） |
| 两者兼得 | 先用 `-s gcp` 建**不可变基线**（马上可用、可全量校验），需要时再用 `-s ncbi` 追新；未变动文件会硬链接复用 |

实测数字（2026-09-24）见 [`../README.md`](../README.md) 的"数据源怎么选"一节。

### A2. 一定要装 aria2c 吗？

不必。**用不用 aria2c 只取决于 `PATH` 上有没有它**，与数据源无关：

```bash
--aria2c auto      # 默认：找到就用
--aria2c none      # 强制内置下载器
--aria2c /opt/aria2c/bin/aria2c   # 指定（路径无效会立刻报错）
```

没有 aria2c **不损失正确性**：内置下载器同样具备分片并发（Range）、按偏移写入、断点续传、
md5 校验与空间看门狗。差别只在连接数上限与进度显示。TB 级任务建议装。

### A3. 需要装 `gsutil` / `gcloud` / `aws` / `curl` / `tar` 吗？

**都不需要。** 云端访问全部是**匿名 HTTPS**（Python 标准库完成），归档在进程内用 `tarfile` 解压。
`doctor` 会把这些工具标注为 `present but never invoked by this tool`——列出来只是避免误会。

### A4. Python 版本要求？

**3.9+ 就能运行**；**3.11+** 才能*读取* TOML 配置文件（`config --init` 生成模板在 3.9/3.10 也可以）。
**零第三方 Python 包**，不需要 `pip install` 任何东西。

### A5. 下载 `taxdb` 必须指定 `-s ncbi` 吗？

不必。三个源都有 taxonomy，且工具**默认自动附加 `taxdb`**（任何源）：

```bash
python3 blastdb_download.py -r /data/blastdb download nt            # 自动带 taxdb
python3 blastdb_download.py -r /data/blastdb -s ncbi download nt    # 同样自动带
python3 blastdb_download.py -r /data/blastdb --no-taxdb download nt # 关掉
```

NCBI 的 `taxdb.tar.gz` 实测 62.34 MiB（解压 293.9 MiB）；重复副本（归档自带的那份）在安装时按确定性规则消解。

### A6. 一个 `--root` 能混用不同源吗？

**不同次运行可以**：`current` 指向的快照会把未变动的库硬链接带过来。
但**同一次运行不要混装同一个库**：跨源的 `taxdb` 与 NCBI 归档自带的 taxonomy 可能来自不同构建，
工具会按"构建时间取新"决定并告警——能用，但你得多看一眼告警。

### A7. Windows 能跑吗？

能，但不是主目标：内置下载器走 `lseek`+`write` 回退（略慢、正确性相同），硬链接无权限时回退为复制，
而 **`current` 软链接需要"开发者模式"**（或管理员）。建议 Linux/macOS/WSL。

---

## B. 命令行与参数

### B1. 为什么把选项写在子命令后面会报 `unrecognized arguments`？

**全局参数**写在子命令前后都行；**子命令专属参数**必须写在对应子命令之后：

```bash
blastdb_download.py download nt --jobs 8        # ✓ --jobs 是全局的
blastdb_download.py --jobs 8 download nt        # ✓ 也可以
blastdb_download.py --force download nt         # ✗ --force 属于 download
blastdb_download.py download --force nt         # ✓
```

放错位置时错误信息会直接指出归属：`hint: --force is an option of the download sub-command; put it after download`。

### B2. `--force` 和 `repair` 有什么区别？

- `download --force`：**忽略已装版本**，把请求的库整份重新下载（最笨但最彻底）。
- `repair`：先逐文件校验，**只重下校验不过的文件**，其余硬链接复用（推荐用于修损坏）。

### B3. `--reuse-verify` 该怎么选？

| 值 | 复用时做什么 | 适合 |
|---|---|---|
| `probe`（默认） | 大小 + 头尾各 64 KiB 内容指纹 | 日常（毫秒级开销，能抓截断/首尾损坏） |
| `md5` | 全量 md5 | 强校验场景（代价是每次都要哈希整棵树） |
| `size` | 只看大小 | 只在你确信文件没被动过时 |

**注意**：没有官方 md5 的文件（目前只有 `<db>-nucl-metadata.json`）**永不参与复用**——"同大小"不是身份证明。

### B4. `-k/--min-split-size` 是干什么的？

单文件分片的**最小粒度**（默认 64M）：大到超过它的文件才会被切成多连接并行下载。
小文件（如索引 `.nin`）不会切分——这是有意的，避免为几百 KB 的文件开多条连接。

### B5. `--keep-archives` 什么时候用？

默认解压后就删掉归档（只留 `.md5` 作为证据），这样峰值磁盘 ≈ 载荷 + 一个归档。
加 `--keep-archives` 会在快照里保留全部 `.tar.gz`：**峰值变成载荷 + 全部归档**（`nt` ≈ 1.9 TiB），
适合想留原始证据的场景。

### B6. `--limit-rate` 两种下载器都生效吗？

生效。aria2c 走它自己的限速，内置下载器走共享令牌桶（1.3.1 起；更早版本该参数只对 aria2 生效）。

---

## C. 日志与输出

### C1. `nohup ... --log-file x` 为什么 `nohup.out` 里还有一份日志？

**1.3.0 起不会了。** 现在一旦有日志文件（`--log-file` 指定的，或写操作默认的 `<root>/log`），
控制台自动进入 `--console auto`：只有**警告与错误**留在屏幕，进度/计划/每文件行只进文件。

```bash
--console auto    # 默认：有日志文件时只留警告/错误
--console full    # 屏幕与文件都输出
--console errors  # 只留错误
--console off     # 完全静默
--no-log-file     # 连默认的 <root>/log 也不要
```

### C2. `blastdb_download.py download nt > log` 为什么 `log` 几乎是空的？

因为**诊断信息走 stderr**，stdout 只放数据（`showall`、`--dry-run`、`--json` 的输出）。
正确写法：

```bash
blastdb_download.py ... download nt > nt.out 2> nt.err
blastdb_download.py ... download nt 2>&1 | tee nt.log
blastdb_download.py ... --log-file /var/log/nt.log download nt   # 最推荐
```

### C3. 日志太吵 / 想看进度怎么办？

- 进度粒度：每个文件完成一行（始终记录） + 周期性进度行，后者间隔用 `--progress-interval`（默认 3600 秒）：

```
2026-09-24 13:02:27 [3/12] 16S_ribosomal_RNA.nnd  218.82 KiB in 2s (415.34 KiB/s)
2026-09-24 13:02:28 16S_ribosomal_RNA:   4.6% 832.71 KiB/17.58 MiB 415.31 KiB/s files 3/12 eta 41s
```

- 实时看进度：`tail -f /var/log/blastdb/nt.log`；
- 想更简短：`--progress-interval 600`（10 分钟一行）；
- 想安静：`--console off`。

### C4. `-q` 会连错误都不显示吗？

不会：`-q` 只静音**控制台**（含警告），**错误始终上屏**（退出码需要可解释），日志文件也不受影响。
要连错误都不显示：`--console off`。

---

## D. 失败、中断与续跑

### D1. 出现 `N/M file(s) failed to download` 该怎么继续？

看日志里的**原因分组**，它已经给出数量、代表文件与建议，例如：

```
ERROR: 325/384 file(s) failed to download.
ERROR:     318 x no space left on the filesystem
ERROR:           e.g. nt.015.tar.gz, nt.016.tar.gz (+316 more)
ERROR:   nothing was installed; the verified files and completed chunks are kept under …/.staging
ERROR:   to continue, fix the cause above and re-run the same command
```

**`nothing was installed`** 意味着 `current` 没被动过，已下内容保留在 `.staging`：
**修好原因后重跑同一条命令即可**，已下完的文件与已完成分片不会重下。
按原因处置的完整表格见 [`OPERATIONS.md`](OPERATIONS.md) 的"失败处置手册"。

### D2. 下载一半被 `kill`、断连或断电，会怎样？

安全。三层续跑（重跑同一条命令即继续）：

1. **文件内**：分片状态 + 按偏移写入只补缺失分片；单流从未落盘长度继续；
2. **staging 复用**：已下完但未安装的文件（含已解压的归档，凭解压回执）；
3. **快照复用**：与已装内容一致的硬链接。

分片**先落盘 `fsync` 再记账**，所以断电也不会出现"状态文件说完成了、数据没落盘"；
万一真发生，最终 md5 会把该文件整体作废重下。

### D3. 怎么接着"以前用 aria2c 循环"下到一半的现场？

```bash
python3 blastdb_download.py -r /data/db \
        --adopt /old/dir --adopt-partial --adopt-extracted \
        -s ncbi --log-file /var/log/nt.log download nt
```

- **完整文件**：按权威 md5 校验通过才硬链接复用；
- **没下完的文件**：有 `.aria2` 控制文件就带着它接管；否则必须是**无空洞的顺序前缀**（分片留下的空洞会被检测并拒绝）；
- **已解压的载荷**：归档已被 `rm` 时，只有整套构成**一次完整构建**才接管（时间一致、与源当前发布日期一致、卷数吻合、`.nin` 引用的 blob 存在、各卷文件种类一致），否则拒绝并打印原因后正常下载。

先看现场还剩多少：

```bash
python3 blastdb_download.py -r /data/db staging --against-remote
```

### D4. 中途能 Ctrl-C 吗？要等多久？

能。信号在**当前分片/请求结束或超时后**生效（默认 socket 超时 60 秒，可用 `--timeout` 调小），
**排队中的分片会被取消而不是跑完**。退出码 130，并提示续跑位置。任何时候中断都不会让 `current` 半更新。

### D5. `MIXED BUILD TIMESTAMPS` 是什么？

这是工具**拒绝安装**时的判定：下载/接管的这堆卷**内嵌构建时间戳不一致**，即"新旧混装"。
这也正是它比"只校验单文件 md5"更安全的地方——单文件都合法，集合却是撕裂的。
处理：直接重跑（会重新拉取）；若来自手工目录，用 `inspect` 定位哪些卷不对。

### D6. 下载期间上游发新版（torn）会怎样？

工具在下载前后各读一次修订指纹：不一致就说明"传输期间官网在发版"。

```bash
--on-torn retry   # 默认：丢弃本次 staging 后等待重试（--torn-retries / --torn-wait）
--on-torn fail    # 直接中止，退出码 4，且不留下任何快照
```

无论哪种，**绝不会装入混装集合**。

### D7. 云快照会消失吗？

会。云端**只保留约 3 个快照（≈1 个月）**。1 TiB 的 `nt` 若跨过保留期，后续文件会 404：
工具会明确报错，重跑会落到新快照、抢救仍然一致的卷、其余重下。

### D8. 退出码分别是什么？

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 用法/运行错误（含"拒绝安装"以外的失败） |
| 2 | 命令行参数错误（argparse，例如子命令参数放错位置） |
| 3 | **校验失败，已拒绝安装**（`current` 未改动） |
| 4 | 检测到更新竞态（torn）并中止 |
| 130 | 被 Ctrl-C 中断 |

---

## E. 空间与配额

### E1. `df` 显示空间够，为什么还是失败？

两个常见原因：

1. **用户配额**远小于 `df` 的可用量（并行文件系统、容器叠层很常见）→ 先跑 `doctor`；
2. **峰值不等于下载量**：NCBI 源是"压缩归档 → 解压落盘"，峰值 = **载荷 + 一个归档**
   （`nt` ≈ 1.07 TiB + 0.73–4.78 GiB；`--keep-archives` 则 ≈ 1.9 TiB）。

### E2. `nt` 到底要多少空间？

| 项 | 值（实测 2026-09-24） |
|---|---|
| 需下载（NCBI 压缩） | 932 GiB |
| 落盘（解压后） | 1.07 TiB |
| 峰值（提取模式） | 载荷 + 单卷（0.73–4.78 GiB） |
| 峰值（GCP 裸索引，不解压） | ≈1.0 TiB |
| 峰值（`--keep-archives`） | ≈1.9 TiB |

`--dry-run` 会直接把估算与当前可用空间打印出来。

### E3. 空间不够/临时紧张怎么办？

- 换 `-s gcp`：**不需要解压**，峰值只有载荷本身；
- `--min-free 50G`：可用空间跌破下限立即停（**已下内容全部保留**），而不是把盘写满再报一堆失败；
- 清理旧快照：`list` 看占用 → `gc --keep 2`（或先 `gc --dry-run`）；
- 快照之间是硬链接，真正额外占盘只有"变动过的文件"。

### E4. `quota` 命令没装怎么办？

`apt install quota`（Debian/Ubuntu）或 `yum install quota`（RHEL/CentOS）；Lustre 用 `lfs`，XFS 用 `xfs_quota`。
没装时工具只看 `df`，并在 `doctor` 里明确提示"并行文件系统上配额可能远小于可用空间"。

---

## F. 校验与配合 BLAST 使用

### F1. 怎么确认下载的东西没问题？

```bash
blastdb_download.py -r /data/db verify            # 全量 md5 + 集合指纹 + 完整性（离线）
blastdb_download.py -r /data/db verify --quick    # 只查大小 + 头尾指纹（秒级）
blastdb_download.py -r /data/db verify --against-remote   # 顺便看上游有没有新版
```

`verify` 依据的是安装时逐文件**证明过**的 md5、来源与来源归档（状态文件），所以完全不需要联网。

### F2. `blastdbcmd` / `blastdbcheck` 报 `mdb_env_open: MDB_INVALID: File is not an LMDB file`

这是**本地 BLAST 构建与 NCBI 的 LMDB 布局不兼容**，与下载正确性无关：
NCBI 官方 `tar.gz` 手工解压后同样报错，而本地 `makeblastdb` 建的库正常。
所以 `--smoke-test auto`（默认）只告警不阻断；权威判据是 md5 证据 + `inspect`/`verify`。
换一个能读 NCBI LMDB 的 BLAST 版本即可。

### F3. `blastdbcmd -outfmt "%S"` 显示 `N/A`？

物种名来自 `taxdb`，它必须和数据库**在同一个 `BLASTDB` 目录**里。云端源默认自动附加；
NCBI 也单独发布 `taxdb.tar.gz`（且部分归档自带）。用 `ls $BLASTDB` 确认有
`taxdb.btd` / `taxdb.bti` / `taxonomy4blast.sqlite3`。

### F4. 怎么检查一个已有的（别人的/手工下的）目录有没有混装？

```bash
blastdb_download.py inspect nt --dir /data/public/databases/NT
blastdb_download.py --json inspect nt --dir /data/public/databases/NT
```

不需要状态文件、不需要 BLAST、不联网。能抓：混装、卷号缺口、缺文件/多文件、被截断、
载荷与元数据不同构建。示例输出见 [`../README.md`](../README.md) 的"校验已有镜像"。

### F5. BLAST 怎么指向下载好的库？

```bash
export BLASTDB=/data/public/databases/current
blastn -db nt -query q.fa -out out.txt
```

也可以直接给路径：`blastdbcmd -db /data/public/databases/current/nt -info`。

### F6. 怎么知道我现在用得到底是哪一版数据？

- `blastdb_download.py list` → 当前快照名（含来源与日期）；
- `blastdb_download.py verify` → 每个库的 `rev` 与**内嵌构建时间**（例如 `build 2026-07-21T05:36`）；
- 状态文件里有逐库的 `source_snapshot` / `last_updated` / 每个文件的证明来源。

---

## G. 运维、迁移与回滚

### G1. 从 `update_blastdb.pl` 迁移要几步？

```bash
# 1) 给旧目录体检（判断是否混装）
blastdb_download.py inspect nt --dir /data/public/databases/NT
# 2) 建第一份不可变基线
blastdb_download.py -r /data/public/databases -s gcp -j 8 --log-file /var/log/base.log download nt taxdb
# 3) 切换 BLASTDB 到 <root>/current（此后旧目录只作备份）
```

旧目录里若有半成品要接着用，加 `--adopt <旧目录> --adopt-partial`。详见 [`OPERATIONS.md`](OPERATIONS.md)。

### G2. 怎么回滚？

`current` 是软链接，回滚是一次原子切换：

```bash
blastdb_download.py -r /data/db list              # 看快照
blastdb_download.py -r /data/db rollback --list
blastdb_download.py -r /data/db rollback --to 1   # 0 = 最新，1 = 上一个
blastdb_download.py -r /data/db verify            # 回滚后复核
```

用 `--keep-snapshots N`（默认 2）决定保留几份。

### G3. `gc` 会删什么？中断后能跑吗？

`gc` 会删**旧快照**和**整个 `.staging`**。
**中断之后不要立刻跑 `gc`**——那等于放弃断点；成功安装后 staging 本来就会被清（快照树自身就是续跑点）。
先 `gc --dry-run` 看清要删什么。

### G4. 能同时跑两个进程吗？

不能对同一个 `--root` 同时跑**写命令**：`download`/`repair`/`gc`/`rollback` 会取 `<root>/.lock`，
第二个进程会明确报错退出而不是互相破坏。只读命令（`list`/`verify`/`showall`/`inspect`/`staging`/`doctor`/`config`）不加锁，
可以在下载期间使用。

### G5. 定时更新怎么写？

cron 与 systemd 单元示例见 [`OPERATIONS.md`](OPERATIONS.md)。建议：

```bash
blastdb_download.py -r /data/db -s ncbi -j 8 \
    --log-file /var/log/blastdb/nt.log --keep-snapshots 2 --min-free 50G \
    download nt core_nt taxdb
```

跑完顺手 `verify --quick`，有问题再 `repair`。

### G6. 多份快照会不会把磁盘翻几倍？

不会。快照之间用**硬链接**共享未变动的文件，只有真正变动的文件才额外占盘。
`list` 里显示的每份快照大小是"按文件计"，会有重复计算；实际额外占用很小。
