# 设备告警分派演练（双查询执行器 · Flask 单进程）

模拟两台查询执行器 **M1 / M2** 上告警与台账分散存储时的接单、容量预留、
跨机额度校验与告警→责任班组关联流程。纯 Flask 单进程应用，后台用线程模拟
两台执行器的执行时序。

## 业务规则

- **开场设置**：为每台执行器设置「可同时加载的台账行数上限」，无活动工单时
  可在页面调整。
- **录单**：每单分别录入两台的
  - 告警：每行 `告警号,设备号`（本单告警号全单互异）
  - 台账：每行 `设备号,班组`（台账设备号全单互异）
  - 本单跨机行数额度；全单最多涉及 12 个设备。
- **台账只加载一次**：每台为同一单只加载一次本机房台账，接单瞬间按加载行数
  **立即预留**该台剩余台账容量。
- **跨机读取计额度**：告警正文或台账行需要从另一台读取时，每行计 1 次跨机
  读取。分派采用最低跨机成本方案（成本相同时优先告警所在机）。
- **接单判定**：存在同时满足「本单跨机额度」与「两台剩余台账容量」的安排才
  接单并预留容量，否则返回「额度不足」且不占用任何容量。
- **暂停**：可勾选台账加载后暂停；暂停期间仍可提交其他工单。暂停中可设置
  失败模拟，然后点继续。
- **失败模拟**：一台先算完、另一台失败时**整单无结果**（不输出任何告警行）。
- **释放预留**：成功或失败结束时都释放两台预留容量。
- **结果**：每个告警只出一行；查不到设备台账时责任班组留空。页面展示各台
  加载行数与本单跨机实读行数（含跨机告警/跨机台账明细）。

## 目录结构

```
app/
  engine.py            纯领域逻辑：解析、校验、最低成本分派与额度/容量判定
  store.py             线程安全的工单存储、容量预留、后台执行线程（暂停/失败）
  server.py            Flask 路由与 JSON API
  templates/index.html 单页界面
  static/              前端 JS / CSS
tests/                 unittest 测试（Flask 缺失时自动跳过 API 测试）
Dockerfile
docker-compose.yml
```

## Docker Compose 启动

```bash
docker compose up --build
# 打开 http://localhost:5000
```

开场容量由环境变量 `DEFAULT_LEDGER_CAP` 控制（默认 20），可在
`docker-compose.yml` 中修改。

## 本地运行（无 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m flask --app app.server:create_app run --with-threads --port 5000
```

## 测试

```bash
python -m unittest discover -s tests -v
```

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 两台容量/预留/剩余 + 全部工单快照 |
| POST | `/api/capacity` | `{machine, capacity}` 调整台账上限（无活动工单时） |
| POST | `/api/orders` | 提交工单（见下） |
| POST | `/api/orders/<id>/resume` | 继续暂停中的工单 |
| POST | `/api/orders/<id>/fail` | `{machine}` 暂停中设置失败模拟 |

提交工单请求体：

```json
{
  "quota": 3,
  "pause_after_load": true,
  "alarms": {"M1": "A001,D01", "M2": ""},
  "ledger": {"M1": "D01,甲班", "M2": "D02,乙班"}
}
```
