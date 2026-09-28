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

### 灾害事件时效中止（台风/地震等）

时效规则独立落在 `suspension.py`（纯时间计算，无 IO），数据落在 `suspension_events` 表：

- 案件受理后 **15 天**为原期限；主管登记事件（编号、名称、类型、受灾区域、开始时间）后，
  区域内（精确匹配或辖区前缀包含）案件自事件开始时**停表**，进行中的事件不会判逾期。
- 主管解除事件时按**实际暂停时长**顺延当前期限；多个事件重叠/相邻时区间先求并集，
  **重复天数不重复增加**；事件早于受理时间的部分自动裁剪，期限已走完后才开始的事件不救济。
- 已结案件（approved/rejected/closed）可由主管 `reopen`，受理时间与已累积顺延保持不变，
  可继续补证据、重新分配、查勘、核损、最终核定。

接口：

- `POST /api/events/suspensions`：主管登记事件（`event_no,name,kind,regions[],started_at[,ended_at]`，时间为 ISO 8601）
- `POST /api/events/lift`：主管解除事件（`event_no[,ended_at]`，默认按当前时间），返回每个受影响案件的原期限/当前期限/暂停秒数
- `GET /api/events`：查看全部事件（含 `active`）
- `GET /api/claims/{id}/detail`：案件详情，含原期限、暂停区间（逐段时长与覆盖事件）、
  当前期限、是否停表/逾期、关联事件、资料（证据/查勘/付款）与完整时间线
- `POST /api/claims/reopen`：主管重开已结案件（`claim_id,reason`）
- 队列 `GET /api/queue` 与状态 `GET /api/state` 的每条案件均内嵌 `deadline` 对象
  （`original_deadline`、`current_deadline`、`paused_seconds`、`suspended`、`overdue`、`windows`），
  队列按停表优先、当前期限先后排序；时间线包含 `suspension.registered` / `suspension.lifted` 全局审计记录。

页面 `/`：队列展示受理时间、原期限、当前期限、累计暂停与停表/逾期标记；
可登记/解除事件；点案件看暂停区间、关联事件、资料、时间线，并支持主管重开。


## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整赔付流程、重复报案、乐观锁冲突、批量伪证识别和角色权限。

## 局限

认证依赖请求头；证据仅校验提交的 SHA-256，不实际保存附件；欺诈规则是原型规则而非精算模型；支付记录可审计，但不连接真实银行、保险核心或气象灾害数据源。
