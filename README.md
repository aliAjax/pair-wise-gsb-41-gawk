# 巨灾保险理赔调度系统

标准库实现的巨灾理赔受理、分级、查勘、复核、紧急预付和最终核定服务，数据保存到 SQLite。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8207`，默认数据库 `catastrophe_claims.db`。可通过 `--db`、`--host`、`--port` 修改。

## 主要接口

请求头 `X-User`、`X-Role` 表示用户与角色。角色有 `intake`、`adjuster`、`surveyor`、`supervisor`、`auditor`。

- `GET /health`、`GET /api/state`、`GET /api/queue`
- `POST /api/claims`：创建报案并识别重复报案
- `POST /api/claims/triage`：计算优先级和欺诈风险
- `POST /api/claims/assign`：分配查勘人员
- `POST /api/evidence`：添加证据并识别跨案件批量复用
- `POST /api/claims/survey`、`POST /api/claims/submit-review`
- `POST /api/claims/emergency-advance`：仅限监督人员、紧急且未超20%的案件
- `POST /api/claims/finalize`：锁定最终核定结果

### 事件时效中止

台风/地震等灾害期间，受灾区域在办赔案的处理时限**停表**，避免理赔员联系不上客户被判逾期。

- `POST /api/events`：主管登记事件（`code`、`name`、`peril_type`、`region`、`started_at`，可选 `note`、`ended_at` 解除时间），区域内非终态案件自动置为 `suspended`
- `POST /api/events/lift`：主管解除事件，按**实际暂停时长**顺延当前期限；区域内无其他生效事件时自动重开案件，可继续办理
- `POST /api/claims/reopen`：事件全部解除后主管手动重开
- `GET /api/events`：事件列表；`GET /api/claims/{id}/detail`：案件详情（原期限、暂停区间、当前期限、命中事件、时间线）

时效规则（实现见 `clocks.py`，纯函数）：

- 原期限 = 受理时间 `created_at` + 15 天；
- 暂停区间 = 事件时间窗 ∩ [受理时间, 结案/当前时间]，且案件区域必须相同；
- 多个重叠事件先**合并区间取并集**再计时，重叠天数不重复增加；
- 当前期限 = 原期限 + 累计暂停秒数；停表期间不判逾期，终态案件不再计时效。

队列（`GET /api/queue`、`GET /api/state`）每条案件附带 `clock` 字段：`original_deadline`、`pause_segments`、`paused_days/paused_seconds`、`current_deadline`、`clock_stopped`、`overdue`。中止期间分级、分配、证据、查勘、核损、预付、核定等写操作统一返回 409。

## 代码分层

- `storage.py`：资料层，SQLite 建表/迁移（`disaster_events`、`claims.pre_suspend_status`）与数据访问
- `clocks.py`：时效层，原期限/暂停区间合并/当前期限的纯计算
- `app.py`：接口与业务编排层（标准库 HTTP 服务）
- `static/`：页面层，`index.html`（结构）、`styles.css`（样式）、`app.js`（交互）分开

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整赔付流程、重复报案、乐观锁冲突、批量伪证识别和角色权限。

## 局限

认证依赖请求头；证据仅校验提交的 SHA-256，不实际保存附件；欺诈规则是原型规则而非精算模型；支付记录可审计，但不连接真实银行、保险核心或气象灾害数据源。
