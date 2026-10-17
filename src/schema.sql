-- 问询函最小 query 库
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 文件层：可追溯、可去重、可增量
CREATE TABLE IF NOT EXISTS letter (
  letter_id   TEXT PRIMARY KEY,          -- 如 皇裕精密-r1
  company     TEXT NOT NULL,
  board       TEXT NOT NULL,             -- 北交所/科创板/创业板/深主板/新三板
  round       INTEGER NOT NULL,          -- 1/2/3
  source_url  TEXT,                      -- 交易所官网原文链接
  file_path   TEXT,
  file_hash   TEXT UNIQUE,               -- 幂等去重
  pages       INTEGER,
  chapters    TEXT,                      -- 出现的章节目录，逗号分隔
  imported_at TEXT
);

-- 问题节层：章节-问题节-最小 query 的层级在这里体现
CREATE TABLE IF NOT EXISTS question (
  question_id TEXT PRIMARY KEY,          -- 如 皇裕精密-r1-4
  letter_id   TEXT NOT NULL REFERENCES letter(letter_id),
  chapter     TEXT,
  question_no INTEGER,
  title       TEXT,
  page_start  INTEGER,
  page_end    INTEGER
);

-- 最小 query 层
CREATE TABLE IF NOT EXISTS query (
  q_id            TEXT PRIMARY KEY,      -- 如 皇裕精密-r1-0042
  letter_id       TEXT NOT NULL REFERENCES letter(letter_id),
  question_id     TEXT NOT NULL REFERENCES question(question_id),
  company         TEXT NOT NULL,
  board           TEXT NOT NULL,
  round           INTEGER NOT NULL,
  chapter         TEXT,                  -- 原文，如"二、财务会计信息与管理层分析"
  chapter_std     TEXT,                  -- 去掉"一、二、"编号，跨公司对齐用
  question_no     INTEGER,
  question        TEXT,
  sub_question    TEXT,                  -- （N）小问标题
  asker           TEXT,                  -- 请发行人 / 请保荐机构、发行人律师
  item_no         TEXT,                  -- ① / （2）
  seq_in_letter   INTEGER,
  seq_in_question INTEGER,
  page_start      INTEGER,
  page_end        INTEGER,
  raw_text        TEXT NOT NULL,
  norm_strict     TEXT NOT NULL,         -- 保留数字，精确召回 + 高亮定位
  norm_loose      TEXT NOT NULL,         -- 去数字，宽召回
  norm_core       TEXT NOT NULL,         -- 再去掉通用法规/准则引用，**打分用**
  char_len        INTEGER,
  is_boilerplate  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_q_align ON query(chapter_std, question_no);
CREATE INDEX IF NOT EXISTS idx_q_src   ON query(board, round);
CREATE INDEX IF NOT EXISTS idx_q_norm  ON query(norm_strict);
CREATE INDEX IF NOT EXISTS idx_q_letter ON query(letter_id);

-- 中文子串检索：FTS5 trigram，零部署，支持任意子串与错字
CREATE VIRTUAL TABLE IF NOT EXISTS query_fts USING fts5(
  q_id UNINDEXED,
  norm_loose,
  tokenize = 'trigram'
);

-- n-gram 文档频率：用来算 IDF，让"企业会计准则""是否符合"这类
-- 满库都是的通用词自动降权，不必手工穷举。
-- n 由 src.search.GRAM_N 决定（当前 4），改了要重新建库。
CREATE TABLE IF NOT EXISTS gram_df (
  gram TEXT PRIMARY KEY,
  df   INTEGER NOT NULL
);
