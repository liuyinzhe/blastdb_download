# 设计说明（维护者参考）

本文说明 `blastdb_download.py` 的内部约定：为什么这样设计、有哪些不变量、状态文件长什么样、
以及要扩展时该动哪里。面向长期维护者；面向使用者的内容见 [`../README.md`](../README.md)。

---

## 1. 唯一的核心目标

> **安装的每套库必须是"一次完整构建"，且在安装前被证明过；做不到就拒绝安装。**

其余一切（并行、续跑、日志、配额）都是围绕这条目标的服务。判断一个改动是否合格，先问：
它会不会让"无法证明"的东西被当成"已验证"？会，就不合格。

---

## 2. 数据模型

```
RemoteFile  : name, url, size, md5, md5_origin, token, role
              role ∈ {payload, archive, metadata}
DbTarget    : dbname, dbtype, source_key, snapshot_id, files[], archives[],
              number_of_volumes, bytes_total, bytes_compressed, last_updated
              signature() -> 规范化文本；revkey() = sha1(signature())[:16]
```

`signature()` 包含：来源、快照、库名、类型、`last-updated`、卷数、以及**每个文件的
`名字:md5(或 size):token`**（按名字排序）。因此：

- 同一内容 → 同一 `revkey`（幂等，重复运行不做无用功）；
- 内容变化（哪怕只有一个小文件）→ 不同 `revkey`（staging 隔离、快照命名、树指纹都随之变化）。

`revkey` 是**整个协议的地基**：

1. staging 目录按 `revkey` 命名 → 断点绝不能跨修订续传；
2. 下载前后各读一次 `revkey` → 不一致即"官网在发版"，重试或中止；
3. 快照树指纹 = 所有库 `revkey` 的哈希 → 判断"这次要建的东西是不是已经有了"。

---

## 3. 权威来源矩阵（每项检查靠什么作数、能抓什么）

| 检查 | 依据（权威） | 能抓到 | 代码 |
|---|---|---|---|
| 单文件内容 | NCBI `.md5` 边车 / GCS `md5Hash` / S3 单段 ETag；否则 size + 内嵌指纹 | 截断、损坏、错版本的单文件 | `verify_local_file` / `verify_fetched_file` |
| 身份可证明性 | **必须有 md5**（`identity_provable()`） | "同大小不等于同内容"（1.3.1 修的真 bug） | 复用/抢救/接管三处 |
| 卷序完整性 | 文件名卷号 + `.nin/.pin` 内嵌卷号，必须 `0..N-1` 连续且一致 | 缺卷、错位、少下了一卷 | `verify_table` |
| 同一次构建 | 所有卷内嵌构建时间戳必须**完全一致** | 跨卷混装（最危险的一类） | `verify_table` |
| 库自述一致性 | `<db>.njs` 的 `last-updated` / `number-of-volumes` 与卷指纹一致 | 半更新、清单与内容错配 | `verify_table` |
| 载荷清单与体积 | `<db>-<type>-metadata.json` 的 `files` / `bytes-total` / `number-of-volumes` / `last-updated` | 缺文件、多文件、被截断、元数据与载荷不同构建 | `check_database_metadata` |
| 载荷归属 | 文件名必须属于该库（`belongs_to_database`），共享 taxonomy 除外 | CDN 返错对象、清单被截断、归档没解出来 | `verify_table` |
| 各卷文件种类一致 | 各卷扩展名集合必须一致（最低卷可多带共享 blob） | 某一卷少了 `.nsq` 之类 | `check_payload_uniformity` |
| 共享载荷冲突 | 同名不同内容时：有独立 `taxdb` 用它，否则按构建时间取新 | 隐式"后解压覆盖先解压" | `merge_tables` |

**明确的取舍**：没有 md5 的文件（目前只有 NCBI 的 `<db>-<type>-metadata.json`，~500 B）
不参与任何复用，只重新下载。理由见 §3 第二行。

---

## 4. 磁盘布局与状态文件

```
<root>/
  current -> <snapshot>                 # 原子切换的软链接
  <snapshot>/                           # 一次"内容快照"，名 = <source>-<日期>-<指纹8>
    .blastdb-download.json              # 状态文件（见下）
    nt.000.nhr … taxdb.bti …            # 库文件（跨快照硬链接，几乎不重复占盘）
  .staging/<db>/<revkey>/               # 下载暂存（隔离单位 = 库 + 修订）
    <file>.blparts.json                 # 内置下载器分片状态
    <file>.aria2                        # aria2 断点控制文件
    extracted/                          # 解压产物
    extracted/.receipt.json             # 解压回执：归档 → 成员(名字/大小/md5)
  .lock                                 # 写操作互斥（flock，只含持锁进程信息）
  log                                   # 写操作默认日志
```

状态文件（`STATE_FORMAT`，当前 1）：

```jsonc
{
  "tool": "blastdb_download.py", "tool_version": "1.4.0", "format": 1,
  "created": "2026-09-24T03:00:00Z", "source": "gcp",
  "source_snapshot": "2026-07-21-01-05-02", "snapshot": "gcp-2026-07-21-01-05-02-cc41a566",
  "previous": "…", "build_date": "2026-07-21", "tree_fingerprint": "…", "content_hash": "…",
  "dbs": {
    "16S_ribosomal_RNA": {
      "revkey": "983496c7c2958465", "source_key": "gcp", "source_snapshot": "…",
      "verification": "md5",              // md5 | mixed  ← 诚实标注校验强度
      "last_updated": "2026-07-21T00:00:00", "number_of_volumes": 1,
      "files": {
        "16S_ribosomal_RNA.ndb": {
          "size": 1478656, "md5": "…", "md5_origin": "gcs-md5Hash",
          "token": "1787598080625498",     // GCS generation / S3 ETag
          "role": "payload",
          "probe": {"size": …, "head": "…", "tail": "…"},   // 复用时先看它
          "archive": "…", "archive_md5": "…",               // 来自哪个归档
          "adopted_from": "/data/old/…"                      // 接管来源（可追溯）
        }
      },
      "shared_files": {"taxdb.btd": "taxdb"}                 // 由别的库提供
    }
  }
}
```

`verify` 完全离线就靠它：每个字节的**证明来源**都写在里面。

---

## 5. 五层复用（顺序即优先级）

`fetch_target()` 中的顺序不能随意调整：

1. **已安装快照**（`split_reuse`）：远端 md5 == 记录 md5，且本地仍通过 `--reuse-verify`
   （默认 probe：大小 + 头尾各 64 KiB）→ 硬链接。
2. **解压回执**（`remembered_members`）：上次已解压的归档，成员按回执里的 size+md5 逐一复核 → 复用。
3. **跨修订抢救**（`salvage_other_revisions`）：同名文件在**别的 revkey** 目录里且 md5 一致 →
   硬链接进当前 staging；随后删掉旧目录（"保留一致的、删除不配套的"）。**要求 md5。**
4. **外部接管**（`adopt_complete_and_partial` / `adopt_extracted_payload`）：`--adopt` 目录里的
   完整文件（要求 md5）、半成品（要求 md5 + 有 `.aria2` 或"无空洞前缀"）、整套已解压载荷
   （要求：单一构建时间 + 与源当前发布一致 + 卷数吻合 + `.nin` 引用的 blob 存在 + 各卷种类一致）。
5. **同 staging 目录**（layer 2 快捷）：本次运行的 `revkey` 目录里已有的文件 → 复核后复用。

之后才是真正下载。任何一步"不通过"都只是**退回下载**，绝不降级为"当作通过"。

---

## 6. 失败分类与重试（`failure_policy`）

| 类别 | 触发词 | 行为 |
|---|---|---|
| `retry` | timeout、connection reset、http 403、http 5xx、host name resolution、md5/size mismatch、unknown | 整批重试，最多 `--file-retries`（默认 3），指数退避；遇 403/重置**自动降并发** |
| `fatal` | no space left、quota exceeded、permission denied、http 404、too many open files | 立即停止并给出建议，不浪费重试 |
| 空间看门狗 | 可用空间 < `--min-free`（默认 2 GiB，**已扣配额**） | 立即中止（SIGTERM→SIGKILL aria2 / 置 stop 事件），已下内容保留 |
| 撕裂 | 下载前后 `revkey` 不一致 | 丢弃 staging 重试（`--on-torn retry`）或中止（`fail`，退出码 4） |

注意顺序：`monitor.raise_if_tripped()` 在重试判断**之前**，避免"拼命重试但盘已满"。

---

## 7. 并发与锁

| 共享对象 | 保护 |
|---|---|
| `LOG`（文件 + stderr） | `Log._lock` |
| `Progress` 计数器 | `Progress._lock`；`render()` 内部先取值后输出，避免嵌套锁 |
| 每个下载文件的 fd / 分片集合 | `FilePlan.lock`、`BuiltinBackend._fd_lock` |
| 线程池 | 每轮一个 `ThreadPoolExecutor`；异常路径 `shutdown(wait=False, cancel_futures=True)` + 置 stop 事件（Ctrl-C 立即停） |
| 镜像根目录 | `RootLock`（`flock` 排他非阻塞）；`download/repair/gc/rollback` 加锁，只读命令不加 |
| Windows 无 `os.pwrite` | `write_at()` 回退 + 全局写锁 |

快照目录**建成后不可变**，这是"读端永不见半更新"的前提，也是 `verify`/`inspect` 可以在下载
进行时安全运行的原因。

---

## 8. 怎么扩展

**加数据源**：实现 `Source`：`resolve()`、`snapshot_id()`、`list_databases()`、
`revision(dbname, fresh=False)`、`invalidate()`。硬性要求只有一条：`revision()` 必须对同一内容
返回同一 `revkey`、对不同内容返回不同 `revkey`。若能提供逐文件 md5，请放在 `RemoteFile.md5`
（`md5_origin` 写明来源，`verification` 会如实汇报）。

**加校验**：优先使用**载荷自带**的证据（内嵌指纹、`bytes-total`、清单里的文件列表），
在 `verify_table()` / `check_database_metadata()` 里以"issue 字符串"形式追加，并加一条测试
（正例 + 反例）。不要引入新的远端声明作为权威。

**加下载引擎**：实现 `run(plans, jobs, connections, min_split, limit_rate, watchdog, progress)`，
返回 `{dest: {ok, error, from_log}}`。**校验必须在引擎之外做**（我们不信任引擎自述），
失败文件必须删除，进度要回灌 `Progress`。

**改状态格式**：`STATE_FORMAT` +1，并保证 `verify` 仍能读旧格式（否则在 CHANGELOG 写明需要重建）。

---

## 9. 不变量清单（改动时必须保持）

1. 无法证明的内容**永不**被当作已验证（宁拒绝、不混装）。
2. 复用/抢救/接管必须有权威 md5；probe 只用于"快速发现损坏"，不作为身份证明。
3. staging 按 `revkey` 隔离；跨修订续传在物理上不可能发生。
4. 安装成功前 `current` 不变；安装是"新目录 + 原子换链"。
5. 分片先落盘 `fsync`、再记账；状态文件声称完成的，磁盘上一定已有。
6. 失败的下载文件必须删除（aria2 会把坏文件留在磁盘上）。
7. 诊断走 stderr / 日志文件；stdout 只放数据。
8. 只有 Python 标准库；外部程序缺失必须优雅降级。
9. 每个 CLI 选项都必须在 README（中英）里出现，且真正进入生效配置。
10. 拒绝安装时，错误信息要写清**原因 + 建议动作**（否则运维无法处理）。

---

## 10. 实测事实与复现方法

维护时若怀疑上游变了，用下面的最小手段重新核对（都不需要下载完整库）：

```bash
# 云端权威指针 + 保留了几个快照
curl -s https://storage.googleapis.com/blast-db/latest-dir; echo
curl -s "https://storage.googleapis.com/storage/v1/b/blast-db/o?delimiter=/&maxResults=20&fields=prefixes"

# 清单（各库的卷数/大小/更新时间）
curl -sf https://ftp.ncbi.nlm.nih.gov/blast/db/blastdb-metadata-1-1.json | \
  python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d));print([x['dbname'] for x in d][:5])"

# 某个卷的构建指纹（前 512 字节足够：u32 version|type|ordinal|len + 标题 + blob 名 + 时间戳）
curl -s -r 0-160 "https://s3.amazonaws.com/ncbi-blast-databases/2026-07-21-01-05-02/nt.000.nin" | xxd | head -4
```

2026-09-24 的观测值（写进 README 的那批）：

| 事实 | 值 |
|---|---|
| `latest-dir`（AWS 与 GCS 一致） | `2026-07-21-01-05-02` |
| 云端保留的快照数 | **3 个**（AWS：`07-10/07-14/07-21`；GCS：`07-21/09-19/09-22`） |
| 云端快照对象数 | 10168，其中 `-metadata.json` 41 个 |
| NCBI 实时清单 | 39 个库、1389 个载荷 URL，全部位于 `/blast/db` |
| `nt` | 383 卷，载荷 1.070 TiB，压缩 932.5 GiB，单卷 0.73–4.78 GiB |
| `core_nt` | 91 卷，载荷 0.276 TiB |
| `taxdb` | 62.34 MiB 压缩 / 293.9 MiB 解压，`last-updated` 2026-09-16 |
| 内嵌构建时间示例 | `nt`=`Jul 19, 2026 3:10 AM`；`core_nt`=`Jul 18, 2026 1:17 AM`；`nr`=`Jul 14, 2026 12:42 AM` |
| 归档与逻辑内容的压缩比 | `nt.000.tar.gz` 4.78 GiB ← 逻辑内容 10.09 GiB（0.47），故"归档里是否另带 0.27 GiB taxonomy"无法靠算术判定 |
