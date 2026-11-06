"""迁移框架（DEV-FND-006）。

- `schema_migrations` 记录版本、SQL 校验和与应用时间；
- 每个迁移在单事务中完成，校验失败自动回滚；
- 已应用迁移的 checksum 不一致时拒绝启动（防篡改 / DB-004）。

V1 覆盖本迭代核心表：workspaces / cases / documents / document_versions /
document_pages（技术方案 §13.1 的子集，其余表在对应迭代加入）。
注意：text_content 本迭代为明文列；密钥环（DEV-FND-007）就绪后迁移为加密列。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database

V1__WORKSPACE_CASES_DOCUMENTS = """
CREATE TABLE workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  root_uri TEXT NOT NULL UNIQUE,
  encryption_key_ref TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE cases (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  title TEXT NOT NULL,
  case_type TEXT NOT NULL CHECK (case_type IN ('criminal','civil','commercial','administrative','contract','other')),
  client_name TEXT,
  client_role TEXT,
  stage TEXT,
  default_mode TEXT NOT NULL DEFAULT 'legal_review' CHECK (default_mode IN ('legal_review','contract_review','document_generation')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft','active','restricted','archived','pending_delete')),
  pinned INTEGER NOT NULL DEFAULT 0,
  permission_preset TEXT NOT NULL DEFAULT 'strict_local',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT
);

CREATE INDEX idx_cases_ws_status ON cases(workspace_id, status, updated_at DESC);

CREATE TABLE documents (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE RESTRICT,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  mime TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','security_check','need_password','decrypting','parsing','ocr_queued','ocr_running','indexing','ready','partial','locked','stale','failed')),
  active_version_id TEXT,
  sensitivity TEXT NOT NULL DEFAULT 'normal' CHECK (sensitivity IN ('normal','sensitive','highly_sensitive')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_documents_case ON documents(case_id, updated_at DESC);

CREATE TABLE document_versions (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  parent_id TEXT REFERENCES document_versions(id),
  blob_uri TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  parse_status TEXT NOT NULL DEFAULT 'pending' CHECK (parse_status IN ('pending','parsing','ready','partial','failed')),
  corpus_version INTEGER NOT NULL DEFAULT 1,
  page_count INTEGER,
  origin TEXT NOT NULL DEFAULT 'original' CHECK (origin IN ('original','work_copy','ocr_fix','redacted','history')),
  error_text TEXT,
  created_by_task TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_dv_document ON document_versions(document_id, created_at DESC);

CREATE TABLE document_pages (
  id TEXT PRIMARY KEY,
  version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  page_no INTEGER NOT NULL,
  printed_page TEXT,
  text_content TEXT,
  text_quality TEXT NOT NULL DEFAULT 'native' CHECK (text_quality IN ('native','ocr','ocr_pending','empty')),
  ocr_status TEXT NOT NULL DEFAULT 'none' CHECK (ocr_status IN ('none','pending','running','done','failed')),
  UNIQUE (version_id, page_no)
);
"""

V2__AUDIT_LOGS = """
CREATE TABLE audit_logs (
  seq INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'local_owner',
  action TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  case_id TEXT,
  result TEXT NOT NULL DEFAULT 'ok' CHECK (result IN ('ok','denied','failed')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  prev_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL
);

CREATE INDEX idx_audit_case ON audit_logs(case_id, seq);
CREATE INDEX idx_audit_action ON audit_logs(action, seq);
"""

V3__FTS_INDEX = """
CREATE VIRTUAL TABLE pages_fts USING fts5(
  text_content,
  case_id UNINDEXED,
  document_id UNINDEXED,
  document_title UNINDEXED,
  version_id UNINDEXED,
  page_no UNINDEXED,
  text_quality UNINDEXED,
  tokenize = 'trigram'
);

INSERT INTO pages_fts(text_content, case_id, document_id, document_title, version_id, page_no, text_quality)
SELECT p.text_content, d.case_id, d.id, d.title, p.version_id, p.page_no, p.text_quality
FROM document_pages p
JOIN document_versions v ON v.id = p.version_id
JOIN documents d ON d.id = v.document_id
WHERE p.text_content IS NOT NULL AND p.text_content != '';
"""

V4__TEXT_BLOCKS = """
CREATE TABLE text_blocks (
  id TEXT PRIMARY KEY,
  page_id TEXT NOT NULL REFERENCES document_pages(id) ON DELETE CASCADE,
  block_order INTEGER NOT NULL,
  type TEXT NOT NULL DEFAULT 'text',
  bbox_json TEXT,
  text_enc TEXT,
  confidence REAL,
  block_hash TEXT,
  UNIQUE (page_id, block_order)
);

CREATE INDEX idx_blocks_page ON text_blocks(page_id, block_order);

ALTER TABLE document_pages ADD COLUMN ocr_confidence REAL;
"""

V5__AGENT_TASKS = """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE RESTRICT,
  mode TEXT NOT NULL CHECK (mode IN ('legal_review','contract_review','document_generation')),
  lifecycle TEXT NOT NULL DEFAULT 'CREATED' CHECK (lifecycle IN
    ('CREATED','PLANNING','RUNNING','WAITING_APPROVAL','WAITING_HUMAN','PAUSED',
     'CANCELLING','CANCELLED','VERIFYING','COMPLETED','COMPLETED_WITH_WARNINGS','FAILED')),
  stage TEXT,
  request_text TEXT NOT NULL,
  selected_document_ids_json TEXT,
  client_position_json TEXT,
  corpus_version INTEGER,
  model_policy_json TEXT,
  tool_policy_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE INDEX idx_tasks_case ON tasks(case_id, created_at DESC);

CREATE TABLE task_events (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE (task_id, seq)
);

CREATE TABLE agent_checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  node TEXT NOT NULL,
  state_json TEXT NOT NULL,
  parent_id TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_checkpoints_task ON agent_checkpoints(task_id, created_at DESC);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

V6__APPROVAL_BILLING = """
CREATE TABLE approval_requests (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  case_id TEXT NOT NULL,
  capability TEXT NOT NULL,
  target TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  data_summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','denied','expired')),
  decision TEXT,
  decided_at TEXT,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_approval_task ON approval_requests(task_id, status);

CREATE TABLE permission_grants (
  id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('once','task','case')),
  scope_id TEXT NOT NULL,
  capability TEXT NOT NULL,
  payload_hash TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT
);

CREATE INDEX idx_grants_lookup ON permission_grants(capability, scope_type, scope_id);

CREATE TABLE model_calls (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  input_hash TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  credits_charged INTEGER,
  status TEXT NOT NULL CHECK (status IN ('succeeded','failed','blocked')),
  error_text TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE model_credit_rates (
  model_id TEXT PRIMARY KEY,
  multiplier_input INTEGER NOT NULL,
  multiplier_output INTEGER NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  effective_from TEXT NOT NULL
);

CREATE TABLE credit_batches (
  id TEXT PRIMARY KEY,
  order_ref TEXT,
  reason TEXT NOT NULL DEFAULT 'purchase',
  credits_total INTEGER NOT NULL,
  credits_remaining INTEGER NOT NULL,
  granted_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','exhausted'))
);

CREATE INDEX idx_batches_fefo ON credit_batches(status, expires_at);

CREATE TABLE credit_transactions (
  id TEXT PRIMARY KEY,
  batch_id TEXT REFERENCES credit_batches(id),
  model_call_id TEXT REFERENCES model_calls(id),
  delta INTEGER NOT NULL,
  balance_after INTEGER NOT NULL,
  reason TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL
);
"""

V7__CLAIM_LEDGER = """
CREATE TABLE source_refs (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  document_version_id TEXT NOT NULL,
  page_no INTEGER NOT NULL,
  quote TEXT NOT NULL,
  quote_norm TEXT NOT NULL,
  ocr_confidence REAL,
  corpus_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_sourcerefs_doc ON source_refs(document_id, page_no);

CREATE TABLE claims (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  case_id TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('fact','law','inference','recommendation')),
  text TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('supported','partially_supported','conflict','needs_human','stale')),
  reason_code TEXT,
  confidence_basis TEXT CHECK (confidence_basis IN ('exact_quote','multiple_sources','user_confirmed')),
  created_at TEXT NOT NULL
);

CREATE INDEX idx_claims_task ON claims(task_id, status);
CREATE INDEX idx_claims_case ON claims(case_id, status);

CREATE TABLE claim_sources (
  claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
  source_ref_id TEXT NOT NULL REFERENCES source_refs(id) ON DELETE CASCADE,
  relation TEXT NOT NULL DEFAULT 'supports',
  PRIMARY KEY (claim_id, source_ref_id)
);
"""

V8__WORKFLOW_PRESET = """
ALTER TABLE tasks ADD COLUMN workflow_preset TEXT;

CREATE INDEX idx_tasks_preset ON tasks(mode, workflow_preset);
"""

V9__ARTIFACTS_CONFLICT = """
CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  case_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('report','viz','evidence','strategy','points')),
  title TEXT NOT NULL,
  body_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_artifacts_case ON artifacts(case_id, created_at DESC);

-- 矛盾证据（R-004）：conflict 状态的 Claim 指向对侧 Claim
ALTER TABLE claims ADD COLUMN conflicts_with TEXT REFERENCES claims(id);

CREATE INDEX idx_claims_conflict ON claims(status, conflicts_with);
"""

V10__REDACTION_MCP = """
-- 脱敏映射（REQ-PRIV-002/003 / D-005 / D-010）：
-- original_enc 经 DPAPI 加密，明文映射不出现在普通数据库字段与日志；
-- 案件级隔离：不同案件各自编号，别名无跨案件可比信息。
CREATE TABLE redaction_entities (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL CHECK (entity_type IN
    ('id_card','bank_card','phone','email','credit_code','plate','case_number',
     'name','org','address','custom')),
  original_enc TEXT NOT NULL,
  alias TEXT NOT NULL,
  confidence REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed','needs_human')),
  created_at TEXT NOT NULL,
  UNIQUE (case_id, alias)
);
CREATE INDEX idx_redaction_case ON redaction_entities(case_id, entity_type);

-- 案件级自定义敏感词（PRD 12.3：用户自定义敏感词；案件隔离，不跨案泄漏）
CREATE TABLE redaction_terms (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  term TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (case_id, term)
);

-- 案件级脱敏策略档位（REQ-PRIV-004：轻度/标准/严格/自定义）
ALTER TABLE cases ADD COLUMN redaction_policy TEXT NOT NULL DEFAULT 'standard'
  CHECK (redaction_policy IN ('light','standard','strict','custom'));
ALTER TABLE cases ADD COLUMN redaction_custom_types_json TEXT;

-- MCP 服务器白名单（02 技术方案 §12 / §13.1；凭据不入本表，存 settings 经 DPAPI 加密）
CREATE TABLE mcp_servers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  allowed_tools_json TEXT NOT NULL DEFAULT '[]',
  last_status TEXT NOT NULL DEFAULT 'unknown' CHECK (last_status IN ('unknown','available','offline','error')),
  last_error TEXT,
  last_checked_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_mcp_enabled ON mcp_servers(enabled);
"""

V11__CONTRACT_REVIEW = """
-- 合同审查（PRD §14 / DEV-MOD-005/006 / 技术方案 §5.2、§6.3）：
-- 审查会话、条款切分、风险项、接受/拒绝/撤销操作历史、工作副本版本。
CREATE TABLE contract_reviews (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  primary_document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
  primary_version_id TEXT REFERENCES document_versions(id),
  reviewer_role TEXT NOT NULL DEFAULT 'custom'
    CHECK (reviewer_role IN ('seller','buyer','lessor','lessee','lender','borrower','employer','contractor','custom')),
  custom_role TEXT,
  risk_appetite TEXT NOT NULL DEFAULT 'balanced'
    CHECK (risk_appetite IN ('conservative','balanced','aggressive')),
  contract_type TEXT,
  transaction_goal TEXT,
  governing_law TEXT,
  status TEXT NOT NULL DEFAULT 'analyzing' CHECK (status IN ('analyzing','completed','failed')),
  degraded_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_contract_reviews_case ON contract_reviews(case_id, updated_at DESC);

-- 条款切分结果（contract.split_clauses 工具输出 / PRD 14.3 步骤3）
CREATE TABLE contract_clauses (
  id TEXT PRIMARY KEY,
  review_id TEXT NOT NULL REFERENCES contract_reviews(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  clause_ref TEXT NOT NULL,
  heading TEXT,
  text TEXT NOT NULL,
  page_no INTEGER,
  char_start INTEGER,
  char_end INTEGER,
  refs_json TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (review_id, seq)
);

-- 风险项（PRD 14.4：等级/位置/原文/说明/影响/修改建议/建议替换文本/依据/置信/处理状态）
CREATE TABLE contract_risk_items (
  id TEXT PRIMARY KEY,
  review_id TEXT NOT NULL REFERENCES contract_reviews(id) ON DELETE CASCADE,
  claim_id TEXT REFERENCES claims(id) ON DELETE SET NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('severe','high','medium','low')),
  category TEXT NOT NULL DEFAULT 'legal'
    CHECK (category IN ('business','legal','completeness')),
  clause_id TEXT REFERENCES contract_clauses(id) ON DELETE SET NULL,
  clause_ref TEXT NOT NULL,
  original_text TEXT NOT NULL,
  risk_note TEXT NOT NULL,
  impact TEXT,
  suggestion TEXT,
  replacement_text TEXT,
  basis TEXT,
  confidence TEXT NOT NULL DEFAULT 'medium'
    CHECK (confidence IN ('verified','high','medium','low')),
  decision TEXT NOT NULL DEFAULT 'pending'
    CHECK (decision IN ('pending','accepted','rejected','edited')),
  edited_text TEXT,
  decided_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_contract_risks_review ON contract_risk_items(review_id, risk_level);
CREATE INDEX idx_contract_risks_decision ON contract_risk_items(review_id, decision);

-- 接受/拒绝/撤销/重做操作历史（REQ-CON-007：操作可撤销；redo 重新应用 undone=1 的记录）
CREATE TABLE contract_actions (
  id TEXT PRIMARY KEY,
  review_id TEXT NOT NULL REFERENCES contract_reviews(id) ON DELETE CASCADE,
  risk_item_id TEXT NOT NULL REFERENCES contract_risk_items(id) ON DELETE CASCADE,
  action TEXT NOT NULL CHECK (action IN ('accept','reject','edit','undo','redo','reset')),
  before_decision TEXT NOT NULL,
  after_decision TEXT NOT NULL,
  before_text TEXT,
  after_text TEXT,
  undone INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_contract_actions_review ON contract_actions(review_id, created_at DESC);

-- 工作副本版本（REQ-CON-006：接受修改只写工作副本，不改原件；
-- 实际文本版本落 document_versions(origin='work_copy')，此处维护版本链与出处）
CREATE TABLE contract_work_copies (
  id TEXT PRIMARY KEY,
  review_id TEXT NOT NULL REFERENCES contract_reviews(id) ON DELETE CASCADE,
  version_no INTEGER NOT NULL,
  document_version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  base_version_id TEXT REFERENCES document_versions(id),
  applied_risk_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE (review_id, version_no)
);
"""

V12__DOC_GEN = """
-- 文书生成（PRD §15 / §16.3 / DEV-MOD-007/008 / 技术方案 §5.3、§6.3）：
-- 模板（导入解析留痕 REQ-TPL-004）、文书草稿（槽位/提纲/正文）、
-- 草稿版本（REQ-GEN-009 崩溃可恢复 / REQ-GEN-010 版本可查）。
-- 产物不进 artifacts（其 kind CHECK 为阅卷类）：草稿自身即数据源。
CREATE TABLE docgen_templates (
  id TEXT PRIMARY KEY,
  case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  doc_type TEXT NOT NULL,
  template_mode TEXT NOT NULL DEFAULT 'strict' CHECK (template_mode IN ('strict','reference')),
  content_md TEXT NOT NULL,
  fields_json TEXT NOT NULL DEFAULT '[]',
  source_name TEXT,
  source_sha256 TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','parse_failed')),
  parse_error TEXT,
  version_no INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_docgen_tpl_case ON docgen_templates(case_id, updated_at DESC);

CREATE TABLE docgen_drafts (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  template_id TEXT REFERENCES docgen_templates(id) ON DELETE SET NULL,
  template_version_no INTEGER,
  doc_type TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT '',
  outline_json TEXT,
  slots_json TEXT NOT NULL DEFAULT '{}',
  body_md TEXT,
  outline_confirmed INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'outline' CHECK (status IN ('outline','draft','confirmed')),
  blocking_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_docgen_drafts_case ON docgen_drafts(case_id, updated_at DESC);

CREATE TABLE docgen_draft_versions (
  id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL REFERENCES docgen_drafts(id) ON DELETE CASCADE,
  version_no INTEGER NOT NULL,
  body_md TEXT NOT NULL,
  outline_json TEXT,
  slots_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE (draft_id, version_no)
);
"""

V13__DOCUMENT_UNLOCK = """
-- 加密文档解锁（REQ-DOC-004/011/012/013 / 技术方案 §11.2）：
-- 原件（加密文件）保持不可变，解锁后的明文经 AES-256-GCM 重新加密后单独存放，
-- 解析/阅读统一取 COALESCE(unlocked_blob_uri, blob_uri)；
-- 父子关系由 parent_id 表达（REQ-DOC-014），原件的 sha256 始终对应 blob_uri。
ALTER TABLE document_versions ADD COLUMN unlocked_blob_uri TEXT;
"""

V14__BILLING_ORDERS = """
-- 套餐订单（REQ-BILL-001/002 / PRD §12.7）：
-- 当前支付通道为模拟实现（channel='mock'），接入第三方订阅平台（DEC-017）后
-- 由服务端回调驱动同样的状态流转：pending → paid → 发放积分批次。
CREATE TABLE billing_orders (
  id TEXT PRIMARY KEY,
  order_no TEXT NOT NULL UNIQUE,
  package_code TEXT NOT NULL,
  package_name TEXT NOT NULL,
  price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
  credits INTEGER NOT NULL CHECK (credits > 0),
  valid_months INTEGER NOT NULL DEFAULT 3,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','cancelled','expired')),
  channel TEXT NOT NULL DEFAULT 'mock',
  external_trade_no TEXT,
  idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL,
  paid_at TEXT
);

CREATE INDEX idx_orders_status ON billing_orders(status, created_at);

CREATE INDEX idx_batches_order ON credit_batches(order_ref);
"""

V15__MEMORY = """
-- 记忆分层（技术方案 §8.1 / DEV-AGT-007）：
-- session 会话工作记忆（任务期，任务终态后清理）、case 案件记忆（随案件，强制案件隔离）、
-- user 用户偏好（跨案件；kind 限定为偏好类且禁止携带 case_id，杜绝案件事实进入全局偏好，M-001）。
-- 知识库层按当前范围暂不实现（scope 预留扩展）。
CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  scope TEXT NOT NULL CHECK (scope IN ('session','case','user')),
  case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,
  task_id TEXT,
  kind TEXT NOT NULL,
  key_text TEXT NOT NULL,
  value_text TEXT NOT NULL,
  source_document_id TEXT,
  source_version_id TEXT,
  source_page_no INTEGER,
  source_quote TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','stale','superseded','deleted')),
  superseded_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_memories_case ON memories(case_id, status, updated_at DESC);
CREATE INDEX idx_memories_scope ON memories(scope, status);
CREATE INDEX idx_memories_source ON memories(source_document_id);
CREATE INDEX idx_memories_task ON memories(task_id);

-- 召回索引（trigram，与 pages_fts 一致）：删除/更正时同步维护（M-005）
CREATE VIRTUAL TABLE memories_fts USING fts5(
  memory_id UNINDEXED,
  case_id UNINDEXED,
  scope UNINDEXED,
  text,
  tokenize = 'trigram'
);
"""

V16__TASK_BUDGET = """
-- 任务预算与无进展控制（DEV-AGT-006 / 技术方案 §7；A-002/A-003/A-004/A-013）：
-- 轮次、墙钟、模型/工具调用次数、token 与积分六类上限；计数持久化以支持取消/恢复后连续计数。
CREATE TABLE task_budget (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  limits_json TEXT NOT NULL DEFAULT '{}',
  used_json TEXT NOT NULL DEFAULT '{}',
  no_progress_rounds INTEGER NOT NULL DEFAULT 0,
  stage TEXT NOT NULL DEFAULT 'ok' CHECK (stage IN ('ok','warn','blocked')),
  reasons_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL
);

-- 任务级预算覆盖（agent.start 入参 budget，缺省用系统默认上限）
ALTER TABLE tasks ADD COLUMN budget_json TEXT;
"""

V17__OCR_CORRECTION = """
-- OCR 人工更正（DEV-DOC-010 / O-008 / Q-009）：
-- 原件与页图不变；OCR 原值与更正值同时保留，更正历史追加式记录（原值不覆盖）。
-- 页文本 text_quality 维持 'ocr'（不新增 CHECK 值，避免重建表）；是否已更正由本表派生。
CREATE TABLE ocr_corrections (
  id TEXT PRIMARY KEY,
  page_id TEXT NOT NULL REFERENCES document_pages(id) ON DELETE CASCADE,
  document_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  original_text TEXT NOT NULL,
  corrected_text TEXT NOT NULL,
  original_confidence REAL,
  note TEXT,
  actor TEXT NOT NULL DEFAULT 'local_owner',
  created_at TEXT NOT NULL
);

CREATE INDEX idx_ocr_corrections_page ON ocr_corrections(page_id, created_at DESC);
CREATE INDEX idx_ocr_corrections_doc ON ocr_corrections(document_id, version_id);
"""

V18__EDITOR_SESSIONS = """
-- 派生文档编辑会话与草稿版本类型（DEV-MOD-009 / REQ-GEN-009/010、PRD §18.3）：
-- 版本 kind 区分 渲染/编辑/自动保存/恢复，支撑版本比较与恢复点；
-- 编辑会话 closed_at 为空表示上次未正常退出，由 recovery 入口提示恢复或放弃。
ALTER TABLE docgen_draft_versions ADD COLUMN kind TEXT NOT NULL DEFAULT 'render';

CREATE TABLE editor_sessions (
  id TEXT PRIMARY KEY,
  target_kind TEXT NOT NULL,
  target_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  last_autosave_at TEXT,
  closed_at TEXT,
  recovered_at TEXT,
  recovery_action TEXT
);

CREATE INDEX idx_editor_sessions_open ON editor_sessions(target_kind, target_id, closed_at);
CREATE INDEX idx_editor_sessions_case ON editor_sessions(case_id, recovered_at, opened_at DESC);
"""

V19__DOCUMENT_ANNOTATIONS = """
-- 文档页批注（DEV-DOC-010）：批注是派生数据，原件与页图不变；
-- quote 为可选原文锚定片段（用户选定/粘贴），定位语义与引用一致（Q-010 规范化）；
-- 是否仍可定位在读取时计算（quote_missing），页文本变化不静默错位；
-- 删除文档/版本时随 page 级联删除；审计只记长度与标记，不落正文。
CREATE TABLE document_annotations (
  id TEXT PRIMARY KEY,
  page_id TEXT NOT NULL REFERENCES document_pages(id) ON DELETE CASCADE,
  document_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  page_no INTEGER NOT NULL,
  quote TEXT,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
  resolved_at TEXT,
  actor TEXT NOT NULL DEFAULT 'local_owner',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_doc_annotations_page ON document_annotations(page_id, created_at DESC);
CREATE INDEX idx_doc_annotations_doc ON document_annotations(document_id, status, created_at);
CREATE INDEX idx_doc_annotations_case ON document_annotations(case_id);
"""

V20__TEMPLATE_VERSIONS = """
-- 模板版本与产物留痕（REQ-TPL-005 / G-002）：
-- root_id 标识同一模板的版本组（首个版本的 id），历史迁移行回填自身 id；
-- is_current 为组内当前版本指针：回退=移动指针，较新版本保留不删除；
-- 解析失败的导入不进入版本组（独立留痕行，REQ-TPL-004 语义不变）；
-- 草稿补 template_content_sha256：历史产物保存所用模板版本与内容哈希。
ALTER TABLE docgen_templates ADD COLUMN root_id TEXT;
UPDATE docgen_templates SET root_id = id WHERE root_id IS NULL;
ALTER TABLE docgen_templates ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1;
CREATE INDEX idx_docgen_tpl_root ON docgen_templates(root_id, version_no);

ALTER TABLE docgen_drafts ADD COLUMN template_content_sha256 TEXT;
"""

V21__SKILLS = """
-- 技能（提示词 + 允许工具白名单，非可执行代码）：由 Agent 经 skill_install
-- 从 https URL 拉取安装，工作空间级共享（REQ-AI-003 / REQ-AI-007）。
CREATE TABLE skills (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL,
  prompt_md TEXT NOT NULL,
  allowed_tools_json TEXT NOT NULL DEFAULT '[]',
  content_sha256 TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 任务级技能绑定：本次任务选中的技能 id 列表（JSON 数组）。
ALTER TABLE tasks ADD COLUMN skills_json TEXT;
"""

V22__MCP_AUTH_AND_HISTORY = """
-- MCP 认证头：连接器密钥随服务配置存储（SQLCipher 静态加密保护；
-- 输出与审计一律脱敏，仅记 hasAuth 与 header 名，不落密钥值）。
ALTER TABLE mcp_servers ADD COLUMN headers_json TEXT NOT NULL DEFAULT '{}';

-- 任务对话历史：最近几轮 user/assistant 文本随任务入库，
-- 支撑跨任务多轮流程（如 Agent 安装 MCP 时追问密钥后继续完成配置）。
ALTER TABLE tasks ADD COLUMN history_json TEXT;
"""

# V23：文档级脱敏派生（文档列表「脱敏处理」按钮）
# 派生脱敏版指向原文档（原始卷宗只读；脱敏不覆盖原件，AGENTS.md 硬约束）。
V23__DOCUMENT_REDACTED_COPY = """
ALTER TABLE documents ADD COLUMN origin_document_id TEXT REFERENCES documents(id) ON DELETE CASCADE;
CREATE INDEX idx_documents_origin ON documents(origin_document_id);
"""

# V24：脱敏改为原地打码（不再派生副本，节省存储与心智负担）
# 星号打码直接写入已解析页文本（document_pages），原件二进制不动；
# Agent 阅卷 / FTS 检索 / 文本视图统一使用打码后文本；清理历史派生副本。
V24__DOCUMENT_REDACT_INPLACE = """
ALTER TABLE documents ADD COLUMN is_redacted INTEGER NOT NULL DEFAULT 0;
DELETE FROM pages_fts WHERE document_id IN (SELECT id FROM documents WHERE origin_document_id IS NOT NULL);
DELETE FROM document_pages WHERE version_id IN (
    SELECT id FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE origin_document_id IS NOT NULL)
);
DELETE FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE origin_document_id IS NOT NULL);
DELETE FROM documents WHERE origin_document_id IS NOT NULL;
DROP INDEX IF EXISTS idx_documents_origin;
"""

# V25：页批注荧光标注色（红/黄/蓝）；NULL 为历史数据（前端按默认黄展示）。
# 纯展示属性：不参与 quote 定位与审计正文，非法值写入端过滤为 NULL。
V25__ANNOTATION_COLOR = """
ALTER TABLE document_annotations ADD COLUMN color TEXT;
"""

# V26：会话持久化（conversation）。此前跨任务上下文依赖前端 localStorage →
# history 参数 → 800 字/条截断的三级有损链路（合同/文书模式完全不传）；
# 现在每轮完整问答（user 全文 + assistant 终稿全文）落库，任务发起时从
# Core 读全量历史注入，localStorage 仅承载 UI 快照。
V26__CONVERSATIONS = """
CREATE TABLE conversations (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_conversations_case ON conversations(case_id, updated_at DESC);

CREATE TABLE conversation_messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content TEXT NOT NULL,
  task_id TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_conv_msgs ON conversation_messages(conversation_id, created_at);

ALTER TABLE tasks ADD COLUMN conversation_id TEXT;
"""

# V27：产物 kind 放宽，支持「file」（任务经沙箱落盘的文档自动登记进产物面板）
# ——缺该取值 CHECK 会拒绝写入且调用方静默吞异常，产物面板恒为空。
# SQLite 无法 ALTER CHECK：重建表迁移，数据原样保留。
# 注意：已发布的迁移文本不可就地修改（apply_migrations 校验 checksum 防篡改 /
# DB-004）。本版本曾于 2026-09-24 前被就地改过（追加 'chat' 并把 task_id 改可空），
# 导致既有工作空间 checksum 不一致而无法打开；现还原为本条原始文本，
# 相关变更改由 V28 承载。
V27__ARTIFACT_FILE_KIND = """
ALTER TABLE artifacts RENAME TO artifacts_old;

CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  case_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('report','viz','evidence','strategy','points','file')),
  title TEXT NOT NULL,
  body_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

INSERT INTO artifacts(id, task_id, case_id, kind, title, body_json, created_at)
  SELECT id, task_id, case_id, kind, title, body_json, created_at FROM artifacts_old;

DROP TABLE artifacts_old;
"""

# V28：在 V27 基础上再放宽两处（承接原被误加进 V27 的变更）
# - kind 追加「chat」：自主循环收尾的对话分析结论以 kind='chat' 落库；
# - task_id 放宽可空：孤儿补登记（artifact.list 自愈历史未登记文件）无任务归属。
# 同时重建 V27 重建表时被连带删除的 idx_artifacts_case（V9 建立，重命名后
# DROP TABLE artifacts_old 会连带删除其索引）。
V28__ARTIFACT_CHAT_KIND = """
ALTER TABLE artifacts RENAME TO artifacts_old;

CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
  case_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('report','viz','evidence','strategy','points','file','chat')),
  title TEXT NOT NULL,
  body_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

INSERT INTO artifacts(id, task_id, case_id, kind, title, body_json, created_at)
  SELECT id, task_id, case_id, kind, title, body_json, created_at FROM artifacts_old;

DROP TABLE artifacts_old;

CREATE INDEX idx_artifacts_case ON artifacts(case_id, created_at DESC);
"""

V30__ARTIFACT_VERSION_GROUPS = """
-- 产物版本组（交付物迭代改写）：同组产物即同一交付物的历史版本。
-- 存量产物各自成组（group_id = 自身 id，version_no = 1）；此后同组追加
-- version_no 递增的行，组内最新版本即当前稿；回退=复制旧版本追加为新当前
-- 版本（与 docgen.restore_version 同语义，历史不删）。
ALTER TABLE artifacts RENAME TO artifacts_old;

CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
  case_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('report','viz','evidence','strategy','points','file','chat')),
  title TEXT NOT NULL,
  body_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  group_id TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1
);

INSERT INTO artifacts(id, task_id, case_id, kind, title, body_json, created_at, group_id, version_no)
  SELECT id, task_id, case_id, kind, title, body_json, created_at, id, 1 FROM artifacts_old;

DROP TABLE artifacts_old;

CREATE INDEX idx_artifacts_case ON artifacts(case_id, created_at DESC);
CREATE INDEX idx_artifacts_group ON artifacts(group_id, version_no DESC);
"""

V31__ARTIFACT_DISMISSED = """
-- 产物移除（dismiss）：用户可把调试残留等非交付物文件移出产物概览。
-- 只隐藏不删数据（气泡卡/概览不再返回）；文件本体保留在磁盘；
-- 被移除的文件名不再被孤儿补登记/收尾登记找回。
ALTER TABLE artifacts ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_artifacts_dismissed ON artifacts(case_id, dismissed);
"""

V32__APPROVAL_SCOPE = """
-- 审批授权作用域（会话隔离）：SHELL_EXEC 等"案件内允许"的长效授权从绑定
-- case_id 改为绑定会话（conversation_id）。审批卡记录 scope_id，allow_case
-- 时按它写 grant——重开新对话窗口（新会话）后授权不跨会话生效，需重新审批；
-- 无会话的任务回退 case_id（防御路径）。
ALTER TABLE approval_requests ADD COLUMN scope_id TEXT;
"""

V33__ARTIFACT_ANNOTATIONS = """
-- 产物批注：AI 生成产物（报告/图表/文件等）的批注为派生数据，产物本体不变。
-- 产物无页模型（整篇单锚），批注绑定具体产物版本 id——版本组内新增版本后，
-- 旧批注不跨版本显示（与文档批注 version 绑定语义一致）；不设外键（artifacts
-- 表在 V30 曾重建，避免历史库约束失效），随案件数据生命周期统一清理。
CREATE TABLE artifact_annotations (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    quote TEXT,
    body TEXT NOT NULL,
    color TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_artifact_annotations_artifact ON artifact_annotations(artifact_id, created_at);
CREATE INDEX idx_artifact_annotations_case ON artifact_annotations(case_id);
"""

V34__TASK_CREATION = """
ALTER TABLE cases ADD COLUMN mode_config_json TEXT NOT NULL DEFAULT '{}';
CREATE TABLE case_creation_requests (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    request_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY(workspace_id, request_id)
);
"""

V35__CONTRACT_WORKSPACE = """
CREATE TABLE contract_overviews (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE REFERENCES cases(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE contract_checklist_versions (
    id TEXT PRIMARY KEY,
    overview_id TEXT NOT NULL REFERENCES contract_overviews(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    overview_revision INTEGER NOT NULL,
    items_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','confirmed','stale')),
    confirmed_at TEXT,
    snapshot_hash TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(overview_id, revision)
);
CREATE INDEX idx_contract_checklists_overview ON contract_checklist_versions(overview_id, revision DESC);
"""

V36__CONTRACT_RUNS = """
CREATE TABLE contract_review_runs (
    id TEXT PRIMARY KEY,
    overview_id TEXT NOT NULL REFERENCES contract_overviews(id) ON DELETE CASCADE,
    snapshot_id TEXT NOT NULL REFERENCES contract_checklist_versions(id),
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    review_id TEXT REFERENCES contract_reviews(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    UNIQUE(overview_id, request_id)
);
CREATE TABLE contract_item_results (
    run_id TEXT NOT NULL REFERENCES contract_review_runs(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY(run_id, item_id)
);
"""

V37__BEST_PRACTICES = """
CREATE TABLE best_practice_templates (
    template_id TEXT PRIMARY KEY REFERENCES docgen_templates(id) ON DELETE CASCADE,
    stable_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    config_json TEXT NOT NULL,
    UNIQUE(case_id, stable_id)
);
"""

V38__WORD_WORK_COPIES = """
CREATE TABLE word_work_copies (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
    source_version_id TEXT REFERENCES document_versions(id) ON DELETE CASCADE,
    draft_id TEXT REFERENCES docgen_drafts(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE word_work_copy_versions (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL REFERENCES word_work_copies(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    model_json TEXT NOT NULL,
    blob_uri TEXT NOT NULL,
    kind TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(target_id, revision),
    UNIQUE(target_id, request_id)
);
CREATE INDEX idx_word_work_copies_case ON word_work_copies(case_id);
"""

V39__SUBJECT_REPORTS = """
CREATE TABLE contract_subject_reports (
    id TEXT PRIMARY KEY,
    overview_id TEXT NOT NULL REFERENCES contract_overviews(id) ON DELETE CASCADE,
    overview_revision INTEGER NOT NULL,
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_subject_reports_overview ON contract_subject_reports(overview_id, created_at DESC);
"""

V40__WORD_DRAFT_LINKS = """
ALTER TABLE word_work_copies ADD COLUMN draft_base_updated_at TEXT;
ALTER TABLE docgen_draft_versions ADD COLUMN word_model_json TEXT;
ALTER TABLE docgen_draft_versions ADD COLUMN word_blob_uri TEXT;
"""

V41__RISK_APPLICATIONS = """
CREATE TABLE word_risk_applications (
    request_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES contract_review_runs(id) ON DELETE CASCADE,
    risk_id TEXT NOT NULL,
    target_id TEXT NOT NULL REFERENCES word_work_copies(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, risk_id, target_id)
);
CREATE INDEX idx_word_risk_applications_run ON word_risk_applications(run_id);
"""

V42__MATERIAL_COPIES = """
CREATE TABLE material_copy_sources (
    document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    source_case_id TEXT NOT NULL,
    source_document_id TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE material_copy_requests (
    request_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    target_case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE
);
"""

V43__SUBJECT_REPORTS_BATCH = """
-- M4：主体审查改为一次任务批量审查多个主体；同一任务的每个主体各一条报告，
-- task_id 不再唯一（一批报告共享同一任务与同一次外发授权 digest）。
ALTER TABLE contract_subject_reports RENAME TO contract_subject_reports_old;

CREATE TABLE contract_subject_reports (
    id TEXT PRIMARY KEY,
    overview_id TEXT NOT NULL REFERENCES contract_overviews(id) ON DELETE CASCADE,
    overview_revision INTEGER NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

INSERT INTO contract_subject_reports(id, overview_id, overview_revision, task_id, report_json, created_at)
  SELECT id, overview_id, overview_revision, task_id, report_json, created_at FROM contract_subject_reports_old;

DROP TABLE contract_subject_reports_old;

CREATE INDEX idx_subject_reports_overview ON contract_subject_reports(overview_id, created_at DESC);
CREATE INDEX idx_subject_reports_task ON contract_subject_reports(task_id);
"""

V44__CASE_LINK = """
-- 文书生成任务可关联同工作空间的既有案件：只读共享其材料（文档/解析页）
-- 与案件记忆。关联关系仅记录来源，不复制材料，不改变原件归属。
-- 来源案件删除后关联自动置空（ON DELETE SET NULL），共享范围随之消失。
ALTER TABLE cases ADD COLUMN linked_case_id TEXT REFERENCES cases(id) ON DELETE SET NULL;
CREATE INDEX idx_cases_linked ON cases(linked_case_id);
"""

V45__DROP_SUBJECT_REPORTS = """
-- 主体审查能力已下线，删除其专用报告表与索引。
DROP INDEX IF EXISTS idx_subject_reports_task;
DROP INDEX IF EXISTS idx_subject_reports_overview;
DROP TABLE IF EXISTS contract_subject_reports;
"""

V46__DROP_BILLING = """
-- 应用改为免费向用户提供（用户自行配置模型），移除会员/积分/套餐/订单计费体系。
-- 旧版本迁移保持原样（checksum 校验），这里只做数据层清理。
DROP INDEX IF EXISTS idx_batches_order;
DROP INDEX IF EXISTS idx_batches_fefo;
DROP INDEX IF EXISTS idx_orders_status;
DROP TABLE IF EXISTS credit_transactions;
DROP TABLE IF EXISTS billing_orders;
DROP TABLE IF EXISTS credit_batches;
DROP TABLE IF EXISTS model_credit_rates;
-- model_calls 原是计费结算账本（唯一写入方为已移除的 billing.settle）；
-- 用量与调用次数现由 task_budget（V16）计量并随任务事件下发，该表已无写入方。
DROP TABLE IF EXISTS model_calls;
"""

V47__HUMAN_QUESTIONS = """
-- 文书生成必要用户决策：问题集持久化。
-- 任务在需要用户决策时暂停（tasks.lifecycle=WAITING_HUMAN），问题集落库后
-- 前端刷新/应用重启仍可恢复待答卡片；回答后写入答案并标记 answered，
-- 任务从同一检查点续跑（不新建草稿、不重复用户消息与工具副作用）。
CREATE TABLE human_question_sets (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  conversation_id TEXT,
  draft_id TEXT,
  title TEXT NOT NULL,
  context_digest TEXT,
  questions_json TEXT NOT NULL DEFAULT '[]',
  answers_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','answered','cancelled')),
  checkpoint_id TEXT,
  created_at TEXT NOT NULL,
  answered_at TEXT
);

CREATE INDEX idx_human_questions_task ON human_question_sets(task_id, status, created_at DESC);
"""

V48__DOCX_TEMPLATES = """
-- DOCX 自定义模板（升级方案 §6.1/§6.4 / §10 阶段三）：
-- 用户上传 .docx 范本时以原件为格式基底，模板表补充来源格式、原件存储位置、
-- 字段位置映射、解析状态与格式警告；原件只读保存，生成结果写入新文件。
ALTER TABLE docgen_templates ADD COLUMN source_format TEXT NOT NULL DEFAULT 'md';
ALTER TABLE docgen_templates ADD COLUMN source_blob_uri TEXT;
ALTER TABLE docgen_templates ADD COLUMN source_size_bytes INTEGER;
ALTER TABLE docgen_templates ADD COLUMN field_mapping_json TEXT;
ALTER TABLE docgen_templates ADD COLUMN format_warnings_json TEXT;
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, V1__WORKSPACE_CASES_DOCUMENTS),
    (2, V2__AUDIT_LOGS),
    (3, V3__FTS_INDEX),
    (4, V4__TEXT_BLOCKS),
    (5, V5__AGENT_TASKS),
    (6, V6__APPROVAL_BILLING),
    (7, V7__CLAIM_LEDGER),
    (8, V8__WORKFLOW_PRESET),
    (9, V9__ARTIFACTS_CONFLICT),
    (10, V10__REDACTION_MCP),
    (11, V11__CONTRACT_REVIEW),
    (12, V12__DOC_GEN),
    (13, V13__DOCUMENT_UNLOCK),
    (14, V14__BILLING_ORDERS),
    (15, V15__MEMORY),
    (16, V16__TASK_BUDGET),
    (17, V17__OCR_CORRECTION),
    (18, V18__EDITOR_SESSIONS),
    (19, V19__DOCUMENT_ANNOTATIONS),
    (20, V20__TEMPLATE_VERSIONS),
    (21, V21__SKILLS),
    (22, V22__MCP_AUTH_AND_HISTORY),
    (23, V23__DOCUMENT_REDACTED_COPY),
    (24, V24__DOCUMENT_REDACT_INPLACE),
    (25, V25__ANNOTATION_COLOR),
    (26, V26__CONVERSATIONS),
    (27, V27__ARTIFACT_FILE_KIND),
    (28, V28__ARTIFACT_CHAT_KIND),
    (30, V30__ARTIFACT_VERSION_GROUPS),
    (31, V31__ARTIFACT_DISMISSED),
    (32, V32__APPROVAL_SCOPE),
    (33, V33__ARTIFACT_ANNOTATIONS),
    (34, V34__TASK_CREATION),
    (35, V35__CONTRACT_WORKSPACE),
    (36, V36__CONTRACT_RUNS),
    (37, V37__BEST_PRACTICES),
    (38, V38__WORD_WORK_COPIES),
    (39, V39__SUBJECT_REPORTS),
    (40, V40__WORD_DRAFT_LINKS),
    (41, V41__RISK_APPLICATIONS),
    (42, V42__MATERIAL_COPIES),
    (43, V43__SUBJECT_REPORTS_BATCH),
    (44, V44__CASE_LINK),
    (45, V45__DROP_SUBJECT_REPORTS),
    (46, V46__DROP_BILLING),
    (47, V47__HUMAN_QUESTIONS),
    (48, V48__DOCX_TEMPLATES),
]


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def apply_migrations(db: "Database", migrations: list[tuple[int, str]] | None = None) -> list[int]:
    """应用未执行的迁移，返回本次应用的版本列表。"""
    applied_now: list[int] = []
    with db.transaction() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version INTEGER PRIMARY KEY,
              checksum TEXT NOT NULL,
              applied_at TEXT NOT NULL
            )
            """
        )
        rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        applied = {row["version"]: row["checksum"] for row in rows}

    for version, sql in (MIGRATIONS if migrations is None else migrations):
        if version in applied:
            if applied[version] != _checksum(sql):
                raise RuntimeError(f"migration {version} checksum mismatch: database was modified or from a different build")
            continue
        # 单事务迁移：任一语句失败整体回滚（DB-005 迁移失败旧库仍可恢复）
        with db.transaction() as conn:
            for stmt in sql.split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
                (version, _checksum(sql), datetime.now(timezone.utc).isoformat()),
            )
        applied_now.append(version)
    return applied_now


# ---------------------------------------------------------------------------
# 设备级配置库（profile）迁移：与工作空间库分离
#
# 模型服务商 / 模型目录 / API Key、MCP 连接器（含认证头）、已安装技能属于
# 「本机设备」配置，不属于某一个案件工作空间。它们单独存放在固定位置的
# profile.db（同样是 SQLCipher 加密库，DEK 由系统密钥环包装），切换/新建
# 工作空间时不会丢失；工作空间库只保留案件、文档、会话、产物等任务资料。
# ---------------------------------------------------------------------------

PROFILE_V1 = """
-- 设备级键值设置：model.provider / model.customProviders / model.providerOverrides
-- / model.<providerId>ApiKey（密文）等；敏感值经密钥环包装后落盘。
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- MCP 连接器（含认证头，整库加密；输出只给 hasAuth，不返回密钥值）。
CREATE TABLE mcp_servers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  allowed_tools_json TEXT NOT NULL DEFAULT '[]',
  headers_json TEXT NOT NULL DEFAULT '{}',
  last_status TEXT NOT NULL DEFAULT 'unknown' CHECK (last_status IN ('unknown','available','offline','error')),
  last_error TEXT,
  last_checked_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_mcp_enabled ON mcp_servers(enabled);

-- 已安装技能（提示词包，非可执行代码）。
CREATE TABLE skills (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL,
  prompt_md TEXT NOT NULL,
  allowed_tools_json TEXT NOT NULL DEFAULT '[]',
  content_sha256 TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 设备级审计：设置变更、连接器增删改、技能安装/移除同样需要追加式留痕
-- （只记元数据，不落密钥、认证头值与提示词正文）。
CREATE TABLE audit_logs (
  seq INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'local_owner',
  action TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  case_id TEXT,
  result TEXT NOT NULL DEFAULT 'ok' CHECK (result IN ('ok','denied','failed')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  prev_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL
);
CREATE INDEX idx_audit_action ON audit_logs(action, seq);
"""

PROFILE_MIGRATIONS: list[tuple[int, str]] = [
    (1, PROFILE_V1),
]
