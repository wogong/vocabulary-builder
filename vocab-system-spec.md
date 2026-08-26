# 语境化生词收集系统 — 技术规格说明书

> 版本 v0.5 · 状态：P0 与 P1 已实现（决策留档见 §11）
>
> 一句话描述：在浏览器和 Telegram 中查词时，自动捕获单词及其所在语境，经服务端处理后实时写入 Anki 牌组，并提供轻量的复习提醒。

---

## 目录

1. [目标与非目标](#1-目标与非目标)
2. [总体架构](#2-总体架构)
3. [核心设计决策](#3-核心设计决策)
4. [数据模型](#4-数据模型)
5. [Server 规格](#5-server-规格)
6. [Chrome 插件规格](#6-chrome-插件规格)
7. [Telegram Bot 规格](#7-telegram-bot-规格)
8. [部署与运维](#8-部署与运维)
9. [实施阶段划分](#9-实施阶段划分)
10. [风险与缓解](#10-风险与缓解)
11. [决策记录](#11-决策记录)

---

## 1. 目标与非目标

### 1.1 目标

| 编号 | 目标 |
|---|---|
| G1 | 在任意网页阅读时，一次交互（双击/快捷键）即可查词，并**自动捕获所在句子** |
| G2 | 生词 + 句子 + URL + 元信息实时送达服务端，并在数秒内出现在 Anki 牌组中 |
| G3 | 同一个词多次遇到时，**合并为一张卡的多个语境**，而非产生重复卡 |
| G4 | 移动端可继续使用 AnkiDroid / AnkiMobile 正常复习（保留 Anki 生态） |
| G5 | Telegram 作为第二采集入口：查词、翻译句子、从句子中挑选生词入库 |
| G6 | Telegram 定期提醒 + 随机抽查，作为 Anki 之外的轻量触点 |
| G7 | 采集数据独立留存，Anki 只是下游投影，可随时重建 |

### 1.2 非目标（本版本明确不做）

- 不做全文沉浸式翻译（会削弱阅读理解的锻炼）
- 不做自建 SRS 复习界面（复习交给 Anki 客户端）
- 不做多用户 SaaS（单用户 / 小圈子，鉴权用静态 token 即可）
- 不做视频字幕挖词（YouTube/Netflix 留待后续）
- 不做移动端浏览器插件（iOS/Android 浏览器扩展生态受限）
- 不做 PDF 阅读支持（Chrome 内置 PDF 阅读器无法注入 content script，PDF.js 方案不在范围内）
- 首版不做 LLM 语境化释义（note type 中预留字段作为扩展点，见 §4.2）
- 首版不做「暂存区批量确认」（inbox 架构天然支持，跑一个月后按牌组质量再评估）
- 不做多语言：主流程固定「英文 → 中文」，仅额外支持查中文词（见 §5.3）

---

## 2. 总体架构

```
┌─────────────────┐        ┌─────────────────┐
│  Chrome 插件     │        │  Telegram Bot   │
│  (MV3)          │        │  (Webhook)      │
└────────┬────────┘        └────────┬────────┘
         │  HTTPS / Bearer token    │
         └──────────┬───────────────┘
                    ▼
       ┌────────────────────────────┐
       │        Server (FastAPI)     │
       │                            │
       │  ┌──────────────────────┐  │
       │  │  HTTP 层             │  │  鉴权 / 校验 / 幂等
       │  └──────────┬───────────┘  │
       │             ▼              │
       │  ┌──────────────────────┐  │
       │  │  Inbox (SQLite)      │  │  ★ 唯一真相源
       │  └──────────┬───────────┘  │
       │             ▼              │
       │  ┌──────────────────────┐  │
       │  │  Enrichment Worker   │  │  词形还原 / 释义 / 挖空 / TTS
       │  └──────────┬───────────┘  │
       │             ▼              │
       │  ┌──────────────────────┐  │
       │  │  Anki Writer         │  │  单线程持有 collection
       │  │  (pip install anki)  │  │
       │  └──────────┬───────────┘  │
       └─────────────┼──────────────┘
                     │ sync (debounced)
                     ▼
       ┌────────────────────────────┐
       │  anki-sync-server（自建）    │
       └─────────────┬──────────────┘
                     │
        ┌────────────┴────────────┐
        ▼                         ▼
   AnkiDroid                 Anki Desktop
```

### 2.1 数据流（Happy Path）

1. 用户在网页双击 `ubiquitous`
2. 插件用 `Intl.Segmenter` 切出所在句子，连同 URL/标题/前后文发给 `POST /v1/items`
3. Server 校验 token → 写入 `inbox` 表（status=`pending`）→ **立即返回 202**
4. Worker 取出 → 词形还原 `ubiquitous` → 查 ECDICT → （可选）LLM 生成语境化释义 → 生成挖空句
5. Anki Writer 检查该 lemma 是否已有 note：
   - 无 → `col.add_note()`
   - 有 → `col.update_note()`，追加新语境
6. 标记 `status=committed`，触发 debounce 同步计时器
7. 60 秒无新增 → `col.sync_collection()` 推到自建 anki-sync-server
8. 手机端 Anki 下拉同步即可看到

---

## 3. 核心设计决策

### D1 — Inbox 是唯一真相源，Anki 是下游投影

所有采集数据先落 SQLite `inbox` 表，再异步投影到 Anki。

**理由：**
- 采集端得到 <100ms 响应，不受 Anki 写入/同步耗时影响
- Anki collection 损坏、需要重建、或你改了 enrichment 逻辑想重跑时，可以 replay
- 保留原始 payload，未来换调度器/换卡片模板都不丢数据

### D2 — Anki collection 单写入者

`anki` 库的 `Collection` **不是线程安全的，且同一文件不能被并发打开**。

**实现约束：**
- 整个进程内有且仅有一个 Anki Writer 线程持有 `Collection` 实例
- 所有写入通过 `queue.Queue` 投递，Writer 串行消费
- FastAPI 用 `--workers 1`；若必须多 worker，Writer 必须拆成独立进程
- 服务器上的这个实例是**唯一创建卡片的地方**，其它客户端只复习

### D3 — 绝不在服务端修改 schema

创建 note type、deck 只在**首次启动**时执行一次，之后代码永不修改 note type 字段、模板、deck options。

**理由：** schema modification 会触发 Anki 的 full sync，导致「上传覆盖 or 下载覆盖」的二选一，多客户端场景下极易丢数据。

**实现：** 启动时检查 note type 是否存在，存在则**只读校验字段名**，不一致时**报错退出**，绝不自动迁移。字段变更走人工迁移流程。

### D4 — 同词合并

去重键 = `(lemma, lang)`。已存在则把新句子追加到 `Contexts` 字段（`<hr>` 分隔），保留最近 N=5 条。

**理由：** 一个词的多个真实语境比五张重复卡有价值得多，这也是 Kindle 生词本做不到的。

### D5 — 显式确认才入库（非全自动）

查词 ≠ 想背。默认只有用户点了「加入」按钮才产生卡片，单纯查看释义不入库。但入库必须**一次点击**完成（无二次确认、无表单），把收集成本压到最低。

**理由：** Kindle 生词本的全收模式会迅速产生几百个你根本不想背的词。可通过配置项 `auto_add_on_lookup` 打开全自动模式。

### D6 — Bot 复习默认为「抽查模式」，不写回调度

Telegram 的随机复习默认**不修改 Anki 卡片的调度状态**，只是展示 + 自评（结果记在 inbox 库自己的表里）。

**理由：** 避免服务端和 AnkiDroid 双写调度数据。真正的复习进度由 Anki 客户端掌握。
可通过配置 `bot_review_writes_back=true` 开启真复习，但届时应停止在其它客户端复习同一牌组。

### D7 — 同步使用自建 anki-sync-server，经宿主机 Cloudflare Tunnel 暴露

不用 AnkiWeb（同步频率受限、客户端版本强制、数据托管在第三方），自建官方 sync server。

**实现约束：**
- 直接用 `anki` 包内置的官方 sync server（`python -m anki.syncserver`），与 Anki Writer 复用同一镜像、同一钉死版本 → 客户端版本天然对齐，media 同步原生支持
- 服务端自己的同步走 docker 内网（`SYNC_ENDPOINT=http://anki-sync:8080/`），不出公网
- AnkiDroid / Desktop 在「自定义同步服务器」中填经 Tunnel 发布的 HTTPS 域名（注意需带尾斜杠，如 `https://anki-sync.example.com/`）
- 注意 Cloudflare 免费版单请求体上限 100MB：正常同步是增量/分块的，不受影响；collection full upload 超限时临时在内网直连处理（见 R9）

---

## 4. 数据模型

### 4.1 Inbox 库（`/data/app/inbox.db`，SQLite + WAL）

```sql
CREATE TABLE items (
    id              INTEGER PRIMARY KEY,
    idem_key        TEXT UNIQUE,          -- 客户端生成的 UUID，用于幂等
    source          TEXT NOT NULL,        -- 'chrome' | 'telegram' | 'api'
    client_id       TEXT,                 -- 设备标识
    raw_word        TEXT NOT NULL,        -- 用户实际选中的形式，如 'ubiquitously'
    lemma           TEXT,                 -- 词形还原结果，如 'ubiquitous'
    lang            TEXT DEFAULT 'en',
    sentence        TEXT NOT NULL,
    sentence_masked TEXT,                 -- 目标词替换为 ____ 的版本
    word_offset     INTEGER,              -- raw_word 在 sentence 中的起始位置，保留下来
                                          -- 才能在重放时正确挖空重复出现的词
    context_before  TEXT,                 -- 前一句（可选）
    context_after   TEXT,                 -- 后一句（可选）
    url             TEXT,
    page_title      TEXT,
    definition      TEXT,                 -- enrichment 产出
    definition_ctx  TEXT,                 -- 语境化释义（LLM）
    phonetic        TEXT,
    translation     TEXT,                 -- 句子翻译
    audio_file      TEXT,                 -- 相对 media 目录
    freq_rank       INTEGER,              -- 词频排名，用于过滤
    status          TEXT NOT NULL,        -- pending|enriched|committed|skipped|failed
    anki_note_id    INTEGER,              -- 投影后的 note id
    merge_target    INTEGER,              -- 若为合并，指向已存在的 item id
    error           TEXT,
    retry_count     INTEGER DEFAULT 0,
    raw_payload     TEXT,                 -- 客户端原始 JSON，原样留存（决策 S8）
    client_ts       TEXT,
    created_at      TEXT NOT NULL,
    committed_at    TEXT
);

CREATE INDEX idx_items_status ON items(status);
CREATE INDEX idx_items_lemma  ON items(lemma, lang);
CREATE INDEX idx_items_created ON items(created_at);

-- Bot 抽查记录（不写回 Anki）
CREATE TABLE bot_reviews (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES items(id),
    chat_id     INTEGER NOT NULL,
    rating      INTEGER,                  -- 1..4
    reviewed_at TEXT NOT NULL
);

-- 用户配置 / 绑定
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 已知词表（用于 Bot 的生词候选判断）
CREATE TABLE known_words (
    lemma      TEXT PRIMARY KEY,
    marked_at  TEXT NOT NULL
);
```

### 4.2 Anki Note Type：`VocabContext`

| 字段 | 说明 | 示例 |
|---|---|---|
| `Word` | lemma，**第一字段，用作去重键** | `ubiquitous` |
| `Phonetic` | 音标 | `/juːˈbɪkwɪtəs/` |
| `Definition` | 词典释义（可多义项） | `adj. 无处不在的；普遍存在的` |
| `ContextDefinition` | 本语境下的单一义项 | `无处不在的` |
| `Contexts` | 语境句 HTML，目标词加粗，`<hr>` 分隔多条 | `Smartphones are now <b>ubiquitous</b>.` |
| `ContextsMasked` | **单句**：最新一条语境的挖空版，用作卡片正面 | `Smartphones are now <b>____</b>.` |
| `Translation` | 首个语境句的中文翻译 | `智能手机现在无处不在。` |
| `Audio` | `[sound:xxx.mp3]` | |
| `Source` | 来源标题 + 链接 HTML | `<a href="...">The Economist</a>` |
| `Meta` | JSON，存 item id / 采集时间等，便于回溯 | `{"item_ids":[12,45]}` |

> `ContextDefinition` 首版留空（LLM 暂不启用），但字段必须从第一天就存在：D3 禁止事后修改 note type，后补字段会触发 full sync。`Audio` 由 edge-tts 填充（P2 落地）。

**卡片模板（单卡型，Cloze 风格但用普通模板实现）：**

- 正面：`{{ContextsMasked}}`，为空时回退到 `{{Word}}`
- 背面：`{{Word}}` + `{{Phonetic}}` + `{{Audio}}` + `{{ContextDefinition}}` + `{{Definition}}` + `{{Contexts}}` + `{{Translation}}` + `{{Source}}`

> 用普通模板而非 Anki Cloze 类型，是为了让「追加语境」不会自动增生卡片。
>
> 两个实现约束：
> 1. `ContextsMasked` 只存最新一条挖空句（而非与 `Contexts` 平行的多条），这样正面
>    模板不需要用 JS 去截取第一段 —— 模板里的 JS 在各客户端表现不一致。历史语境仍
>    完整保留在 `Contexts` 中，背面可见。
> 2. 正面必须有回退分支。Anki 依据正面渲染结果判断卡片是否为空，若正面只引用
>    `ContextsMasked`，无语境的词（§7.3）将不会生成任何卡片。

---

## 5. Server 规格

### 5.1 技术栈

| 组件 | 选型 | 说明 |
|---|---|---|
| Web 框架 | FastAPI + uvicorn | `--workers 1` |
| Anki 写入 | `anki` (PyPI) | **版本必须钉死**，见 §10 R1 |
| 数据库 | SQLite (WAL) | inbox |
| 词形还原 | `lemminflect` | 纯查表，无需下载模型，毫秒级 |
| 基础词典 | ECDICT (SQLite) | 离线、含音标词频释义 |
| 中文查词 | CC-CEDICT | 输入含 CJK 时使用，离线，zh→en |
| 语境化释义 | LLM API | **P3 再启用，默认关闭** |
| TTS | `edge-tts` | 默认启用，生成 mp3，经 `sync_media()` 上行 |
| Bot | `python-telegram-bot` 或 `aiogram` | Webhook 模式 |
| 定时任务 | APScheduler | 提醒、同步、备份 |

### 5.2 进程内组件

```
main process (uvicorn, 1 worker)
├── FastAPI app                  ← HTTP 入口
├── EnrichmentWorker (asyncio)   ← 可并发，纯 IO
├── AnkiWriter (single thread)   ← 串行，持有 Collection
│     └── SyncScheduler          ← debounce 同步
└── TelegramBot (webhook handler，挂在同一 FastAPI 上)
```

### 5.3 API 规格

所有接口前缀 `/v1`，鉴权 `Authorization: Bearer <TOKEN>`。

---

#### `POST /v1/items` — 提交生词

**Request**

```json
{
  "idem_key": "0f2a...uuid",
  "source": "chrome",
  "client_id": "chrome-macbook",
  "word": "ubiquitously",
  "lang": "en",
  "sentence": "Smartphones are now ubiquitously present in daily life.",
  "word_offset": 20,
  "context_before": "Technology has changed rapidly.",
  "context_after": "This raises new questions.",
  "url": "https://example.com/article",
  "page_title": "The Rise of Mobile",
  "client_ts": "2026-08-25T10:30:00Z"
}
```

- `word_offset`：`word` 在 `sentence` 中的字符起始位置。**必须提供**，否则同一个词在句中多次出现时无法正确挖空。
- 除 `word` / `sentence` 外均可选。

**Response `202 Accepted`**

```json
{
  "id": 123,
  "status": "queued",
  "dedup": "new",
  "lemma": "ubiquitous",
  "existing_contexts": 0
}
```

`dedup` 取值：`new`（新词）/ `merged`（已有卡，将追加语境）/ `duplicate`（同词同句，已忽略）

**错误码**

| 码 | 场景 |
|---|---|
| 401 | token 无效 |
| 409 | `idem_key` 重复（返回原 item） |
| 422 | 参数校验失败 |
| 429 | 超出速率限制（默认 60 req/min） |

---

#### `GET /v1/lookup` — 查词（不入库）

Chrome 插件和 Bot 共用。

**Query**：`word`, `sentence`（可选，用于语境化释义）, `lang`

**Response `200`**

```json
{
  "word": "ubiquitously",
  "lemma": "ubiquitous",
  "phonetic": "/juːˈbɪkwɪtəs/",
  "definitions": ["adj. 无处不在的", "adj. 普遍存在的"],
  "context_definition": "无处不在的",
  "freq_rank": 8421,
  "already_known": false,
  "in_deck": true,
  "existing_contexts": 2,
  "audio_url": "/v1/media/ubiquitous.mp3"
}
```

- `in_deck` 让插件的按钮显示为「已在牌组（2 条语境）· 追加」而非「加入」。
- `word` 含 CJK 字符时自动按 `lang=zh` 处理，改查 CC-CEDICT（zh→en），响应结构不变。
- LLM 未启用时 `context_definition` 为 `null`，客户端回退展示 `definitions[0]`。

---

#### `POST /v1/translate` — 句子翻译 + 生词候选

Bot 主要使用。

**Request**：`{"text": "...", "lang": "en", "target": "zh"}`

**Response**

```json
{
  "translation": "……",
  "candidates": [
    {"word": "ubiquitous", "raw": "ubiquitously", "offset": 20,
     "freq_rank": 8421, "brief": "无处不在的", "in_deck": false},
    {"word": "proliferate", "raw": "proliferation", "offset": 55,
     "freq_rank": 9903, "brief": "激增", "in_deck": false}
  ]
}
```

**候选词判定规则**（可配置）：
1. 词形还原后不在 `known_words` 表
2. 不在牌组中（或已在牌组但语境 < 3 条也可提示）
3. `freq_rank > min_rank`（默认 3000，即排除最常见 3000 词）
4. 不是专有名词 / 数字 / 停用词
5. 按 `freq_rank` 降序取前 8 个

---

#### 其它接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/v1/items` | 列表，支持 `status` / `since` / `limit` |
| `GET` | `/v1/items/{id}` | 详情 |
| `PATCH` | `/v1/items/{id}` | 修改 sentence / definition，触发重新投影 |
| `DELETE` | `/v1/items/{id}` | 软删除，同时从 Anki 移除该语境 |
| `POST` | `/v1/known` | 标记为已知（不再提示） |
| `POST` | `/v1/sync` | 手动触发 Anki 同步 |
| `GET` | `/v1/stats` | 今日采集数、待处理数、牌组总数、上次同步时间 |
| `GET` | `/healthz` | 存活探针，返回 queue 深度、collection 是否可写 |

---

#### 可选：AnkiConnect 兼容层

`POST /ankiconnect` — 实现 AnkiConnect v6 协议的子集，使 **Yomitan 可直接把本服务当作 Anki 后端**（配置里把服务器地址改成本服务即可）。

需实现的 action：`version` / `deckNames` / `modelNames` / `modelFieldNames` / `canAddNotes` / `addNote` / `addNotes` / `storeMediaFile` / `guiBrowse`(no-op)

**注意：** 必须处理 CORS 预检，允许 `chrome-extension://*` origin；且必须要求 token（AnkiConnect 原生无认证，绝不能裸奔）。

### 5.4 Anki Writer 实现要点

```python
# 伪代码，实际 API 以钉死的 anki 版本为准
from anki.collection import Collection

class AnkiWriter:
    def __init__(self, path, deck_name, notetype_name):
        self.col = Collection(path)
        self._ensure_schema(deck_name, notetype_name)  # 只创建，不修改
        self._dirty_since = None

    def upsert(self, item):
        nt = self.col.models.by_name(NOTETYPE)
        escaped = item.lemma.replace('"', '\\"')
        existing = self.col.find_notes(f'note:{NOTETYPE} "Word:{escaped}"')
        if existing:
            note = self.col.get_note(existing[0])
            note["Contexts"] = append_context(note["Contexts"], item, max_n=5)
            note["ContextsMasked"] = append_context(note["ContextsMasked"], item, masked=True, max_n=5)
            note["Meta"] = merge_meta(note["Meta"], item.id)
            self.col.update_note(note)
            return note.id, "merged"
        else:
            note = self.col.new_note(nt)
            fill_fields(note, item)
            self.col.add_note(note, self.deck_id)
            return note.id, "new"

    def maybe_sync(self):
        # debounce: 距最后一次写入 >= SYNC_DEBOUNCE_SEC 才同步
        ...
        out = self.col.sync_collection(self.auth)
        if out.required != NO_CHANGES and needs_full_sync(out):
            alert("REQUIRES FULL SYNC — 人工介入")   # ← 绝不自动执行
```

**同步策略**

- Debounce：最后一次写入后 `SYNC_DEBOUNCE_SEC`（默认 60s）触发
- 兜底：每 30 分钟强制同步一次（即使无写入，以拉取其它客户端的复习记录）
- 进程退出前必须同步 + `col.close()`
- 若返回「需要完整同步」→ **只告警不执行**，通过 Telegram 推送给你人工处理

**并发保护**

- 启动时对 collection 目录加文件锁（`fcntl.flock`），防止两个实例同时打开
- 所有写操作走队列，队列深度暴露在 `/healthz`

### 5.5 配置项（环境变量）

```
# 核心
ANKI_COLLECTION_PATH=/data/anki/collection.anki2
ANKI_DECK=Vocab::Reading
ANKI_NOTETYPE=VocabContext
INBOX_DB=/data/app/inbox.db

# 端口（容器内监听 = 发布到 127.0.0.1 的端口）
APP_PORT=8000
SYNC_SERVER_PORT=8080

# 同步（自建 sync server，见 D7；服务端走内网直连）
# compose 下由 SYNC_SERVER_PORT 自动推导，仅在脱离 compose 运行时才需手填
SYNC_ENDPOINT=http://anki-sync:8080/
SYNC_USERNAME=...                    # 对应 anki-sync 的 SYNC_USER1
SYNC_PASSWORD=...
SYNC_DEBOUNCE_SEC=60
SYNC_INTERVAL_SEC=1800

# 鉴权
API_TOKENS=chrome:xxxx,bot:yyyy      # client_id:token 逗号分隔

# Enrichment
ECDICT_PATH=/data/dict/ecdict.db
CEDICT_PATH=/data/dict/cedict.txt
LLM_ENABLED=false                    # P3 再启用
LLM_API_BASE=...
LLM_API_KEY=...
LLM_MODEL=...
TTS_ENABLED=true

# 策略
AUTO_ADD_ON_LOOKUP=false
MIN_FREQ_RANK=3000
MAX_CONTEXTS_PER_NOTE=5

# Telegram
TG_BOT_TOKEN=...
TG_ALLOWED_CHAT_IDS=123456789
TG_WEBHOOK_SECRET=...
BOT_REVIEW_WRITES_BACK=false
```

---

## 6. Chrome 插件规格

### 6.1 基本信息

- Manifest V3
- 权限：`storage`, `contextMenus`, `alarms`
- host_permissions：`<all_urls>`（content script 注入）+ 服务端域名
- 不请求 `tabs`、不收集浏览历史

### 6.2 交互设计

| 操作 | 行为 |
|---|---|
| 双击单词 | 弹出查词卡片（可在设置中改为 Alt+点击 / 选中后按快捷键） |
| 选中短语后按 `Ctrl+Shift+E` | 以选中内容为「词」，其所在句为语境 |
| 查词卡片上点「+」 | 提交到服务端 |
| 查词卡片上点「✓ 已认识」 | 加入 known_words，以后不再提示 |
| 右键菜单「加入生词本」 | 备用入口 |
| 工具栏图标 | 显示今日采集数 / 待发送队列 / 打开设置 |

### 6.3 句子提取算法

这是插件最核心也最容易做砸的部分。

```
1. 从 selection 的 anchorNode 出发，向上找到最近的「块级容器」
   —— p, li, td, blockquote, div(无块级子元素), h1-h6, dd, figcaption
2. 取该容器的 textContent，并建立 (DOM node, offset) → 扁平文本 offset 的映射
3. 用 Intl.Segmenter(lang, { granularity: 'sentence' }) 切句
   —— 优于正则，能正确处理 Mr. / e.g. / U.S. / 引号内句号
4. 定位包含目标词的 segment
5. 前后各多取一个 segment 作为 context_before / context_after
6. 清洗：
   - 折叠连续空白
   - 去掉脚注标记 [1] [2]
   - 去掉零宽字符
   - 若句子 > 400 字符，向目标词两侧各截 200 字符并加 …
   - 若句子 < 3 个词（如导航栏文字），标记 low_confidence，UI 上让用户确认
```

**降级路径**：`Intl.Segmenter` 不可用 → 退回正则 `/(?<=[.!?])\s+(?=[A-Z])/`。

**实测修正**：ICU 默认不带缩写抑制表，`Intl.Segmenter` 能正确处理 `U.S.` / `e.g.`
（后面不接大写或无空格），但会把 `Mr. Smith` 切成两句。因此切句后补一步合并：上一段
以头衔类缩写（Mr/Mrs/Dr/Prof/St/vs 等）结尾时与下一段合并。不包含 `etc.` `Inc.`
这类真的会出现在句末的缩写。

**已知限制**：
- Chrome 内置 PDF 阅读器无法注入 content script。PDF 已明确不在范围内（§1.2），文档中说明即可。
- Kindle 云端阅读器（read.amazon.com）文本层为 canvas/分片渲染，不支持。
- Shadow DOM 内的文本需 `composedPath()` 处理。

### 6.4 UI

- 查词卡片挂在 **Shadow DOM** 中（`mode: 'closed'`），内联全部样式，避免被页面 CSS 污染
- 卡片内容：单词 / 音标 / 发音按钮 / 释义 / **可编辑的句子文本框** / 「+ 加入」/「✓ 已认识」
- 若 `in_deck: true`，按钮变为「+ 追加语境（已有 2 条）」
- 定位：跟随选区，自动避开视口边缘
- Esc 或点击外部关闭

### 6.5 离线队列

Service worker 中维护发送队列，存于 `chrome.storage.local`：

- 发送失败（网络错误 / 5xx）→ 入队，指数退避重试（1s, 5s, 30s, 5min, 30min）
- 每个 item 带 `idem_key`，服务端幂等，重试安全
- 队列上限 500 条，超出丢弃最旧的并告警
- `chrome.alarms` 每 5 分钟尝试冲刷队列
- 工具栏 badge 显示待发送数量

### 6.6 设置页

服务器地址 / Token / 目标牌组 / 触发方式（双击 · Alt+点击 · 快捷键）/ 母语 / 是否自动发音 / 是否高亮已收集的词 / 站点黑名单。

---

## 7. Telegram Bot 规格

### 7.1 部署方式

Webhook 模式，挂载在同一 FastAPI 应用的 `/tg/{secret}` 路径下（服务器已有 HTTPS，无需额外进程）。

访问控制：`TG_ALLOWED_CHAT_IDS` 白名单，非白名单一律不响应。

### 7.2 消息路由

Bot 不使用复杂命令，**根据消息形态自动判断意图**：

```
收到文本消息
├── 以 / 开头            → 命令处理
├── 含 CJK 的短文本       → 中文查词流程（CC-CEDICT，lang=zh）
├── 单个词（无空格）      → 查词流程
├── ≤ 3 个词             → 查词流程（当作词组）
└── > 3 个词             → 句子流程（翻译 + 生词候选）
```

### 7.3 查词流程

```
用户: ubiquitous

Bot:  📖 ubiquitous  /juːˈbɪkwɪtəs/
      adj. 无处不在的；普遍存在的
      词频排名 8421

      ⚠️ 未提供语境，建议连同句子一起发送

      [ ➕ 加入生词本 ]  [ 🔊 发音 ]  [ ✓ 已认识 ]
```

- 无语境时入库，`Contexts` 留空，卡片背面只有释义。UI 上明确提示这类卡质量较低。
- 若已在牌组中，显示「📌 已在牌组 · 3 条语境」并列出现有语境。

### 7.4 句子流程（核心功能）

```
用户: Smartphones are now ubiquitously present, and the
      proliferation of apps has fundamentally altered how
      we consume information.

Bot:  🇨🇳 智能手机如今无处不在，应用程序的激增从根本上
      改变了我们获取信息的方式。

      可能的生词（点击选择）：

      [ ubiquitous 无处不在的 ]
      [ proliferation 激增 ]
      [ fundamentally 从根本上 ]
      [ alter 改变 ]

      [ ✅ 加入选中 (0) ]  [ ❌ 取消 ]
```

**交互细节：**

- 每个候选词是一个 inline keyboard 按钮，点击 toggle 选中状态（选中时前缀变 `☑️`）
- 「加入选中」按钮实时显示计数
- 点击「加入选中」→ 逐个调用内部 `add_item`，**整句作为它们共同的语境**
- 候选词的 callback_data 受 64 字节限制 → 用 `{msg_key}:{idx}` 形式，实际数据存在服务端短期缓存中（TTL 1h）
- 完成后消息 edit 为结果摘要：`✅ 已加入 2 个词：ubiquitous, proliferation`

**长文本处理：** 超过 1000 字符时，先切句，逐句翻译，候选词跨句去重，分多条消息发送。

### 7.5 定期提醒

APScheduler cron 任务：

- 频率与时间可配置，默认 每天 09:00 / 21:00（用户时区）
- **仅当有内容可提醒时才推送**（有到期卡 或 有当日新采集词），避免噪音
- 静默时段（默认 23:00–08:00）不推送
- 推送内容示例：

```
🔔 今日待复习 23 张

今天新收集了 7 个词：
ubiquitous, proliferation, alter, ...

[ 📱 打开 AnkiDroid ]  [ 🎲 在这里抽查 5 个 ]  [ 😴 今天算了 ]
```

- 「今天算了」→ 当日不再推送（写入 settings）
- 连续 3 天无响应 → 自动降频到每天 1 次，并提示

### 7.6 随机抽查

```
Bot:  🎲 抽查 1/5

      Smartphones are now ____ present in daily life.

      [ 🤔 显示答案 ]  [ ⏭ 跳过 ]

--- 点击后 ---

Bot:  🎲 抽查 1/5

      Smartphones are now ubiquitous present in daily life.

      ubiquitous /juːˈbɪkwɪtəs/
      adj. 无处不在的

      [ 😰 忘了 ] [ 😐 勉强 ] [ 🙂 记得 ] [ 😎 简单 ]
```

**选词策略**（抽查模式，不读 Anki 调度）：
1. 60% 从最近 7 天采集的词中随机
2. 30% 从 `bot_reviews` 中评分低（1-2）且超过 2 天未抽查的词
3. 10% 完全随机（老词捞回）

评分只写入 `bot_reviews` 表。`BOT_REVIEW_WRITES_BACK=true` 时，改为读 `col.sched` 取到期卡并回写评分。

### 7.7 命令列表

| 命令 | 说明 |
|---|---|
| `/start` | 欢迎 + 绑定说明 |
| `/review [n]` | 立即抽查 n 个（默认 5） |
| `/today` | 今日采集的词列表 |
| `/stats` | 采集统计 + 牌组规模 + 上次同步时间 |
| `/search <word>` | 查这个词是否已在牌组，显示所有语境 |
| `/known <word>` | 标记为已知 |
| `/undo` | 撤销最近一次加入 |
| `/pause [days]` | 暂停提醒 |
| `/settings` | 提醒时间、抽查数量、词频阈值 |
| `/sync` | 手动触发 Anki 同步 |

---

## 8. 部署与运维

### 8.1 docker-compose

```yaml
services:
  app:
    build: .
    restart: unless-stopped
    env_file: .env
    environment:
      UVICORN_PORT: ${APP_PORT:-8000}
      # 由 SYNC_SERVER_PORT 推导，避免与 sync 容器实际端口对不上
      SYNC_ENDPOINT: http://anki-sync:${SYNC_SERVER_PORT:-8080}/
    ports:
      - "127.0.0.1:${APP_PORT:-8000}:${APP_PORT:-8000}"
    volumes:
      - ./data/anki:/data/anki       # collection + media
      - ./data/app:/data/app         # inbox.db
      - ./data/dict:/data/dict:ro    # ECDICT + CC-CEDICT（P1 起）
    healthcheck:                     # 镜像里没有 curl，用 python 探活
      test: ["CMD", "python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('UVICORN_PORT','8000')+'/healthz')"]
      interval: 30s
    # 注意：绝不能扩到多副本，Anki collection 单写入者
    deploy:
      replicas: 1

  anki-sync:
    build: .                         # 复用 app 镜像，保证 anki 版本一致（D7）
    restart: unless-stopped
    command: python -m anki.syncserver
    ports:
      - "127.0.0.1:${SYNC_SERVER_PORT:-8080}:${SYNC_SERVER_PORT:-8080}"
    environment:
      SYNC_HOST: 0.0.0.0             # 默认只绑 loopback，容器间将不可达
      SYNC_PORT: ${SYNC_SERVER_PORT:-8080}
      SYNC_USER1: ${SYNC_USERNAME}:${SYNC_PASSWORD}
      SYNC_BASE: /data/sync
    volumes:
      - ./data/sync:/data/sync
```

两个端口只绑定 `127.0.0.1`，不直接对公网开放。公网 HTTPS 由宿主机已有的 cloudflared 发布：两条 public hostname 分别指向 `localhost:$APP_PORT`（`/v1/*`、`/tg/*`）与 `localhost:$SYNC_SERVER_PORT`（sync），TLS 由 Cloudflare 终结。**AnkiConnect 兼容层若启用，同样只经 tunnel 暴露并强制 token。**

端口全部可配（`.env` 中的 `APP_PORT` / `SYNC_SERVER_PORT`，默认 8000 / 8080），容器内监听端口与发布端口取同一值：uvicorn 直接读 `UVICORN_PORT`，sync server 直接读 `SYNC_PORT`，都不需要改代码或重新构建镜像。

### 8.2 备份

三层，缺一不可：

| 层 | 内容 | 频率 | 保留 |
|---|---|---|---|
| L1 | `inbox.db`（SQLite `.backup` 命令，不能直接 cp） | 每小时 | 48 份 |
| L2 | `collection.anki2` + media（先停写、`col.close()` 再复制） | 每天 03:00 | 14 天 |
| L3 | 导出 `.apkg`（`col.export_anki_package()`），异地存储 | 每周 | 8 份 |

L3 是最后防线：即使 collection 彻底损坏、sync 冲突炸了、sync server 数据丢了，apkg 可以在任何 Anki 上直接导入。完全自建同步意味着没有任何云端副本，L3 是硬要求而非可选项。

### 8.3 监控与告警（通过 Telegram 推送给自己）

- Worker 队列积压 > 50
- 连续 3 次同步失败
- **同步返回「需要完整同步」** ← 最高优先级
- collection 文件锁获取失败
- 磁盘剩余 < 10%
- 每日 09:00 推送一条运行简报

### 8.4 首次初始化（一次性，对应决策 S11）

1. 在桌面 Anki 上手工创建 `VocabContext` note type 和目标牌组，调好卡片模板与样式
2. 桌面 Anki 设置「自定义同步服务器」指向自建 sync server，同步上去
3. 服务端首次启动时从 sync server **下载**这份 collection 作为起点
4. 之后服务端只读校验 schema（D3），永不修改

比在代码里用 API 拼 note type 可靠得多，也符合 D3「服务端不改 schema」的原则。

---

## 9. 实施阶段划分

### P0 — 最小闭环（目标：一周内能用）

- [x] Server：`POST /v1/items` + inbox 表 + AnkiWriter（无 enrichment，直接用原词）
- [x] Server：debounce 同步 + 文件锁 + `/healthz`
- [x] Chrome 插件：双击取词 + `Intl.Segmenter` 切句 + 直接提交 + 设置页
- [x] docker-compose + anki-sync-server 起服务，宿主机 cloudflared 发布两条 hostname（含 §8.4 初始化流程）
- [x] L1/L2/L3 备份脚本

**验收：** 网页双击一个词，2 分钟内 AnkiDroid 同步后能看到带语境的卡。

已由 `tests/test_acceptance.py` 自动验证：起真实 sync server → 走 §8.4 bootstrap →
调 `POST /v1/items` → 等 debounce 同步自行触发 → 第二个 collection 同步下来，
断言卡片、语境、挖空正面、所在牌组都正确。实现说明见 `README.md`。

### P1 — 质量提升

- [x] 词形还原 + ECDICT 查词 + CC-CEDICT 中文查词 + `GET /v1/lookup`
- [x] 插件查词卡片 UI（释义预览、可编辑句子、已认识按钮）
- [x] 同词合并逻辑（P0 已完成）
- [x] 离线队列 + 幂等
- [x] 词频过滤

由 `tests/test_p1.py`（41 项）与 `tests/test_queue.js`（16 项）验证。

**实现修正：** 词形还原以 ECDICT 的 `exchange` 变形表为准，lemminflect 仅在词典
查不到时兜底，且加了一条保守规则 —— 原词若出现在自身的 lemma 集合中则视为已是原型，
不再还原。否则 `better` 会变成 `good`、`whose` 会变成 `who`，卡片上就成了另一个词。

**enrichment 位置：** §5.2 把 enrichment 画成独立 worker，P1 实际做成了请求内同步执行 ——
本地 SQLite 查词是亚毫秒级的，异步队列只增加复杂度。等 P2 的 TTS 和 P3 的 LLM 落地
（慢且会失败）再拆出 worker。

### P2 — Telegram Bot

- [ ] Webhook + 白名单 + 查词流程
- [ ] `/v1/translate` + 句子流程 + inline keyboard 选词
- [ ] 定期提醒
- [ ] 随机抽查（抽查模式）
- [ ] TTS 发音（edge-tts + `sync_media()` 媒体同步验证）

### P3 — 可选增强

- [ ] LLM 语境化释义
- [ ] AnkiConnect 兼容层（接入 Yomitan）
- [ ] 插件高亮已收集词
- [ ] 每周复盘报告（本周采集词 + 掌握情况）

---

## 10. 风险与缓解

| ID | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | `anki` 库跨版本 API 变更（尤其 sync 相关签名） | 高 | 在 `pyproject.toml` 中钉死精确版本、`uv.lock` 锁定传递依赖，Dockerfile 读同一份 lock 以免开发与生产漂移；写一个 smoke test 覆盖 add/update/find/sync 四个调用；升级前先在副本上跑 |
| R2 | 同步冲突要求 full sync | 高 | 遵守 D3（不改 schema）；只告警不自动执行；有 L3 apkg 兜底 |
| R3 | Collection 并发打开导致损坏 | 高 | 文件锁 + 单 worker + 队列串行 |
| R4 | 句子提取质量差（导航栏、图注、断行） | 中 | `low_confidence` 标记 + 插件内可编辑 + 服务端最小长度校验 |
| R5 | 采集过量，牌组失控 | 中 | D5 显式确认 + 词频过滤 + 每日采集上限告警 |
| R6 | Token 泄露 | 中 | Cloudflare 层速率限制 / WAF 规则；token 可随时轮换；AnkiConnect 兼容层默认关闭 |
| R7 | LLM 调用慢/失败拖累链路（P3 启用后） | 低 | Enrichment 异步且可降级；LLM 超时 3s 即放弃，用基础词典 |
| R8 | Telegram callback_data 64 字节限制 | 低 | 服务端短期缓存 + 短 key |
| R9 | Cloudflare 免费版单请求体上限 100MB | 低 | 正常同步为增量/分块传输，不受影响；collection full upload 超限时在内网直连 sync server 处理 |
| R10 | `anki` wheel 为 manylinux_2_35，要求 glibc ≥ 2.35 | 低 | 基础镜像用 debian bookworm（glibc 2.36）；Alpine/musl 无 wheel，不可用 |

---

## 11. 决策记录

评审意见已全部合入正文，此处仅留档。

| # | 议题 | 决策 |
|---|---|---|
| S1 | inbox 作为真相源，而非直接写 Anki | ✅ 采纳（§D1） |
| S2 | 不全自动入库 | ✅ 采纳；查词不自动入库，但入库必须一键完成（§D5） |
| S3 | 卡片正面用挖空句而非孤立单词 | ✅ 采纳（§4.2） |
| S4 | 同词合并，而非重复建卡 | ✅ 采纳（§D4） |
| S5 | Bot 复习先做「抽查」，不写回 Anki | ✅ 采纳（§D6） |
| S6 | 生词候选按词频过滤，阈值可调 | ✅ 采纳（§5.3） |
| S7 | Chrome 插件不做全文翻译 | ✅ 采纳（§1.2） |
| S8 | 从一开始就存原始 payload | ✅ 采纳，`items.raw_payload`（§4.1） |
| S9 | 暂存区批量确认 | ⏸ 暂不做；inbox 天然支持（`status=staged`），跑一个月后按牌组质量再评估 |
| S10 | PDF 阅读支持 | ❌ 不在范围内（§1.2） |
| S11 | collection 初始化：桌面 Anki 手工建好 note type，服务端从同步端下载 | ✅ 采纳（§8.4） |
| B1 | 多语言 | 固定英→中；额外支持查中文词（CC-CEDICT，§5.3、§7.2） |
| B2 | LLM 语境化释义 | 暂不做，推迟到 P3；note type 字段预留（§4.2） |
| B3 | TTS 发音 | ✅ 做，edge-tts，P2 落地（media 同步经自建 sync server 验证） |
| B4 | 服务器规格（≥ 1GB RAM） | ✅ 已确认无问题 |
| B5 | 同步方案 | 自建 anki-sync-server（v0.2 曾定 AnkiWeb，因其限制改回自建，见 §D7） |
| B6 | 依赖版本 | 钉住最新版，见附录 A |
| B7 | 入口层 | 不用 Caddy，用宿主机已有的 cloudflared：compose 只在 127.0.0.1 暴露端口，公网 HTTPS 发布由用户在 Cloudflare 侧配置（§8.1） |

---

## 附录 A — 关键依赖版本

按「钉住最新版」原则管理：以下为 2026-08-25 的 PyPI 最新版本快照。依赖用 uv 管理，直接依赖钉在 `pyproject.toml`、传递依赖锁在 `uv.lock`；升级前在副本上跑 smoke test（R1）。

```
anki==26.8.1                 # Writer 与 sync server 共用此版本，需与 AnkiDroid/Desktop 兼容
fastapi==0.141.1
uvicorn[standard]==0.52.4
lemminflect==0.2.3
python-telegram-bot==22.8
apscheduler==3.11.3
httpx==0.28.1
edge-tts==7.2.8
```
