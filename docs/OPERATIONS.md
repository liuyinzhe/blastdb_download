# 运维手册（部署、监控、排障、迁移）

面向"要长期维护一套镜像"的人。使用者手册见 [`../README.md`](../README.md) / [`../README.en.md`](../README.en.md)；
内部设计见 [`DESIGN.md`](DESIGN.md)。

---

## 1. 部署清单

```bash
# 1) 版本与自检
python3 blastdb_download.py --version
python3 blastdb_download.py -r /data/public/databases doctor

# 2) 空跑确认计划与空间（只读，不消耗空间）
python3 blastdb_download.py -r /data/public/databases -s gcp --dry-run download nt taxdb
```

`doctor` 要看的四行：

```
filesystem <dev> (<type>) free <X> of <Y>     ← 文件系统可用量
quota (<tool>) … remaining <R>                ← 用户配额（若有）；空间判断取 df 与它的较小值
not installed (all optional): …               ← 缺哪些可选工具（aria2c/quota 建议装）
built in: python standard library only …       ← 依赖确认：无 pip 包、无云端 CLI
```

**只装 Python 就能跑**；`aria2c` 与 `quota` 是最值得补的两个（前者提速，后者防"空间够却失败"）。

---

## 2. 容量与时间估算（实测基准）

| 库 | 卷数 | 需下载（NCBI 压缩） | 落盘（解压后） | 峰值磁盘（NCBI 提取模式） | 峰值磁盘（GCP 裸索引） |
|---|---|---|---|---|---|
| `nt` | 383 | 932 GiB | 1.07 TiB | 载荷 + 单卷（0.73–4.78 GiB） | ≈1.0 TiB（不解压） |
| `core_nt` | 91 | 240 GiB | 0.276 TiB | 载荷 + 单卷 | ≈0.28 TiB |
| `taxdb` | 1 | 62 MiB | 294 MiB | 同上 | 62 MiB |
| 小型库（16S 等） | 1 | 60–70 MiB | ≈19 MiB 索引 + 294 MiB taxonomy 载荷 | 载荷 + 单卷 | ≈19 MiB |

- `--keep-archives` 会把峰值变成 **载荷 + 全部归档**（`nt` ≈ 1.9 TiB）。
- 时间：实测 8 并发约 60 MiB/s（视线路而定），1 TiB ≈ 4.6 小时；解压额外占 CPU（`nt` 约十几分钟）。
- 快照之间用硬链接共享，日常增量只多占"真正变动的文件"；`--keep-snapshots` 控制保留数（默认 2）。

---

## 3. 定时更新

**cron**（简单可靠；工具自带镜像锁，外层 flock 只是双保险）

```bash
# /etc/cron.d/blastdb
30 3 * * *  blast  /usr/bin/flock -n /var/lock/blastdb.lock \
  /usr/bin/python3 /opt/blastdb_download.py \
    -r /data/public/databases -s ncbi -j 8 \
    --log-file /var/log/blastdb/nt.log \
    --keep-snapshots 2 --min-free 50G \
    download nt core_nt taxdb
```

**systemd**（更好的可观测性：`systemctl status`、`journalctl -u`）

```ini
# /etc/systemd/system/blastdb-update.service
[Unit]
Description=Sync pre-formatted BLAST databases
After=network-online.target

[Service]
Type=oneshot
User=blast
ExecStart=/usr/bin/python3 /opt/blastdb_download.py \
    -r /data/public/databases -s ncbi -j 8 \
    --log-file /var/log/blastdb/nt.log --min-free 50G \
    download nt core_nt taxdb
# 中断后 systemd 重启也会自动续跑（staging 保留）
TimeoutStartSec=infinity
Nice=10

# /etc/systemd/system/blastdb-update.timer
[Unit]
Description=Daily BLAST database sync
[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
[Install]
WantedBy=timers.target
```

**建议的收尾动作**：更新成功后跑一次 `verify --quick`（廉价）并记入日志：

```bash
python3 blastdb_download.py -r /data/public/databases verify --quick || \
  python3 blastdb_download.py -r /data/public/databases repair
```

---

## 4. 监控

长任务看日志文件，不要看 `nohup.out`（默认语义下它只剩告警）：

```bash
tail -f /var/log/blastdb/nt.log
```

一条健康日志长这样：

```
… ---- blastdb_download.py 1.4.0 started: -r … -s gcp -j 8 … download nt taxdb
… source: gcp:2026-07-21-01-05-02
… also fetching taxdb (62.34 MiB) so taxonomy lookups work; use --no-taxdb to skip it
… nt: fetching 3113 file(s) (1.00 TiB) with aria2c, 8 parallel
… [  1/3113] nt.000.nhd  26.0 MiB in 1s (25.3 MiB/s)
… nt:  12.4% 124.3 GiB/1,000.38 GiB 61.14 MiB/s files 45/3113 eta 4h05m
… assembling snapshot gcp-2026-07-21-01-05-02-1a2b3c4d (… files, 4 database(s))
… installed gcp-…; /data/public/databases/current -> gcp-…
… ---- finished
```

要看的关键行：`source:`（本次钉住的快照）、`fetching`（计划量）、`[i/N]`（逐文件）、
`installed`（原子切换完成）、`finished`。

**离线巡检脚本**（不需要网络，适合每天跑）：

```bash
python3 blastdb_download.py -r /data/public/databases verify --quick --json > /tmp/v.json
python3 - <<'EOF'
import json
d=json.load(open('/tmp/v.json'))
bad=[k for k,v in d["databases"].items() if v["status"]!="ok"]
print("snapshot:", d["snapshot"], "| problems:", bad or "none")
EOF
```

`verify --against-remote` 会额外告诉你上游是否已有更新（需要网络）。

---

## 5. 失败处置手册（按日志里的"原因分组"照做）

| 日志里的原因 | 含义 | 处置 |
|---|---|---|
| `no space left on the filesystem` | 盘满 | 腾空间；或换 `-s gcp`（不解压、峰值更低）；`--min-free` 让它更早停 |
| `filesystem quota exceeded` | 用户配额用完（`df` 可能仍显示很多） | `doctor` 看配额；申请提额；`lfs quota -u $USER <fs>` 复核 |
| `HTTP 403 forbidden` / `connection reset` | 被限流 | 让工具自动重试（默认会降并发）；或手工 `-j 4 -x 2` / `--limit-rate 50M` |
| `HTTP 404 not found` | 源正在发版（清单先于文件）或云快照被回收 | 等几分钟重跑；或 `-s gcp` 换不可变快照 |
| `md5 mismatch` / `size mismatch` | 传输被撕裂/损坏 | 工具已删坏文件并重试；反复出现则降并发、检查线路与代理 |
| `host name resolution failed` / `TLS certificate problem` | 网络/代理 | 检查 `HTTPS_PROXY`；自建镜像调试可临时 `--insecure` |
| `MIXED BUILD TIMESTAMPS` | 本地/远端集合前后不一致 | 工具**已拒绝安装**；重跑（会重新拉取）；若来自手工目录，用 `inspect` 定位 |
| `the source published a new revision` | 下载期间官网发版 | `--on-torn retry`（默认）会等待重试；`fail` 则退出码 4 |
| `transfer stopped: only X of usable space left` | 空间看门狗触发 | 已下内容全部保留；腾空间后重跑续传 |
| `another blastdb_download.py run is already using this mirror` | 并发写 | 等它结束；或用 `list`/`verify` 等只读命令 |
| `not an executable file`（`--aria2c`） | 工具路径错 | 修正路径或 `--aria2c none` |

**通用动作**：失败后**不要** `gc`（会清 staging），直接重跑同一条命令即可续跑。

---

## 6. 续跑与抢救

三件事自动发生，无需人工干预：

1. **文件内**：分片状态 + 按偏移写入，只补缺失分片；单流从未落盘长度继续。
2. **staging 复用**：上次已下完但未安装的文件（含已解压的归档，凭解压回执）。
3. **快照复用**：与已安装内容逐文件比对 md5，一致的硬链接。

另外：

- **修订变化时的抢救**：上游又发版或换源后，工具会从旧 staging 里**保留 md5 仍一致的文件**、
  删掉不配套的，再补缺失的。没有官方 md5 的文件（只有那个 ~500 B 的 metadata JSON）不参与抢救。
- **旧脚本的现场**：`--adopt <旧目录> --adopt-partial --adopt-extracted` 可接管
  `aria2c` 循环留下的 tar.gz / `.aria2` / 已解压索引（规则见 `../README.md` 注意事项 4.6）。
- **中断位置的查看**：

```bash
python3 blastdb_download.py -r /data/public/databases staging --against-remote
# identical to the revision … publishes now: re-running resumes it as is
# 或：older one: 57/384 file(s) still match and will be reused, the rest is downloaded again
```

---

## 7. 从 `update_blastdb.pl` 或无脚本流程迁移

```bash
# 1) 先给旧目录体检（离线，判断它是不是混装）
python3 blastdb_download.py inspect nt --dir /data/public/databases/NT

# 2) 建立第一份不可变基线（GCP 快照最确定；也可用 -s ncbi 取最新）
python3 blastdb_download.py -r /data/public/databases -s gcp -j 8 \
        --log-file /var/log/blastdb/base.log download nt taxdb

# 3) 切换 BLASTDB（这一步之后旧目录只作为备份）
export BLASTDB=/data/public/databases/current
```

要点：

- 新快照里的文件多为硬链接，**不会**立刻翻倍占盘；旧目录可保留一段时间做对照。
- 若旧目录被判 `FAILED`（混装），不要试图修补，直接用工具重建一套。
- 已有半成品想接着跑，加 `--adopt /旧目录 --adopt-partial`（见上）。
- 迁移后再把 cron/systemd 指向新命令；`BLASTDB` 从 `<旧目录>` 改成 `<root>/current`。

---

## 8. 日常检查清单

| 频率 | 动作 | 命令 |
|---|---|---|
| 每次更新后 | 快速完整性 | `verify --quick` |
| 每周 | 全量 md5 + 集合指纹 | `verify` |
| 每周 | 看上游是否有新版 | `verify --against-remote`（或 `showall --format pretty`） |
| 每月 | 磁盘与快照占用 | `list`、`gc --keep 2`（或 `gc --dry-run` 先看） |
| 变更前 | 环境自检 | `doctor` |
| 出问题时 | 先定位是不是混装 | `inspect <db> --dir <mirror>` |

**回滚**：`list` 看快照名 → `rollback --to 1`（一次软链接切换，秒级）。
