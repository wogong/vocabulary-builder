# 语境化生词收集系统 — P0 + P1 + P2

按 [`vocab-system-spec.md`](vocab-system-spec.md) 实现的 P0 最小闭环：

> 在网页上双击一个词，它连同所在句子进入 Anki 牌组，同步后在手机上能复习。

```
Chrome 插件 ──HTTPS──▶ app (FastAPI)
                        ├── inbox.db      唯一真相源 (D1)
                        └── AnkiWriter    单线程持有 collection (D2)
                                 │ debounce 同步
                                 ▼
                         anki-sync (自建)  ◀──── AnkiDroid / Desktop
```

## 已实现

**P0 — 最小闭环**

| 能力 | 位置 |
|---|---|
| `POST /v1/items` + inbox 表 + 幂等 | `app/main.py`, `app/db.py` |
| 单写入者 AnkiWriter、文件锁、同词合并 | `app/anki_writer.py` |
| debounce 同步 + 强制间隔，绝不自动 full sync | `app/anki_writer.py` |
| `/healthz`、`/v1/stats`、`/v1/sync` | `app/main.py` |
| 双击取词 + `Intl.Segmenter` 切句 + 设置页 | `extension/` |
| 自建 sync server + 宿主机 cloudflared | `docker-compose.yml` |
| L1/L2/L3 三层备份、inbox 重放 | `scripts/backup.py`, `scripts/reproject.py` |

**P1 — 质量提升**

| 能力 | 位置 |
|---|---|
| 词形还原（ECDICT 变形表优先，lemminflect 兜底） | `app/lemma.py` |
| ECDICT 英文查词 + CC-CEDICT 中文查词 | `app/dictionary.py` |
| `GET /v1/lookup`、`POST /v1/known` | `app/main.py` |
| 采集时写入释义、音标、词频到卡片 | `app/main.py`, `app/anki_writer.py` |
| 插件查词卡片：释义预览、可编辑句子、已认识 | `extension/content.js` |
| 离线队列：指数退避、幂等重试、badge 计数 | `extension/background.js` |
| 词频标记（COCA 排名 + `MIN_FREQ_RANK`） | `app/main.py` |

**P2 — Telegram bot 与发音**

| 能力 | 位置 |
|---|---|
| Webhook + 白名单 + 消息路由 | `app/bot.py`, `app/telegram.py` |
| 查词流程（释义、加入、发音、已认识） | `app/bot.py` |
| 句子流程：生词候选 + inline keyboard 多选 | `app/translate.py`, `app/bot.py` |
| `POST /v1/translate` | `app/main.py` |
| 定期提醒（静默时段、无内容不打扰、今日免打扰） | `app/bot.py` + APScheduler |
| 随机抽查（不写回 Anki 调度，D6） | `app/bot.py`, `bot_reviews` 表 |
| `/undo` 撤销最近一次加入 | `app/bot.py` |
| TTS 发音写入 `Audio` 字段并随 media 同步 | `app/tts.py` |

**尚未实现**：LLM 语境化释义、AnkiConnect 兼容层（P3）。
句子翻译需要一个 OpenAI 兼容端点（`LLM_*`），默认关闭 —— 关闭时句子流程仍会挑出生词候选。

## 部署

### 1. 起服务

```bash
cp .env.example .env      # 填 SYNC_PASSWORD 和 API_TOKENS，端口如有冲突也在这里改
docker compose up -d --build
```

`API_TOKENS` 里的 token 自己生成：`python -c "import secrets;print(secrets.token_urlsafe(32))"`

`DATA_DIR` 决定 collection、`inbox.db`、同步服务器状态放在宿主机哪里，默认 `./data`。
**务必指到同步盘（Dropbox / iCloud）之外** —— collection 是活的 SQLite 数据库，
被云盘同步工具在写入过程中抓走副本可能损坏它，而且 media 动辄几百 MB。

端口由 `.env` 里的 `APP_PORT`（默认 8000）和 `SYNC_SERVER_PORT`（默认 8080）控制，
容器内监听端口和发布到 `127.0.0.1` 的端口是同一个值。`SYNC_ENDPOINT` 不用管 ——
compose 会按 `SYNC_SERVER_PORT` 自动拼出来，不会跟实际端口对不上。

### 2. 初始化 collection（一次性，spec §8.4）

服务端永远不创建、不修改 note type（D3），所以要先给它一份准备好的 collection。
**app 会一直崩溃重启直到这一步做完**，这是设计如此，不是同步故障。

**先判断你属于哪种情况 —— 选错会很麻烦：**

整套系统里只有**一份** collection，服务端和你所有 Anki 客户端共用它。所以如果你已经有
在用的 Anki 收藏，服务端必须使用**那一份**，而不是另起一份。

#### 情况 A：你已经有在用的 Anki 收藏（绝大多数人）

必须走桌面 Anki 这条路，把 note type 加进你**现有的**收藏里再传上来：

```bash
uv sync                                    # 建 .venv，装依赖（含 Python 3.13）
uv run python scripts/make_notetype.py VocabContext.apkg
```

1. 桌面 Anki 打开你**平时用的**收藏，导入 `VocabContext.apkg`，按喜好调模板样式，
   删掉那条示例笔记
2. 「自定义同步服务器」填 `https://anki-sync.example.com/`（末尾斜杠不能少），
   同步上去（首次会问方向，选**上传**）
3. 然后再让服务端下载它：

```bash
docker compose stop app
docker compose run --rm app python scripts/bootstrap_collection.py
docker compose up -d app
```

#### 情况 B：全新开始，没有任何要保留的 Anki 数据

直接在服务端建好并推给 sync server，不用绕桌面：

```bash
docker compose stop app
docker compose run --rm app python scripts/bootstrap_collection.py --seed-local
docker compose up -d app
```

之后客户端首次同步时选**下载**即可。

> ⚠️ **不要在情况 A 用 `--seed-local`。** 它会把一份空白 collection 推上同步服务器；
> 你的客户端之后再上传自己的收藏，就会覆盖掉它，服务端本地那份随即与服务器血缘分离，
> 同步永久停在「需要 full sync」。真实数据不会丢（R2 只告警不执行），但要按下面
> 「同步卡在 full sync required」一节手工收拾。

两条路都必须先 `stop app`，否则崩溃重启中的容器会跟脚本抢 collection 文件锁。

### 3. 公网发布（宿主机 cloudflared）

compose 只在 `127.0.0.1` 暴露端口，由你已有的 cloudflared 配两条 public hostname：

| hostname | service |
|---|---|
| `api.example.com` | `http://localhost:$APP_PORT` |
| `anki-sync.example.com` | `http://localhost:$SYNC_SERVER_PORT` |

### 4. 配置 Anki 客户端

桌面 Anki 和 AnkiDroid 的「自定义同步服务器」都填 `https://anki-sync.example.com/`
（**末尾斜杠不能少**），账号密码用 `.env` 里的 `SYNC_USERNAME` / `SYNC_PASSWORD`。
首次同步会是一次 full download，把步骤 2 建好的牌组拉下来。

之后服务端只做增量同步，遇到需要 full sync 只告警不执行（R2）—— 这时候要人工介入，
`/healthz` 里的 `full_sync_required` 会是 `true`。

### 5. 词典（可选，P1）

没有词典也能用 —— 只是卡片上不会有释义、音标和词频，词形还原退到 lemminflect。

```bash
uv run python scripts/fetch_dicts.py --dest "$DATA_DIR/dict"
```

`$DATA_DIR/dict` 会被挂载成容器里的 `/data/dict`，也就是 `.env` 里 `ECDICT_PATH`
指向的位置。ECDICT 解压后约 **812 MB**，注意磁盘空间；CC-CEDICT 只有 4 MB。

两个常见坑：

- **目标目录可能是 root 属主**（docker 建的），脚本会在下载前就检查可写性并给出
  chown 命令，不会白下 206 MB。
- **从 P0 升上来的 `.env` 里没有 `ECDICT_PATH` / `CEDICT_PATH`**（P1 才加的），
  路径为空时词典静默禁用。对照 `.env.example` 补齐，启动日志里也会告警。

装好后 `GET /v1/stats` 的 `dictionaries` 应为 `{"ecdict": true, "cedict": true}`。

### 6. 装插件

`chrome://extensions` → 打开开发者模式 → 「加载已解压的扩展程序」→ 选 `extension/` 目录。
在扩展的「选项」里填服务器地址（`https://api.example.com`）和 token，点「测试连接」。

### 7. Telegram bot（可选，P2）

1. 找 [@BotFather](https://t.me/BotFather) 建一个 bot，拿到 token
2. 找 [@userinfobot](https://t.me/userinfobot) 拿到你自己的 chat id
3. 填进 `.env`：

```
TG_BOT_TOKEN=<token>
TG_WEBHOOK_SECRET=<python -c "import secrets;print(secrets.token_urlsafe(24))">
TG_ALLOWED_CHAT_IDS=<你的 chat id>
```

4. 重启并注册 webhook（Telegram 只投递到 HTTPS，所以要用 cloudflared 那个域名）：

```bash
docker compose up -d app
docker compose exec -T app python scripts/set_webhook.py https://api.example.com
```

白名单之外的消息一律不响应。发音默认开启（`TTS_ENABLED`），生成的 mp3 写进
collection 的 media 并随同步下发到手机。

### 8. 备份（宿主机 crontab）

```cron
17 *    * * *  cd /path/to/neo && docker compose exec -T app python scripts/backup.py l1
30 3    * * *  cd /path/to/neo && docker compose exec -T app python scripts/backup.py l2
45 4    * * 0  cd /path/to/neo && docker compose exec -T app python scripts/backup.py l3
```

L1/L2 用 SQLite 在线备份，不需要停服务；L3 从 L2 的副本导出 `.apkg`，不碰运行中的 collection。
**把 L3 的 `.apkg` 同步到异地** —— 完全自建同步意味着没有任何云端副本，这是最后一道防线。

## 开发与测试

依赖用 uv 管理，`pyproject.toml` + `uv.lock` 是唯一的版本来源 —— Dockerfile 也读同一个
lock，所以钉死的 anki 版本不会在开发和生产之间漂移（R1）。`.python-version` 让本地和
镜像都用 Python 3.13。

```bash
uv sync                             # 建 .venv 并按 uv.lock 装依赖

uv run python tests/smoke_anki.py       # R1：钉住 anki API（含真实同步往返），升级 anki 前必跑
uv run python tests/test_p0.py          # 接口 / 去重合并 / 挖空 / 转义 / 文件锁 / schema 漂移
uv run python tests/test_p1.py          # 词典 / 词形还原 / lookup / known / 释义写入
uv run python tests/test_p2.py          # bot 路由 / 候选词 / 抽查 / 提醒 / 撤销 / webhook 鉴权
uv run python tests/test_media_sync.py  # 真实 edge-tts 音频经同步服务器到达第二台设备
uv run python tests/test_bootstrap.py   # 两条首次初始化路径 + AnkiWeb 回退守卫
uv run python tests/test_recovery.py    # collection 被换掉后的重放，以及同步重试退避
uv run python tests/test_acceptance.py  # 全链路：bootstrap → 采集 → 同步 → 第二台设备看到卡
node tests/test_sentence.js             # 切句与 offset 跟踪
node tests/test_queue.js                # 插件离线队列：退避、幂等、上限
```

本地跑服务（不用 docker）：

```bash
export ANKI_COLLECTION_PATH="$DATA_DIR/anki/collection.anki2"
export INBOX_DB="$DATA_DIR/app/inbox.db"
export API_TOKENS=dev:dev-token
uv run uvicorn app.main:app --reload --workers 1 --port 8000   # 或设 UVICORN_PORT
```

`--workers 1` 不是可选项：一个进程只能有一个 collection 写入者（D2）。

升级依赖：改 `pyproject.toml` 里的 pin → `uv lock` → **先跑 `smoke_anki.py`** → 再 `docker compose build`。

## 排障

### 同步卡在 `full sync required`

`/healthz` 里 `full_sync_required: true`，日志反复出现 `SYNC REQUIRES FULL SYNC`。
说明服务端本地 collection 与同步服务器上那份**血缘不同**了（典型原因：客户端上传了
自己的收藏，覆盖掉服务端推上去的那份）。服务端不会自动选方向，因为那等于选择让哪边
被覆盖（R2）。

采集到的词不会丢 —— 它们都在 `inbox.db` 里，可以重放（D1）。按以下顺序收拾：

```bash
# 1. 确认要保留的是哪一份。通常是同步服务器上那份（你的真实收藏）
docker compose stop app

# 2. 确保那份收藏里有 VocabContext note type：
#    桌面 Anki 打开它 → 导入 VocabContext.apkg → 同步上去

# 3. 让服务端丢掉本地那份，重新下载
docker compose run --rm app sh -c 'mv /data/anki/collection.anki2 /data/anki/collection.anki2.bak'
docker compose run --rm app python scripts/bootstrap_collection.py

# 4. 把采集过的词重新投影进新 collection（已存在的会合并，不会重复）
docker compose run --rm app python scripts/reproject.py --all
docker compose up -d app
```

第 3 步保留了 `.bak`，确认无误后再删。

### 词进了服务端但 Anki 里看不到

先看 `/healthz`：`committed` 有数字说明卡片已经建好了，问题在同步这一环 ——
`last_sync_at` 为 `null` 就是从没成功同步过，看 `last_sync_error`。
`committed` 为 0 才是采集链路的问题，那就查插件设置和 `docker compose logs app`。

## 几个不显然的地方

- **collection 是投影，不是数据**。所有采集先落 `inbox.db`，Anki 坏了可以重放（D1）。
- **note type 字段一个都不能少**。`ContextDefinition`、`Audio` 这些 P0 用不上的字段也必须
  从第一天就存在 —— 事后加字段是 schema 变更，会触发 full sync（D3）。
- **卡片正面回退到 `{{Word}}`**。若 `ContextsMasked` 为空且模板只引用它，Anki 不会生成卡片。
- **`Intl.Segmenter` 不认缩写**。ICU 默认无缩写抑制表，"Mr. Smith" 会被切成两句，
  `extension/sentence.js` 里对头衔类缩写做了合并。
- **anki 需要 glibc ≥ 2.35**（manylinux_2_35 wheel），所以基础镜像是 bookworm，Alpine 不行。
- **数据目录不要放在云同步盘里**。collection 是活的 SQLite 数据库，Dropbox 之类的工具
  在写入中途抓副本可能损坏它；media 也有几百 MB。用 `DATA_DIR` 指到别处。
- **词形还原以 ECDICT 的变形表为准**，lemminflect 只在词典查不到时兜底，且只有当原词
  不是自身的原型时才还原 —— 否则 `better` 会变成 `good`、`whose` 会变成 `who`。
- **卡片上手改过的释义不会被覆盖**。再次采集同一个词只补空字段、追加语境。
- **重试幂等靠 `idem_key`**：离线队列重发时若服务端已经收过，返回 409，队列按成功处理。
- **bot 的抽查不写回 Anki 调度**（D6）。评分只进 `bot_reviews` 表 —— 服务端和 AnkiDroid
  同时写一张卡的调度状态，是这套架构里最容易丢数据的地方。
- **collection 只能由 writer 线程读写**。需要在别处读（待复习数、`/undo`、TTS 挂载）
  一律走 `writer.ask(fn)`，它把闭包送到 writer 线程执行再把结果送回来。
- **TTS 不在 writer 线程里做**。edge-tts 是网络调用，会阻塞写入；它在自己的 worker
  里合成，只把结果交给 writer 落盘。
