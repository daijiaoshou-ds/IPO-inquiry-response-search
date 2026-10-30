// 问询检索 · Rust 检索内核
//
// 两个引擎：
//   fts   —— 阶段①：SQLite FTS5 粗筛 + 自算打分。结果与 Python 100% 一致，
//            速度 1.7x。用来做对照组。
//   index —— 阶段②：自建 4-gram 倒排索引（mmap），绕开 FTS5/bm25。
//
// 边界（两个引擎都一样）：**不做中文归一化**。NFKC、全角、标点表、法规正则
// 全留在 Python（normalize.py），输入里 core/loose/strict/clauses 都是算好的。
//
// 用法：
//   search-core --build-index eval/search.index --db data/queries.db
//   search-core --engine index --index eval/search.index --golden eval/golden.json --out eval/rust_out.json
//   search-core --engine fts   --golden eval/golden.json --out eval/rust_out.json --limit 20

mod common;
mod index;

use common::*;
use rusqlite::Connection;
use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};
use std::time::Instant;

const MAX_FTS_TERMS: usize = 150;

// ------------------------------------------------------------------ 输入/输出

#[derive(Deserialize)]
struct Golden {
    meta: Meta,
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Meta {
    top: usize,
}

#[derive(Deserialize)]
struct Case {
    // 黄金集里 id 是混合类型：单条用 q_id 字符串，整段粘贴用 -1-i 这类负数
    id: serde_json::Value,
    #[serde(default)]
    tag: Option<String>,
    #[serde(default)]
    board: Option<String>,
    parts: Vec<Part>,
}

#[derive(Serialize)]
struct OutDoc {
    meta: OutMeta,
    cases: Vec<OutCase>,
}

#[derive(Serialize)]
struct OutMeta {
    engine: String,
    cases: usize,
    ms: f64,
}

#[derive(Serialize)]
struct OutCase {
    id: serde_json::Value,
    tag: Option<String>,
    ms: f64,
    parts: Vec<OutPart>,
    flat: Vec<FlatHit>,
}

#[derive(Serialize)]
struct OutPart {
    text: String,
    hits: Vec<Hit>,
}

#[derive(Serialize)]
struct FlatHit {
    q_id: String,
    score: f64,
}

// ------------------------------------------------------------------ IDF（仅 fts 引擎需要）

struct Idf {
    table: FxHashMap<u128, f64>,
    default: f64,
}

impl Idf {
    fn load(conn: &Connection) -> rusqlite::Result<Self> {
        let n: f64 = conn.query_row(
            "SELECT COUNT(*) FROM query WHERE norm_core <> ''",
            [],
            |r| r.get(0),
        )?;
        let mut table: FxHashMap<u128, f64> = FxHashMap::default();
        table.reserve(3_100_000);
        let mut stmt = conn.prepare("SELECT gram, df FROM gram_df")?;
        let mut rows = stmt.query([])?;
        while let Some(r) = rows.next()? {
            let g: String = r.get(0)?;
            let df: f64 = r.get(1)?;
            let chars: Vec<char> = g.chars().collect();
            table.insert(pack_chars(&chars), (1.0 + n / (1.0 + df)).ln());
        }
        Ok(Idf { table, default: (1.0 + n).ln() })
    }

    #[inline]
    fn w(&self, g: u128) -> f64 {
        *self.table.get(&g).unwrap_or(&self.default)
    }
}

// ------------------------------------------------------------------ 粗筛（FTS 引擎）

fn fts_terms(conn: &Connection, q_loose: &str) -> rusqlite::Result<Option<String>> {
    let chars: Vec<char> = q_loose.chars().collect();
    if chars.len() < GRAM_N {
        return Ok(None);
    }
    let mut seen: FxHashMap<u128, ()> = FxHashMap::default();
    let mut grams: Vec<(u128, String)> = Vec::new();
    for i in 0..=chars.len() - GRAM_N {
        let key = pack_chars(&chars[i..i + GRAM_N]);
        if seen.insert(key, ()).is_none() {
            grams.push((key, chars[i..i + GRAM_N].iter().collect()));
        }
    }
    if grams.is_empty() {
        return Ok(None);
    }
    if grams.len() > MAX_FTS_TERMS {
        let mut df: FxHashMap<String, i64> = FxHashMap::default();
        let texts: Vec<String> = grams.iter().map(|(_, s)| s.clone()).collect();
        for chunk in texts.chunks(400) {
            let ph = vec!["?"; chunk.len()].join(",");
            let sql = format!("SELECT gram, df FROM gram_df WHERE gram IN ({})", ph);
            let mut stmt = conn.prepare(&sql)?;
            let mut rows = stmt.query(rusqlite::params_from_iter(chunk.iter()))?;
            while let Some(r) = rows.next()? {
                let g: String = r.get(0)?;
                let d: i64 = r.get(1)?;
                df.insert(g, d);
            }
        }
        grams.sort_by(|a, b| {
            let da = df.get(&a.1).copied().unwrap_or(0);
            let db = df.get(&b.1).copied().unwrap_or(0);
            da.cmp(&db).then_with(|| a.1.cmp(&b.1))
        });
        grams.truncate(MAX_FTS_TERMS);
    }
    let s = grams
        .iter()
        .map(|(_, g)| format!("\"{}\"", g.replace('"', "\"\"")))
        .collect::<Vec<_>>()
        .join(" OR ");
    Ok(Some(s))
}

// ------------------------------------------------------------------ FTS 引擎检索

struct Scored {
    sim: f64,
    exact: bool,
    q_id: String,
    target: String,
}

fn sim_of(
    side: &FxHashMap<u128, f64>,
    na: f64,
    grams: &[(u128, u32)],
    nb: f64,
    idf: &Idf,
) -> f64 {
    if na == 0.0 || nb == 0.0 {
        return 0.0;
    }
    let mut num = 0.0f64;
    for &(k, cnt) in grams {
        if let Some(&wa) = side.get(&k) {
            num += wa * cnt as f64 * idf.w(k);
        }
    }
    sim_from(num, na, nb)
}

fn search_fts(
    conn: &Connection,
    part: &Part,
    board: Option<&str>,
    top: usize,
    idf: &Idf,
) -> rusqlite::Result<Vec<Hit>> {
    let terms = match fts_terms(conn, &part.loose)? {
        Some(t) => t,
        None => return Ok(Vec::new()),
    };
    let sql = if board.is_some() {
        "SELECT q_id, norm_core, norm_loose, norm_strict FROM query \
         WHERE board = ?1 AND is_boilerplate = 0 AND q_id IN \
         (SELECT q_id FROM query_fts WHERE query_fts MATCH ?2 \
          ORDER BY bm25(query_fts) LIMIT 6000)"
    } else {
        "SELECT q_id, norm_core, norm_loose, norm_strict FROM query \
         WHERE is_boilerplate = 0 AND q_id IN \
         (SELECT q_id FROM query_fts WHERE query_fts MATCH ?1 \
          ORDER BY bm25(query_fts) LIMIT 6000)"
    };
    let mut stmt = conn.prepare_cached(sql)?;
    let mut rows = match board {
        Some(b) => stmt.query(rusqlite::params![b, terms])?,
        None => stmt.query(rusqlite::params![terms])?,
    };
    let mut cands: Vec<(String, String, String)> = Vec::new();
    while let Some(r) = rows.next()? {
        let q_id: String = r.get(0)?;
        let core: String = r.get(1)?;
        let loose: String = r.get(2)?;
        let strict: String = r.get(3)?;
        cands.push((q_id, if core.is_empty() { loose } else { core }, strict));
    }
    drop(rows);

    let q_counts = gram_counts(&part.core);
    let q_side: FxHashMap<u128, f64> = q_counts
        .iter()
        .map(|&(k, c)| (k, c as f64 * idf.w(k)))
        .collect();
    let q_na: f64 = q_side.values().map(|v| v * v).sum();
    let q_chars: Vec<char> = part.core.chars().collect();

    let clauses: Vec<(FxHashMap<u128, f64>, f64)> = part
        .clauses
        .iter()
        .map(|c| {
            let m: FxHashMap<u128, f64> = gram_counts(c)
                .iter()
                .map(|&(k, cnt)| (k, cnt as f64 * idf.w(k)))
                .collect();
            let na: f64 = m.values().map(|v| v * v).sum();
            (m, na)
        })
        .collect();
    let use_clauses = clauses.len() > 1;
    let defer = use_clauses && cands.len() * clauses.len() > CLAUSE_WORK_BUDGET;

    let mut scored: Vec<Scored> = Vec::with_capacity(cands.len());
    for (q_id, target, strict) in cands.iter() {
        let cc = gram_counts(target);
        let mut nb = 0.0f64;
        for &(k, cnt) in &cc {
            let vb = cnt as f64 * idf.w(k);
            nb += vb * vb;
        }
        let mut sim = sim_of(&q_side, q_na, &cc, nb, idf);
        if use_clauses && !defer {
            let mut best = 0.0f64;
            for (side, na) in clauses.iter() {
                let s = sim_of(side, *na, &cc, nb, idf) * CLAUSE_DISCOUNT;
                if s > best {
                    best = s;
                }
            }
            if best > sim {
                sim = best;
            }
        }
        let exact = !part.strict.is_empty() && strict.contains(&part.strict);
        scored.push(Scored { sim, exact, q_id: q_id.clone(), target: target.clone() });
    }

    // 同分按 q_id 兜底，和 Python 完全一致（否则并列顺序会不同）
    let by_sim = |a: &Scored, b: &Scored| {
        b.sim
            .partial_cmp(&a.sim)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| b.exact.cmp(&a.exact))
            .then_with(|| a.q_id.cmp(&b.q_id))
    };
    scored.sort_by(by_sim);

    if defer {
        let clause_pool = (top * 4).max(CLAUSE_WORK_BUDGET / clauses.len());
        for s in scored.iter_mut().take(clause_pool) {
            let cc = gram_counts(&s.target);
            let mut nb = 0.0f64;
            for &(k, cnt) in &cc {
                let vb = cnt as f64 * idf.w(k);
                nb += vb * vb;
            }
            let mut best = 0.0f64;
            for (side, na) in clauses.iter() {
                let v = sim_of(side, *na, &cc, nb, idf) * CLAUSE_DISCOUNT;
                if v > best {
                    best = v;
                }
            }
            if best > s.sim {
                s.sim = best;
            }
        }
        scored.sort_by(by_sim);
    }

    let q_len = q_chars.len().max(1);
    let keep = (LCS_CHAR_BUDGET / (q_len * q_len)).clamp(1, TOP_FOR_LCS).max(40);
    let mut hits: Vec<Hit> = Vec::with_capacity(keep.min(scored.len()));
    for s in scored.iter().take(keep) {
        let tc: Vec<char> = s.target.chars().collect();
        let ln = lcs_len(&q_chars, &tc);
        let score = final_score(s.sim, ln, q_chars.len(), tc.len());
        hits.push(Hit {
            q_id: s.q_id.clone(),
            score: round_to(score, 4),
            sim: round_to(s.sim, 3),
            lcs: ln,
            exact: s.exact,
        });
    }
    hits.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| b.exact.cmp(&a.exact))
            .then_with(|| a.q_id.cmp(&b.q_id))
    });
    hits.truncate(top);
    Ok(hits)
}

// ------------------------------------------------------------------ sidecar 模式
//
// 由 Python 侧拉起一个常驻进程，stdin 收请求、stdout 回结果，一行一个 JSON。
// 好处：索引只 mmap 一次；Rust 崩了不会带崩网页服务，Python 能立刻回退。

#[derive(Deserialize)]
struct Req {
    #[serde(default)]
    id: i64,
    #[serde(default)]
    board: Option<String>,
    #[serde(default)]
    round: Option<i32>,
    top: usize,
    parts: Vec<Part>,
}

#[derive(Serialize)]
struct RespPart {
    hits: Vec<Hit>,
}

#[derive(Serialize)]
struct Resp {
    id: i64,
    ms: f64,
    parts: Vec<RespPart>,
}

/// 打开索引并校验与库是否一致（库变了就拒绝启动，绝不静默返回旧结果）
fn open_index_verified(
    conn: &Connection,
    path: &str,
) -> Result<index::Index, Box<dyn std::error::Error>> {
    let idx = index::Index::open(path)?;
    let db_docs: i64 = conn.query_row("SELECT COUNT(*) FROM query", [], |r| r.get(0))?;
    let db_chars: i64 =
        conn.query_row("SELECT COALESCE(SUM(char_len),0) FROM query", [], |r| r.get(0))?;
    // 禁用条数单独对账：标了禁用却不重建索引时，篇数和字数都不变，
    // 光看上面两个条件校验会通过，于是禁用静默失效（条目照常被检索出来）。
    let db_banned: i64 =
        conn.query_row("SELECT COUNT(*) FROM query WHERE disabled = 1", [], |r| r.get(0))?;
    if idx.doc_count() as i64 != db_docs
        || idx.char_sum() != db_chars as u64
        || idx.n_disabled() as i64 != db_banned
    {
        return Err(format!(
            "索引与库不一致：索引 {} 篇 / 字数 {} / 禁用 {}，库里 {} 篇 / 字数 {} / 禁用 {}。\
             请重新 --build-index",
            idx.doc_count(),
            idx.char_sum(),
            idx.n_disabled(),
            db_docs,
            db_chars,
            db_banned
        )
        .into());
    }
    Ok(idx)
}

fn serve_loop(
    idx: &index::Index,
    searcher: &mut index::Searcher,
    conn: &Connection,
) -> Result<(), Box<dyn std::error::Error>> {
    use std::io::{BufRead, Write};
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut out = std::io::BufWriter::new(stdout.lock());
    eprintln!(
        "ready: {} 篇 / {} gram",
        idx.doc_count(),
        idx.gram_count()
    );
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let t0 = Instant::now();
        let payload = match serde_json::from_str::<Req>(&line) {
            Ok(req) => {
                let mut parts = Vec::with_capacity(req.parts.len());
                let mut err: Option<String> = None;
                for p in &req.parts {
                    match searcher.search(idx, conn, p, req.board.as_deref(), req.round, req.top) {
                        Ok(hits) => parts.push(RespPart { hits }),
                        Err(e) => {
                            err = Some(e.to_string());
                            break;
                        }
                    }
                }
                match err {
                    Some(e) => serde_json::json!({"id": req.id, "error": e}).to_string(),
                    None => serde_json::to_string(&Resp {
                        id: req.id,
                        ms: round_to(t0.elapsed().as_secs_f64() * 1000.0, 1),
                        parts,
                    })?,
                }
            }
            Err(e) => serde_json::json!({"id": -1, "error": format!("bad request: {}", e)})
                .to_string(),
        };
        out.write_all(payload.as_bytes())?;
        out.write_all(b"\n")?;
        out.flush()?;
    }
    Ok(())
}

// ------------------------------------------------------------------ main

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    let mut golden = "eval/golden.json".to_string();
    let mut db = "data/queries.db".to_string();
    let mut out = "eval/rust_out.json".to_string();
    let mut index_path = "eval/search.index".to_string();
    let mut build_index = String::new();
    let mut engine = "fts".to_string();
    let mut limit: usize = 0;
    let mut serve = false;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--golden" => golden = args.get(i + 1).cloned().unwrap_or_default(),
            "--db" => db = args.get(i + 1).cloned().unwrap_or_default(),
            "--out" => out = args.get(i + 1).cloned().unwrap_or_default(),
            "--index" => index_path = args.get(i + 1).cloned().unwrap_or_default(),
            "--build-index" => build_index = args.get(i + 1).cloned().unwrap_or_default(),
            "--engine" => engine = args.get(i + 1).cloned().unwrap_or_default(),
            "--limit" => limit = args.get(i + 1).and_then(|s| s.parse().ok()).unwrap_or(0),
            "--serve" => {
                serve = true;
                i += 1;
                continue;
            }
            "-h" | "--help" => {
                println!("用法:");
                println!("  search-core --build-index <index.bin> --db data/queries.db");
                println!("  search-core --engine fts|index [--index <index.bin>] --golden <golden.json> --out <out.json> [--limit N]");
                return Ok(());
            }
            other => eprintln!("忽略未知参数 {}", other),
        }
        i += 2;
    }

    let conn = Connection::open(&db)?;

    // ---- 建索引 ----
    if !build_index.is_empty() {
        return index::build(&conn, &build_index);
    }

    if serve {
        let idx = open_index_verified(&conn, &index_path)?;
        let mut searcher = index::Searcher::new(&idx);
        return serve_loop(&idx, &mut searcher, &conn);
    }

    let doc: Golden = serde_json::from_str(&std::fs::read_to_string(&golden)?)?;
    let top = doc.meta.top.max(1);

    // ---- 打开引擎 ----
    let use_index = engine == "index";
    let mut idx_opt: Option<index::Index> = None;
    let mut searcher: Option<index::Searcher> = None;
    let mut idf_opt: Option<Idf> = None;

    if use_index {
        let t0 = Instant::now();
        let idx = open_index_verified(&conn, &index_path)?;
        println!(
            "索引 {} 篇 / {} gram，打开 {:.0} ms",
            idx.doc_count(),
            idx.gram_count(),
            t0.elapsed().as_secs_f64() * 1000.0
        );
        searcher = Some(index::Searcher::new(&idx));
        idx_opt = Some(idx);
    } else {
        let t0 = Instant::now();
        let idf = Idf::load(&conn)?;
        println!(
            "IDF 加载 {:.0} ms（{} 条 gram）",
            t0.elapsed().as_secs_f64() * 1000.0,
            idf.table.len()
        );
        idf_opt = Some(idf);
    }

    let n = if limit > 0 { limit.min(doc.cases.len()) } else { doc.cases.len() };
    let mut cases: Vec<OutCase> = Vec::with_capacity(n);
    let t_all = Instant::now();
    for (k, c) in doc.cases.iter().take(n).enumerate() {
        let t0 = Instant::now();
        let mut parts: Vec<OutPart> = Vec::with_capacity(c.parts.len());
        let mut merged: FxHashMap<String, f64> = FxHashMap::default();
        for p in &c.parts {
            let hits = if use_index {
                searcher.as_mut().unwrap().search(
                    idx_opt.as_ref().unwrap(),
                    &conn,
                    p,
                    c.board.as_deref(),
                    None,
                    top,
                )?
            } else {
                search_fts(&conn, p, c.board.as_deref(), top, idf_opt.as_ref().unwrap())?
            };
            for h in &hits {
                let e = merged.entry(h.q_id.clone()).or_insert(f64::MIN);
                if h.score > *e {
                    *e = h.score;
                }
            }
            parts.push(OutPart { text: p.text.clone(), hits });
        }
        let mut flat: Vec<FlatHit> = merged
            .into_iter()
            .map(|(q_id, score)| FlatHit { q_id, score })
            .collect();
        flat.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| a.q_id.cmp(&b.q_id))
        });
        flat.truncate(top);

        cases.push(OutCase {
            id: c.id.clone(),
            tag: c.tag.clone(),
            ms: round_to(t0.elapsed().as_secs_f64() * 1000.0, 1),
            parts,
            flat,
        });
        if (k + 1) % 20 == 0 || k + 1 == n {
            println!("  {}/{}  {:.1}s", k + 1, n, t_all.elapsed().as_secs_f64());
        }
    }

    let doc_out = OutDoc {
        meta: OutMeta {
            engine: engine.clone(),
            cases: cases.len(),
            ms: round_to(t_all.elapsed().as_secs_f64() * 1000.0, 1),
        },
        cases,
    };
    std::fs::write(&out, serde_json::to_string(&doc_out)?)?;

    let mut ms: Vec<f64> = doc_out.cases.iter().map(|c| c.ms).collect();
    ms.sort_by(|a, b| a.partial_cmp(b).unwrap());
    println!();
    println!("已写入 {}（引擎 {}）", out, engine);
    println!(
        "  单条耗时 中位 {:.0} ms / p95 {:.0} ms / 最大 {:.0} ms / 合计 {:.1}s",
        ms[ms.len() / 2],
        ms[(ms.len() as f64 * 0.95) as usize],
        ms[ms.len() - 1],
        doc_out.meta.ms / 1000.0
    );
    Ok(())
}
