[TOC]

# NCBI BLAST 预格式化数据库：下载方式、一致性风险与检查方法

本文整理三种获取 NCBI BLAST 预格式化数据库（`nt` / `nr` / `core_nt` …）的方式，
说明**为什么"下载下来的文件前后版本不一致"会毁掉索引**，给出**可落地的检查方法**，
并推荐用 [`blastdb_download.py`](../blastdb_download.py) 替代原来的手工流程（使用手册见 [`README.md`](../README.md)）。

- 文中带「实测」标记的结论，都是在 2026-09-23 用真实 NCBI/GCP/S3 接口核验过的。
- 带「原始笔记」的是已有记录的整理，已就地补充精确说明。
- 带「⚠️ 纠正」的是核验后发现与事实不符、需要修正的说法。

---

## 0. 结论速览

| 方式 | 并行 | 版本一致性 | 校验强度 | 能否续跑 | 适用 |
|---|---|---|---|---|---|
| `update_blastdb.pl`（NCBI 源） | ✗（`--num_threads` 对 NCBI 源无效） | **无保证**（实时目录原地替换，只逐文件校验 md5） | 单文件 md5 | ✗（重跑重下） | 官方脚本，能用但有一致性风险 |
| `update_blastdb.pl --source gcp/aws` | ✓ | **快照级一致**（`latest-dir` 指向的目录不可变） | **完全不校验** | ✗ | 想要"某一天的完整快照" |
| 手写 `aria2c` 循环 | ✓ | **最差**（连一个版本点都没固定） | 单文件 md5 | ✗ | 应急/少量库 |
| **`blastdb_download.py`** | ✓（aria2c 可选） | **卷集合级一致**，下载前后各复核一次 | 权威 md5 + 内嵌构建指纹 + 元数据交叉校验 | ✓ 三层断点 | 生产环境推荐 |

一句话：**慢一点没关系，版本混了才致命**——混版不会报错，只会让比对结果悄悄出错。

---

## 0.5 数据源怎么选 + 最稳健的下载流程

> 本工具**不需要** `update_blastdb.pl`，也不需要 `gsutil`/`gcloud`/`aws`/`curl`/`tar`
> 这些外部程序——只依赖 Python 标准库（详见 [`README.md`](../README.md#第三方软件依赖哪些必须装)）。

三个源都有全部库（含 `taxdb`），差别在新鲜度、校验强度和空间/CPU 开销（2026-09-24 实测）：

| | NCBI HTTPS（`-s ncbi`） | GCP（`-s gcp`，默认） | AWS（`-s aws`） |
|---|---|---|---|
| 新鲜度 | **最新**（`nt` 发布 2026-09-15） | 快照 `2026-07-21-01-05-02`（滞后约 1–2 个月） | 同 GCP |
| 目录性质 | **原地可变**（发布期逐个替换） | 不可变快照 + `latest-dir` 指针 | 同 GCP |
| 布局 | `*.tar.gz` + 每文件 `.md5` | 裸索引文件，**无需解压** | 同 GCP |
| 逐文件校验 | ✓ `.md5` 边车 | ✓ 对象 `md5Hash` | ⚠ 大对象 ETag 是分片哈希，**不是 md5** |
| 下载量 / 峰值磁盘（`nt`） | 932 GiB / 载荷 1.07 TiB + 单卷 0.73–4.78 GiB | ≈1.0 TiB / ≈1.0 TiB（无解压） | 同 GCP |
| 主要风险 | 发布窗口内可能前后不一致（工具会重试或中止） | 数据滞后；快照只保留约 3 个（≈1 个月） | 校验强度低；快照保留期同 |

选择建议：**只求稳 → 默认 `-s gcp`；要最新 → `-s ncbi`（一致性协议专为可变目录设计）；
`-s aws` 仅在通道/凭证有优势且能接受校验降级时用**。想两者兼得：先 `-s gcp` 建不可变基线，之后按需
`-s ncbi` 追新（未变动文件会硬链接复用）。

最稳健的下载流程（完整说明见 [`README.md`](../README.md#最稳健的下载流程推荐照抄)）：

```bash
ROOT=/data/public/databases
blastdb_download.py -r "$ROOT" doctor                      # 空间/配额/工具自检
blastdb_download.py -r "$ROOT" -s gcp --dry-run download nt taxdb   # 空跑看计划
nohup blastdb_download.py -r "$ROOT" -s gcp -j 8 --connections 4 \
      --min-free 50G --file-retries 3 --keep-snapshots 2 \
      --log-file /var/log/blastdb/nt.log download nt taxdb &
blastdb_download.py -r "$ROOT" verify                       # 完成后全量复核
export BLASTDB="$ROOT/current"
```

> 注意云快照**只保留约 3 个（≈1 个月）**。1 TiB 的 `nt` 若跨过保留期，后续文件会 404；
> 工具会明确报错，重跑会落到新快照并抢救仍一致的卷。

## 1. 正规下载方式：`update_blastdb.pl`

### 1.1 基本用法（原始笔记）

```bash
# GCP 源上可用的数据库及其更新时间
update_blastdb.pl --showall pretty --source gcp
# 默认下载 NCBI 的 FTP 站点
update_blastdb.pl --decompress --num_threads 8 nt
# 手动指定 GCP 源
update_blastdb.pl --source gcp --decompress nt
```

### 1.2 参数补充说明（源码级核对）

| 参数 | 真实行为 |
|---|---|
| `--source {ncbi,gcp,aws}` | 不写则自动探测（先试探 GCP/AWS 的 metadata 服务，探测不到就用 NCBI） |
| `--decompress` | **仅对 `--source ncbi` 有效**（POD 原文："only applicable when the download source is ncbi"）。所以 `--source gcp --decompress nt` 里的 `--decompress` 是空操作 |
| `--num_threads N` | **只对云端源生效**（POD 原文："to perform downloads in parallel when data comes from the cloud"）。所以 `--decompress --num_threads 8 nt`（NCBI 源）**并不会并行** |
| `--showall [tsv\|pretty]` | 读的是同一个 manifest；`pretty` 会多打表头与 GB 数 |
| `--force` | 忽略本地 mtime，强制重下 |
| `--legacy_exit_code` | 恢复旧退出码语义：0 成功无下载 / 1 成功有下载 / 2 出错 |
| `--force_ftp` + `--passive` | 走真正的 FTP 协议（默认走 HTTPS，作者注释说 HTTPS 更稳） |
| `--blastdb_version` | ⚠️ **POD 里写了但这个选项在脚本里根本没有实现**（`GetOptions` 里没有它）。别被文档误导 |

### 1.3 脚本内部做了什么（源码级事实）

1. **NCBI 源**：读 `https://ftp.ncbi.nlm.nih.gov/blast/db/blastdb-metadata-1-1.json`，
   按你给的库名匹配文件，下载 `X.tar.gz` 与 `X.tar.gz.md5`，逐个比对 md5；只有本地文件比远端旧才下。
2. **云端源（GCP/AWS）**：读桶里的 `latest-dir` 得到快照目录名，再读该目录下的 manifest，
   直接下载**裸索引文件**（不是 tar.gz），**全程不做任何校验**。
3. 新鲜度判断靠 HTTP `Last-Modified` 与本地 mtime 比较。
4. Google/Amazon 上还分别支持 `gsutil` / `aws s3 cp` 加速（`--num_threads` 就是喂给它们的）。

### 1.4 它的短板

- **只是一致性的最小单位**：逐个文件的 md5 都通过，但**卷与卷之间可能来自不同构建**——这是本文的核心风险，详见第 3 节。
- **云端源零校验**：GCS/S3 分支没有任何 md5 检查（GCS 对象其实自带 `md5Hash`，S3 对分段上传的 ETag 不是 md5）。
- **不能续跑**：中断后重跑，已下载的完整文件除非 mtime 判定命中，否则重下；tar.gz 场景下 curl 也不做分片续传。
- **原地覆盖**：更新过程中目录里可能短暂出现新旧混装，任何同时运行的 `blastn` 都可能读到不一致的库。

---

## 2. 非正规方式：手写 `aria2c` 循环

### 2.1 你的脚本（原始笔记，原样保留）

```bash
#!/bin/bash
#https://ftp.ncbi.nlm.nih.gov/blast/db/
#2024-03-20  135
#2026-09-07 376
for i in {000..376}
do
    aria2c -c https://ftp.ncbi.nlm.nih.gov/blast/db/nt.${i}.tar.gz
    aria2c -c https://ftp.ncbi.nlm.nih.gov/blast/db/nt.${i}.tar.gz.md5
    md5sum -c nt.${i}.tar.gz.md5 && tar -zxvf nt.${i}.tar.gz && rm -rf nt.${i}.tar.gz echo "nt.${i} has done."
done
```

### 2.2 实测结论：它做到了什么、没做到什么

做到的：

- 每个卷都下了 `.md5` 并 `md5sum -c` 校验 → **单卷内部不会坏**（`&&` 也顺带挡住了坏包解压）。
- 天然可以多进程并行、可以断点重跑（`aria2c -c`）。

没做到的（关键）：

- **没有"版本点"**。循环从第 0 卷跑到第 376 卷可能跨越几小时到几天，而 NCBI 是**实时原地替换**的；
  官网恰好在这期间发新版，就会得到"第 0…120 卷是新版、其余是旧版"这种**混装集合**。
- 连"开始下载前先固定一次版本"这一步都没有，所以前后差异可以远超一天。

### 2.3 潜在问题（原始笔记的结论，已确认）

> ncbi 的网页端持续更新文件，导致下载的文件前后版本不一致；导致索引有问题。

这是准确的。值得强调后果：**混装不会让 BLAST 报错**，`blastn` 照常运行、照常输出结果，
只是结果可能错——这比"下载失败"危险得多。

### 2.4 脚本本身的两个小毛病

1. `rm -rf nt.${i}.tar.gz echo "nt.${i} has done."` —— 少了分隔符：`echo` 和那句提示被当成
   `rm` 的**文件名参数**，所以提示永远不会打印（而且会在当前目录找名为 `echo` 的文件）。
   应该是 `rm -f nt.${i}.tar.gz; echo "nt.${i} has done."`。
2. 没有 `set -e`、没有失败记录：任一 `aria2c` 失败后循环继续，最后你无法判断哪些卷缺了。
   加 `|| echo "FAIL $i" >> failed.txt` 之类才有可追溯性。

---

## 3. 为什么会不一致：三条硬事实

### 3.1 NCBI 在线目录是"原地可变"的

`https://ftp.ncbi.nlm.nih.gov/blast/db/` 发布新版时逐个替换文件。`nt` 有 383 卷、`nr` 有 176 卷
（实测 2026-09-23 的 manifest：`nt` 383 卷、`nr` 176 卷、`core_nt` 91 卷）。
更新窗口跨越数小时，期间任何"边下边比"的方案都会撕裂。

### 3.2 云端桶是"快照目录 + 指针"，只有指针可信（实测）

```
latest-dir               -> 2026-07-21-01-05-02      ← 唯一权威指针
2026-07-21-01-05-02/     ← 完整快照（10168 个对象，裸索引文件，非 tar.gz）
2026-09-19-01-05-02/     ← 有 blastdb-metadata-1-1.json
2026-09-22-01-05-02/     ← 目录名更新，但 blastdb-metadata-1-1.json 仍是 404（正在灌数）
```

2016-09-23 实测：`latest-dir` 仍指向 `2026-07-21-01-05-02`，而桶里已存在 `2026-09-22-01-05-02/`，
**该目录缺少 manifest**。任何"挑名字最新的目录"的逻辑都会下到一套残缺的库。

⚠️ 纠正：原始笔记说"`update_blastdb.pl` 有单独的快照下载网址，版本只旧 1 个月；这个命令是
同步全部数据到特定时期的文件"——**只有加 `--source gcp/aws` 时前半句才成立**；
不加 `--source` 时它下的是 NCBI 实时目录，**并不固定在某个时期**。所以默认用法下它并没有
"同步到特定时期"的能力。

### 3.3 每个卷内部都带"构建指纹"（这是最有用的判据）

卷索引文件 `.nin`（核酸）/ `.pin`（蛋白）的头部是定长结构：

```
u32 version(5) | u32 dbtype(0=核酸,1=蛋白) | u32 卷序号 | 标题 | 卷基名 | 构建时间戳
```

实测（HTTP Range 读取云端对象前 512 字节）：

| 文件 | 内嵌卷序号 | 内嵌构建时间 |
|---|---|---|
| `nt.000.nin` / `nt.001.nin` / `nt.002.nin` / `nt.175.nin` | 0 / 1 / 2 / 175 | 全部 `Jul 19, 2026  3:10 AM` |
| `core_nt.00.nin` / `core_nt.01.nin` | 0 / 1 | 全部 `Jul 18, 2026  1:17 AM` |
| `nr.000.pin` / `nr.001.pin` | 0 / 1 | 全部 `Jul 14, 2026 12:42 AM` |

并且 `nt.njs` 里的 `"last-updated": "2026-07-19T03:10:00"` 与之严格一致；
`<db>-nucl-metadata.json` 里的 `last-updated` 也一致（日期级）。

> **推论**：两个卷属于同一次构建 ⇔ 它们的内嵌构建时间戳相同。
> 这个判断完全在本地完成，不需要联网，也不依赖任何一方元数据的自述。

---

## 4. 索引不一致的检查方法

### 4.1 快速抽样：`blastdbcmd`（原始笔记 + 补充）

```
#格式符	含义	说明
#%a	Accession	序列的登录号，如 XM_066217556.1、XR_010627017.1
#%o	OID	序列在 BLAST 数据库中的内部编号（数字）
#%T	TaxID	NCBI 分类学编号，如 9606 代表人类
#%S	Scientific Name	物种的科学名称，如 Homo sapiens；若 taxdb 未正确加载则显示 N/A

blastdbcmd -db /data/public/databases/NT/nt -entry XM_066217556.1 -outfmt "%a %o %T %S"
# 不一致内容
XR_010627017.1 99541112 58331 N/A
```

补充：

- **`taxdb` 从哪来（三个源都有，不需要为了它换源）**：GCP/AWS 把 `taxdb` 作为清单里的一等库
  （3 个裸对象，带 `md5Hash`）；NCBI 单独发布 `taxdb.tar.gz`（实测 62.34 MiB 压缩 / 293.9 MiB 解压，
  2026-09-16 更新），并且**实测单卷库的归档里也自带同一份载荷**（多卷库是否自带无官方说明）。
  本工具**默认会自动附加 `taxdb`（源不限）**，`--no-taxdb` 可关；因此
  `-s ncbi` 与 taxdb 无关——它是为了取最新数据。
- `%S` 为 `N/A` 说明 **BLASTDB 目录里没有 `taxdb`**（需要 `taxdb.btd` / `taxdb.bti` /
  `taxonomy4blast.sqlite3` 与数据库在同一目录）。`%a`/`%o`/`%T` 仍能读出来，说明数据本身没坏。
- 这类抽查**只能查你恰好抽到的序列**；它能暴露"某条序列的 OID/物种对不上"，
  但对"整体是不是同一次构建"基本没有统计功效。适合当回归检查，不适合当一致性判据。
- 换 `-entry` 为随机登录号、或直接 `blastdbcmd -db X -info` 看
  `Date:`（构建时间）与 `Volumes:` 是否符合预期，信息量更大。

### 4.2 ISAM 抽样：`blastdbcheck`（原始笔记 + 实测警告）

```bash
blastdbcheck -db nt -dbtype nucl -verbosity 2      # 汇总
blastdbcheck -db nt -dbtype nucl -verbosity 3      # 详情
```

真实参数（`blastdbcheck -h`）：`-db`、`-dbtype {guess,nucl,prot}`、`-dir`、`-recursive`、
`-verbosity 0..4`、`-full`、`-stride`、`-random`（默认随机抽 200 个 OID）、`-ends`、
`-no_isam`、`-legacy`、`-must_have_taxids`、`-cdd_delta`。

它默认做的事（实测输出）：

```
ISAM testing is ENABLED.
Legacy testing is DISABLED.
TaxID testing is DISABLED.
By default, testing 200 randomly sampled OIDs.
```

⚠️ **本机实测的重要警告**：`conda install -c bioconda blast` 装的 **BLAST 2.17.0 读不了
NCBI 预格式化库**：

```
[ERROR] caught exception in .../16S_ribosomal_RNA
NCBI C++ Exception:
  ... BLASTDB::ncbi::CBlastLMDBManager::CBlastEnv::CBlastEnv() - LMDB runtime error:
      mdb_env_open: MDB_INVALID: File is not an LMDB file
 Result=FAILURE. 1 errors reported in 1 volume(s).
```

关键点：**这是干净镜像也会出现的失败**（我用 GCP 快照下载的 `16S_ribosomal_RNA`，
md5 与官方 `md5Hash` 完全一致，同样 FAILURE），而且 **NCBI 官方 tar.gz 手工解压后也一样**，
但本地 `makeblastdb` 自建的库正常 → 属于**客户端 BLAST/LMDB 构建与 NCBI 写出的布局不兼容**，
不是下载问题。

结论：**在当前 conda 环境里，`blastdbcheck` 不能用作一致性判据**（它对任何 NCBI 库都报错，
分不清"混装"和"读不了"）。要让它可用，需要换成能读 NCBI LMDB 的 BLAST 版本。

### 4.3 结构指纹检查（推荐；不需要 BLAST、不需要联网）

这是唯一在**任何环境**都能给出可信答案的方法：直接读每个卷 `.nin`/`.pin` 头部。

用 `blastdb_download.py` 一条命令即可（可对**任意**已有目录使用，包括上面那个 aria2c 循环下出来的目录）：

```bash
python3 blastdb_download.py inspect nt --dir /data/public/databases/NT
```

正常输出：

```
nt  (/data/public/databases/NT)
  files              : 3452
  volume indexes     : 345 (ordinals 0..344)
  embedded build date: 2026-07-19T03:10 (all volumes agree)
  verdict            : OK
```

混装输出（这正是手工循环最可能的结果）：

```
nt  (/data/public/databases/NT)
  volume indexes     : 345 (ordinals 0..344)
  embedded build date: MIXED -> 2026-07-19T03:10: nt.000.nin, nt.001.nin ...
                              2026-09-16T13:46: nt.344.nin
  verdict            : FAILED
      MIXED BUILD TIMESTAMPS across volumes - this file set is torn
```

它同时能发现：缺卷（卷序号不连续）、文件名卷号与内嵌卷号不符、缺少/多余载荷文件、
载荷文件被截断、载荷与其 metadata 来自不同构建。

### 4.4 完整性交叉检查：`<db>-nucl-metadata.json`（实测可用）

镜像里与数据并排放着 `<db>-nucl-metadata.json`，其中 `files` 列表、`number-of-volumes`、
`bytes-total` 三项与磁盘上的载荷**完全吻合**（实测 `16S_ribosomal_RNA`：
`bytes-total=18427887` 等于 11 个载荷文件字节数之和；`LSU_prokaryote_rRNA`：
`bytes-total=4064539` 同样精确相等）。

因此它是一条**离线完整性判据**：

```bash
# 用 jq 手工比一下（示例）
jq -r '.files[], ."bytes-total", ."last-updated"' 16S_ribosomal_RNA-nucl-metadata.json
```

`blastdb_download.py` 的 `inspect` / `verify` 已经内置了这三项检查（缺文件、多文件、
字节数不符、时间戳与卷指纹不一致都会报错）。实测四种破坏方式都能被抓出来：

| 破坏方式 | `inspect` 的判定 |
|---|---|
| 把 `.nin` 换成另一版本 | `last-updated` 与卷指纹日期不一致 → FAILED（并顺便报出字节数不符） |
| 删掉 `.nsq` | `lists 1 file(s) that are absent: ...` → FAILED |
| 把 `.nsq` 截断一半 | `bytes-total 18427887 != the 13356971 bytes of payload on disk` → FAILED |
| 多放一个杂文件 | `does not list 1 file(s) that are present: ...` → FAILED |

### 4.5 推荐的检查组合

```bash
# 1) 结构指纹 + 完整性（离线，最可靠）—— 每次更新后、以及怀疑数据时
python3 blastdb_download.py verify                    # 本工具装的镜像：含全量 md5
python3 blastdb_download.py inspect nt --dir /path    # 别人的/手工下的目录

# 2) 若有能读 NCBI LMDB 的 BLAST：抽样回归
blastdbcmd  -db nt -info
blastdbcheck -db nt -dbtype nucl -verbosity 2 -random 200

# 3) 业务层面的抽查（你自己脚本里的那两条）
blastdbcmd -db nt -entry XM_066217556.1 -outfmt "%a %o %T %S"
```

---

## 5. 推荐：`blastdb_download.py`

### 5.1 最小用法

```bash
# 看有什么
python3 blastdb_download.py -r /data/public/databases showall --format pretty

# 下载/更新（默认 GCP 不可变快照；要最新数据加 -s ncbi）
python3 blastdb_download.py -r /data/public/databases -s ncbi -j 8 -x 4 download nt taxdb

# 指给 BLAST
export BLASTDB=/data/public/databases/current
blastn -db nt -query q.fa -out out.txt
```

### 5.2 它如何消除本文列出的每一类风险

| 风险 | 处理方式 |
|---|---|
| 卷之间混版（3.1） | 下载前记下 `revkey`，下载后再读一次；变了就丢弃重试或中止（**绝不安装**）。安装前再校验所有卷的内嵌构建时间戳一致、卷序号 `0..N-1` 连续 |
| 选错云端快照（3.2） | 只用 `latest-dir`，并要求目标快照的 manifest 存在、清单里每个文件都在快照里 |
| 单文件损坏 | 权威 md5：NCBI 用 `.md5` 边车，GCS 用 `md5Hash`；aria2c 模式下用 `checksum=md5=` 在下载内校验，失败文件被删除 |
| 缺文件/多文件/截断（4.4） | `<db>-nucl-metadata.json` 的 `files` 与 `bytes-total` 交叉校验 |
| 更新时读到半套数据 | 新快照目录 + **原子替换 `current` 软链接**；读端永不见半更新，回滚是一次软链接切换 |
| 中断后重头再来 | 三层续跑：分片状态 → staging 复用 → 快照硬链接；staging 按 `revkey` 隔离，旧断点不可能污染新版本 |
| 无法判断老目录是否有问题 | `inspect` 直接读 `.nin` 指纹，不用 BLAST、不用状态文件 |

### 5.3 与现有方式对照

```
# 原来的手工循环
for i in {000..376}; do aria2c -c .../nt.${i}.tar.gz; ...; done

# 换成（并行、可续跑、集合级一致性校验、原子切换）
python3 blastdb_download.py -r /data/public/databases -s ncbi -j 8 -x 4 download nt
```

### 5.4 迁移现有目录

工具以 `<root>/current` 软链接对外提供服务，因此迁移只需：

```bash
# 1) 先体检旧目录：能不能用它的 .nin 指纹一眼看出来
python3 blastdb_download.py inspect nt --dir /data/public/databases/NT

# 2) 若旧目录被判 FAILED（混装），直接用新工具重建一份；若是 OK，也可以只把新快照建起来
python3 blastdb_download.py -r /data/public/databases -s gcp -j 8 download nt taxdb

# 3) 切换 BLASTDB 指向 current
export BLASTDB=/data/public/databases/current
```

旧目录可以保留一段时间做对照；新快照内的文件用硬链接复用，重复数据不会真的占两份空间。

---

## 6. 附：数据源地址

| 位置 | 地址 | 特点 |
|---|---|---|
| NCBI FTP/HTTPS | `https://ftp.ncbi.nlm.nih.gov/blast/db/` | **最新**；tar.gz + `.md5` 边车；目录原地可变 |
| GCP | `https://storage.googleapis.com/blast-db/` | 快照目录 + `latest-dir`；对象自带 `md5Hash`；实测滞后约 1 个月 |
| AWS | `https://s3.amazonaws.com/ncbi-blast-databases/` | 同结构；大对象 ETag 是分段哈希，**不是 md5** |
| manifest（清单） | `<源>/blastdb-metadata-1-1.json` | 每个库的描述、文件列表、时间、卷数 |
| 云端指针 | `<源>/latest-dir` | 指向当前权威快照目录；**唯一可信的目录选择依据** |

---

## 6.5 附：本工具自身的版本与更新日志

工具内部版本用 `blastdb_download.py --version` 查看；完整更新日志见
[`CHANGELOG.md`](../CHANGELOG.md)。与本文相关的两次修复值得记住：

- **1.1.0 修**：`taxdb` 曾被误报 "the snapshot is incomplete"——因为按库名前缀列对象会漏掉
  `taxonomy4blast.sqlite3`（它不以 `taxdb` 开头）。现在清单是权威，前缀只是快路径。
- **1.2.0 修**：没有官方 md5/size 的文件不再"仅凭存在"被当作已验证；`Ctrl-C`/中止会立即停止，
  不再把队列里已排队的几百个分片跑完。

## 7. 附：环境与版本

```
# BLAST（注意本文 4.2 的 LMDB 兼容性警告）
conda install -c bioconda blast
blastdbcmd -version      # 本机实测：blast 2.17.0, build Aug 11 2025

# 本文核验时间与观测值
#   核验日期       : 2026-09-23
#   latest-dir     : 2026-07-21-01-05-02（AWS 与 GCS 一致）
#   云端快照对象数  : 10168；其中 -metadata.json 41 个
#   NCBI 实时 manifest：39 个库、1389 个载荷 URL，全部位于 /blast/db
#   nt / nr / core_nt 卷数：383 / 176 / 91
#   实测内嵌构建时间示例：nt = Jul 19, 2026  3:10 AM；core_nt = Jul 18, 2026  1:17 AM
```
