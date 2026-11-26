# 灰链 HUILIAN · MVP

iGaming B2B 服务商目录与推广平台，面向菲律宾市场：服务商付费上架和推广，PAGCOR 持牌运营商免费浏览和咨询，平台负责审核资质与收取推广费。

技术栈：Python 3.11+、Flask、SQLite，界面中英双语、手机优先。整个项目没有前端构建步骤，装好 3 个依赖就能跑。

## 已包含的功能

| 模块 | 内容 |
| --- | --- |
| 首页目录 | 「服务商 / 推荐」两个 tab、四个类别按钮（白标、支付、换汇、营销）、卡片每行 2 张、付费卡片标「广告」、推荐 tab 显示推荐位横幅 |
| 服务商详情 | Logo、资质核验栏、公司介绍、服务列表、公司资料、发送咨询 |
| 注册登录 | 运营商 / 服务商两种账号，上传牌照或注册证书；支付和换汇另需上传 AML/KYC 政策；服务商需勾选「只服务持牌客户」承诺；忘记密码（邮件链接，1 小时有效、只能用一次） |
| 咨询 | 运营商的牌照通过审核后才能发咨询；必须勾选同意分享资料；双方在「我的」里对话，可以关闭咨询 |
| 我的 | 运营商看「我的咨询」；服务商看收件箱和近 30 天的咨询数、回复率、浏览、曝光，可以编辑主页、买推广位、看账单、续传资质 |
| 推广位 | 推荐位、类别置顶、运营商月刊按月预订，有名额上限；下单即生成账单，7 天内没付款就自动释放；推荐位横幅要先经过审核 |
| 管理后台 | 审核队列（通过 / 驳回 / 要求补件）、推广位档期、账单（确认到账 / 作废）、资质复核提醒、公司暂停 / 下架 / 恢复、审计日志 |
| 担保协议 | 服务商在咨询对话里发起协议（内容、币种、付款条款、1–6 个阶段）；运营商确认后条款锁定（SHA-256 条款指纹）；每个阶段走「交付 → 验收」「付款 → 确认收款」；全部完成后协议自动结束；任一方可以发起争议、上传证据，由管理员仲裁并记录裁定；所有步骤写入哈希串连的记录，改动任何一条都会被检测到；服务商主页显示担保记录（完成 / 进行中 / 争议笔数）。**平台只做记录与仲裁，不经手资金** |
| Telegram 担保机器人 | 用户在「我的 › Telegram 机器人」用一次性连接码绑定；对方每一步操作都会推送到 Telegram；一键按钮确认条款、标记交付、确认验收、确认收款；`/deals`、`/deal 12`；在群里发 `/bind 12`，协议进展就会推送到群里 |
| 自动规则 | 资质过期后自动暂停该公司并从目录隐藏；逾期没付款的预订自动取消 |

## 在本机运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # 填 SECRET_KEY
flask --app huilian seed        # 建表 + 推广位 + 管理员 + 示例数据
flask --app huilian run         # 打开 http://127.0.0.1:5000
```

示例账号（密码都是 `DemoPass12345`）：

| 角色 | 邮箱 |
| --- | --- |
| 管理员 | `admin@huilian.local`（密码 `ChangeMe12345`） |
| 已验证运营商 | `operator@demo.local` |
| 待审核运营商 | `pending@demo.local` |
| 服务商 A–F | `suppliera@demo.local` … `supplierf@demo.local` |

跑测试：`pip install pytest && pytest`（共 21 个端到端测试，包含担保流程和机器人）。

## 上线部署

**正式环境不要跑 `seed`**（`seed` 会放示例数据）。改用下面这条，只建推广位和一个管理员账号：

```bash
flask --app huilian seed-prod --email 你的邮箱 --password 一个够长的密码
```

方式一，Docker（推荐）：

```bash
docker build -t huilian .
docker run -d -p 8000:8000 -v huilian-data:/data --env-file .env huilian
docker exec -it <容器> flask --app huilian seed-prod --email ... --password ...
```

方式二，普通 VPS：`gunicorn -w 2 -b 0.0.0.0:8000 wsgi:app`，前面用 Nginx 或 Caddy 配 HTTPS。

上线前必做：

1. `.env` 里设置长随机的 `SECRET_KEY`、`BASE_URL`，并设 `SESSION_COOKIE_SECURE=1`。
2. 配好 SMTP，否则邮件只会存进数据库的 `outbox` 表，不会真正发出。
3. 每天跑一次 `flask --app huilian enforce`（用 cron），用来让过期资质失效、暂停对应公司、释放没付款的预订。打开管理后台时也会自动跑一次。
4. 每天备份 `/data` 目录（里面是数据库和所有上传的证件）。

## 用 Render 上线（手机就能操作）

1. 代码推到 GitHub（私有仓库）。
2. Render › New › Blueprint › 选这个仓库。Render 会读取 `render.yaml`，自动建好服务和 1GB 磁盘（Starter 付费方案，新加坡机房）。
3. 按提示填写：`BASE_URL`（例如 `https://huilian.onrender.com`，绑定域名后再改成 `https://huilian.ph`）、`ADMIN_EMAIL`、`ADMIN_PASSWORD`（至少 10 位）。SMTP 和 Telegram 的设置可以之后再补。
4. 部署完成后，用管理员账号登录。每次启动都会自动建表、建推广位和管理员，不需要进命令行。
5. 每日规则（资质过期、逾期释放档期）由每天的第一个访问自动触发，不需要设定时任务。Render 每天会自动给磁盘做快照。
6. 绑定自己的域名：Render › Settings › Custom Domains 加入域名，再到域名商那里按提示加 CNAME 记录。
7. Telegram：填好 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_BOT_USERNAME` 之后，在管理后台左下角点「连接 Telegram 机器人」。

## 设置 Telegram 机器人

1. 在 Telegram 找 @BotFather，用 `/newbot` 建一个机器人，拿到 token 和用户名。
2. 在 `.env` 填写 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_BOT_USERNAME`（不带 @），以及一串随机的 `TELEGRAM_WEBHOOK_SECRET`。
3. 网站以 HTTPS 上线后，执行 `flask --app huilian tg-set-webhook`，Telegram 就会把消息推送到 `BASE_URL/tg/webhook`。
4. 想让机器人在群里收到 `/bind` 命令：在 BotFather 里用 `/setprivacy` 关闭隐私模式，或者在群里用 `/bind@机器人用户名 12` 的写法。

机器人不需要额外安装任何套件，直接用 Python 标准库调用 Telegram 接口。

## 常改的地方

| 要改什么 | 改哪里 |
| --- | --- |
| 推广位价格和名额 | 数据库 `ad_slots` 表（`price_php`、`capacity`）；新装的默认值在 `huilian/seed.py` 的 `SLOTS` |
| 收款账号说明 | `huilian/i18n.py` 里的 `pay_instructions` |
| 所有界面文字（中英） | `huilian/i18n.py` |
| 各类别要求的资质 | `huilian/core.py` 的 `CREDENTIALS_FOR`、`NEEDS_AML` |
| 默认复核周期 | `huilian/core.py` 的 `REVIEW_DEFAULT_DAYS`（运营商 30 天，其他 365 天） |
| 颜色和样式 | `huilian/static/style.css` |
| 中文品牌名 | `huilian/i18n.py` 的 `brand`，以及模板里的「灰」字 logo |

## 第一版还没做（建议下一步）

- RR Pay 到账 webhook 自动确认（目前由管理员在后台手动点「确认到账」）
- 管理员两步验证、登录失败次数限制
- 首批服务商 5 折这类折扣码（目前要手动作废账单再重开）
- 运营商月刊的实际发送
- 用户数量变多后，把 SQLite 换成 PostgreSQL

## 目录结构

```
huilian/
  __init__.py   app 工厂、CSRF、命令行
  auth.py       注册、登录、重设密码、权限
  public.py     首页、服务商详情、咨询、文件
  me.py         「我的」、对话、主页编辑、推广位、账单、资质
  admin.py      管理后台
  deals.py      担保协议规则（状态、哈希记录、争议、仲裁）
  deals_views.py 担保协议网页、Telegram 连接、仲裁后台
  telegram.py   Telegram 担保机器人
  core.py       业务规则（资质有效性、广告位、自动规则、邮件、上传）
  i18n.py       中英文案
  schema.sql    数据表
  seed.py       初始数据
  templates/    页面
  static/       样式
tests/          端到端测试
```
