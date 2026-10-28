// 自建 4-gram 倒排索引：建索引、mmap 打开、按索引检索。
//
// 为什么不用 SQLite 的 FTS5 + bm25（见 eval/ 与文档里的实测）：
//   中位查询命中 7.8 万条，bm25 要排序取 6000 条，SQLite 里花 137~288 ms，
//   而"把命中全部枚举出来"只要 41 ms。慢的不是找，是给全命中排序。
//   自带索引把分数变成"累加"而不是"比较"：
//       对 query 的每个 4-gram，把倒排表里每个 (doc, tf) 累进 acc[doc]
//   acc 就是分子，配预存的 |B| 和 query 侧 |A| 直接得出 sim，全程不碰文档文本。
//
// 文件布局（小端）：
//   [0]   header 128 B
//   [1]   板块名表：u32 个数 + (u8 长度 + utf8)
//   [2]   nb        doc_count × f64   文档侧 |B|²（建索引时按 Python 同一公式算好）
//   [3]   board     doc_count × u8
//   [4]   flags     doc_count × u8    bit0 = 套话
//   [5]   round     doc_count × u8
//   [6]   qid_off   doc_count × u32   指向 [8] 的偏移
//   [7]   qid_len   doc_count × u16
//   [8]   qid_blob  q_id 拼接
//   [9]   gram 表   gram_count × 32   (key u128, off u32, len u32, w f64)，按 key 升序
//   [10]  postings  每个 gram 一段：varint(文档号增量) + varint(tf)

use crate::common::*;
use memmap2::Mmap;
use rusqlite::Connection;
use rustc_hash::FxHashMap;
use std::collections::HashMap;
use std::fs::File;
use std::io::{BufWriter, Seek, SeekFrom, Write};

const MAGIC: &[u8; 8] = b"QBIDX001";

fn set32(b: &mut [u8], at: usize, v: u32) {
    b[at..at + 4].copy_from_slice(&v.to_le_bytes());
}
fn set64(b: &mut [u8], at: usize, v: u64) {
    b[at..at + 8].copy_from_slice(&v.to_le_bytes());
}
const HEADER_LEN: usize = 128;
const GT_ENTRY: usize = 32;

// ------------------------------------------------------------------ varint

#[inline]
fn put_uvar(out: &mut Vec<u8>, mut v: u64) {
    while v >= 0x80 {
        out.push((v as u8) | 0x80);
        v >>= 7;
    }
    out.push(v as u8);
}

#[inline]
fn get_uvar(b: &[u8], p: &mut usize) -> u64 {
    let mut v = 0u64;
    let mut s = 0;
    loop {
        let x = b[*p];
        *p += 1;
        v |= ((x & 0x7f) as u64) << s;
        if x < 0x80 {
            break;
        }
        s += 7;
    }
    v
}

// ------------------------------------------------------------------ 建索引

struct Doc {
    q_id: String,
    target: String,
    has_core: bool,
    board: u8,
    round: u8,
    boiler: bool,
}

pub fn build(conn: &Connection, out_path: &str) -> Result<(), Box<dyn std::error::Error>> {
    let t0 = std::time::Instant::now();
    let mut board_ids: Vec<String> = Vec::new();
    let mut board_map: HashMap<String, u8> = HashMap::new();

    let mut stmt = conn.prepare(
        "SELECT q_id, norm_core, norm_loose, board, round, is_boilerplate, char_len FROM query",
    )?;
    let mut rows = stmt.query([])?;
    let mut docs: Vec<Doc> = Vec::with_capacity(300_000);
    let mut n_core: u32 = 0;
    let mut char_sum: u64 = 0;
    while let Some(r) = rows.next()? {
        let q_id: String = r.get(0)?;
        let core: String = r.get(1)?;
        let loose: String = r.get(2)?;
        let board: String = r.get(3)?;
        let round: i64 = r.get(4)?;
        let boiler: i64 = r.get(5)?;
        let clen: i64 = r.get(6)?;
        char_sum += clen.max(0) as u64;
        let has_core = !core.is_empty();
        if has_core {
            n_core += 1;
        }
        let bid = *board_map.entry(board.clone()).or_insert_with(|| {
            board_ids.push(board);
            (board_ids.len() - 1) as u8
        });
        docs.push(Doc {
            q_id,
            target: if has_core { core } else { loose },
            has_core,
            board: bid,
            round: round.clamp(0, 255) as u8,
            boiler: boiler != 0,
        });
    }
    drop(rows);
    println!(
        "读到 {} 个文档，{} 个板块，{:.1}s",
        docs.len(),
        board_ids.len(),
        t0.elapsed().as_secs_f64()
    );

    // ---- 第一遍：倒排表 ----
    let mut post: FxHashMap<u128, Vec<(u32, u16)>> = FxHashMap::default();
    post.reserve(3_200_000);
    for (i, d) in docs.iter().enumerate() {
        for (k, c) in gram_counts(&d.target) {
            post.entry(k).or_default().push((i as u32, c.min(65535) as u16));
        }
    }
    println!("倒排表 {} 个 gram，{:.1}s", post.len(), t0.elapsed().as_secs_f64());

    // ---- w(g) = ln(1 + n/(1+df))，df = 含该 gram 的文档数 ----
    let n_core_f = n_core as f64;
    let default_w = (1.0 + n_core_f).ln();
    let mut grams: Vec<u128> = post.keys().copied().collect();
    grams.sort_unstable();
    let w_of = |df: usize| (1.0 + n_core_f / (1.0 + df as f64)).ln();

    // ---- 第二遍：文档侧 |B|²（和 Python gram_norm 同一公式、同一求和顺序）----
    let mut nb: Vec<f64> = Vec::with_capacity(docs.len());
    for d in docs.iter() {
        let mut s = 0.0f64;
        for (k, c) in gram_counts(&d.target) {
            let df = post.get(&k).map(|v| v.len()).unwrap_or(0);
            let w = if df == 0 { default_w } else { w_of(df) };
            let v = c as f64 * w;
            s += v * v;
        }
        nb.push(s);
    }

    // ---- 序列化 ----
    let doc_count = docs.len();
    let gram_count = grams.len();

    // 板块名表
    let mut boardblob: Vec<u8> = Vec::new();
    boardblob.extend_from_slice(&(board_ids.len() as u32).to_le_bytes());
    for b in &board_ids {
        boardblob.push(b.len().min(255) as u8);
        boardblob.extend_from_slice(b.as_bytes());
    }

    let mut qid_off: Vec<u32> = Vec::with_capacity(doc_count);
    let mut qid_len: Vec<u16> = Vec::with_capacity(doc_count);
    let mut qidblob: Vec<u8> = Vec::with_capacity(doc_count * 24);
    for d in docs.iter() {
        qid_off.push(qidblob.len() as u32);
        qid_len.push(d.q_id.len().min(65535) as u16);
        qidblob.extend_from_slice(d.q_id.as_bytes());
    }

    // postings
    let mut postbuf: Vec<u8> = Vec::with_capacity(64 << 20);
    let mut gt: Vec<u8> = Vec::with_capacity(gram_count * GT_ENTRY);
    for k in &grams {
        let list = &post[k];
        let off = postbuf.len() as u32;
        let mut prev = 0u32;
        for &(doc, tf) in list.iter() {
            put_uvar(&mut postbuf, (doc - prev) as u64);
            put_uvar(&mut postbuf, tf as u64);
            prev = doc;
        }
        let len = postbuf.len() as u32 - off;
        let w = w_of(list.len());
        gt.extend_from_slice(&k.to_le_bytes());
        gt.extend_from_slice(&off.to_le_bytes());
        gt.extend_from_slice(&len.to_le_bytes());
        gt.extend_from_slice(&w.to_le_bytes());
    }
    println!(
        "postings {:.0} MB，gram 表 {:.0} MB，{:.1}s",
        postbuf.len() as f64 / 1048576.0,
        gt.len() as f64 / 1048576.0,
        t0.elapsed().as_secs_f64()
    );

    // 各段偏移
    let boardblob_off = HEADER_LEN as u64;
    let nb_off = boardblob_off + boardblob.len() as u64;
    let board_off = nb_off + (doc_count * 8) as u64;
    let flags_off = board_off + doc_count as u64;
    let round_off = flags_off + doc_count as u64;
    let qidoff_off = round_off + doc_count as u64;
    let qidlen_off = qidoff_off + (doc_count * 4) as u64;
    let qidblob_off = qidlen_off + (doc_count * 2) as u64;
    let gt_off = qidblob_off + qidblob.len() as u64;
    let post_off = gt_off + gt.len() as u64;

    let mut header = vec![0u8; HEADER_LEN];
    header[0..8].copy_from_slice(MAGIC);
    set32(&mut header, 8, 1); // version
    set32(&mut header, 12, GRAM_N as u32);
    set32(&mut header, 16, doc_count as u32);
    set32(&mut header, 20, gram_count as u32);
    set32(&mut header, 24, n_core);
    set32(&mut header, 28, board_ids.len() as u32);
    set64(&mut header, 32, nb_off);
    set64(&mut header, 40, board_off);
    set64(&mut header, 48, flags_off);
    set64(&mut header, 56, round_off);
    set64(&mut header, 64, qidoff_off);
    set64(&mut header, 72, qidlen_off);
    set64(&mut header, 80, qidblob_off);
    set64(&mut header, 88, gt_off);
    set64(&mut header, 96, post_off);
    set64(&mut header, 104, boardblob_off);
    // 指纹：q_id blob 长度 / doc 数 / gram 数的混合，仅用于人工排查
    let fp = qidblob.len() as u64 ^ ((doc_count as u64) << 40) ^ (gram_count as u64);
    set64(&mut header, 112, fp);
    set64(&mut header, 120, char_sum);

    let f = File::create(out_path)?;
    let mut w = BufWriter::with_capacity(8 << 20, f);
    w.write_all(&header)?;
    w.write_all(&boardblob)?;
    for v in &nb {
        w.write_all(&v.to_le_bytes())?;
    }
    for d in docs.iter() {
        w.write_all(&[d.board])?;
    }
    for d in docs.iter() {
        w.write_all(&[if d.boiler { 1u8 } else { 0u8 }])?;
    }
    for d in docs.iter() {
        w.write_all(&[d.round])?;
    }
    for v in &qid_off {
        w.write_all(&v.to_le_bytes())?;
    }
    for v in &qid_len {
        w.write_all(&v.to_le_bytes())?;
    }
    w.write_all(&qidblob)?;
    w.write_all(&gt)?;
    w.write_all(&postbuf)?;
    drop(gt);
    drop(postbuf);
    {
        let mut fref = w.into_inner()?;
        fref.seek(SeekFrom::Start(0))?;
        fref.write_all(&header)?;
        fref.sync_all()?;
    }

    let size = std::fs::metadata(out_path)?.len();
    println!(
        "已写入 {}（{:.0} MB，{} 文档 / {} gram，{:.1}s）",
        out_path,
        size as f64 / 1048576.0,
        doc_count,
        gram_count,
        t0.elapsed().as_secs_f64()
    );
    Ok(())
}

// ------------------------------------------------------------------ 打开索引

pub struct Index {
    data: Mmap,
    doc_count: usize,
    gram_count: usize,
    n_core: u32,
    nb_off: usize,
    board_off: usize,
    flags_off: usize,
    round_off: usize,
    qidoff_off: usize,
    qidlen_off: usize,
    qidblob_off: usize,
    gt_off: usize,
    post_off: usize,
    boards: Vec<String>,
    fp: u64,
    char_sum: u64,
}

impl Index {
    pub fn open(path: &str) -> Result<Self, Box<dyn std::error::Error>> {
        let f = File::open(path)?;
        let data = unsafe { Mmap::map(&f)? };
        if data.len() < HEADER_LEN || &data[0..8] != MAGIC {
            return Err("索引文件格式不对（magic 不匹配）".into());
        }
        let g32 = |at: usize| u32::from_le_bytes(data[at..at + 4].try_into().unwrap()) as usize;
        let g64 = |at: usize| u64::from_le_bytes(data[at..at + 8].try_into().unwrap()) as usize;
        let n_boards = g32(28);
        let boardblob_off = g64(104);
        let mut boards = Vec::new();
        let mut p = boardblob_off + 4;
        for _ in 0..n_boards {
            let l = data[p] as usize;
            p += 1;
            boards.push(String::from_utf8_lossy(&data[p..p + l]).to_string());
            p += l;
        }
        Ok(Index {
            doc_count: g32(16),
            gram_count: g32(20),
            n_core: g32(24) as u32,
            nb_off: g64(32),
            board_off: g64(40),
            flags_off: g64(48),
            round_off: g64(56),
            qidoff_off: g64(64),
            qidlen_off: g64(72),
            qidblob_off: g64(80),
            gt_off: g64(88),
            post_off: g64(96),
            fp: u64::from_le_bytes(data[112..120].try_into().unwrap()),
            char_sum: u64::from_le_bytes(data[120..128].try_into().unwrap()),
            boards,
            data,
        })
    }

    pub fn doc_count(&self) -> usize {
        self.doc_count
    }
    pub fn gram_count(&self) -> usize {
        self.gram_count
    }
    pub fn fingerprint(&self) -> u64 {
        self.fp
    }
    /// 全库 char_len 之和：库内容变了（哪怕条数没变）这个值就会变
    pub fn char_sum(&self) -> u64 {
        self.char_sum
    }
    pub fn board_id(&self, name: &str) -> Option<u8> {
        self.boards.iter().position(|b| b == name).map(|i| i as u8)
    }

    #[inline]
    fn nb(&self, doc: usize) -> f64 {
        let at = self.nb_off + doc * 8;
        f64::from_le_bytes(self.data[at..at + 8].try_into().unwrap())
    }
    #[inline]
    fn board(&self, doc: usize) -> u8 {
        self.data[self.board_off + doc]
    }
    #[inline]
    fn flag_boiler(&self, doc: usize) -> bool {
        self.data[self.flags_off + doc] & 1 != 0
    }
    #[inline]
    fn round(&self, doc: usize) -> u8 {
        self.data[self.round_off + doc]
    }
    #[inline]
    fn q_id(&self, doc: usize) -> &str {
        let o = u32::from_le_bytes(
            self.data[self.qidoff_off + doc * 4..self.qidoff_off + doc * 4 + 4]
                .try_into()
                .unwrap(),
        ) as usize;
        let l = u16::from_le_bytes(
            self.data[self.qidlen_off + doc * 2..self.qidlen_off + doc * 2 + 2]
                .try_into()
                .unwrap(),
        ) as usize;
        std::str::from_utf8(&self.data[self.qidblob_off + o..self.qidblob_off + o + l]).unwrap_or("")
    }

    /// 二分找 gram，返回 (postings 起始偏移, 长度, w)
    #[inline]
    fn find_gram(&self, key: u128) -> Option<(usize, usize, f64)> {
        let mut lo = 0usize;
        let mut hi = self.gram_count;
        while lo < hi {
            let mid = (lo + hi) / 2;
            let at = self.gt_off + mid * GT_ENTRY;
            let k = u128::from_le_bytes(self.data[at..at + 16].try_into().unwrap());
            if k < key {
                lo = mid + 1;
            } else if k > key {
                hi = mid;
            } else {
                let off = u32::from_le_bytes(self.data[at + 16..at + 20].try_into().unwrap()) as usize;
                let len = u32::from_le_bytes(self.data[at + 20..at + 24].try_into().unwrap()) as usize;
                let w = f64::from_le_bytes(self.data[at + 24..at + 32].try_into().unwrap());
                return Some((self.post_off + off, len, w));
            }
        }
        None
    }
}

// ------------------------------------------------------------------ 检索

pub struct Searcher {
    acc: Vec<f64>,
    stamp: Vec<u32>,
    epoch: u32,
    touched: Vec<u32>,
    default_w: f64,
}

impl Searcher {
    pub fn new(idx: &Index) -> Self {
        Searcher {
            acc: vec![0.0; idx.doc_count()],
            stamp: vec![0; idx.doc_count()],
            epoch: 0,
            touched: Vec::with_capacity(1 << 16),
            default_w: (1.0 + idx.n_core as f64).ln(),
        }
    }

    pub fn search(
        &mut self,
        idx: &Index,
        conn: &Connection,
        part: &Part,
        board: Option<&str>,
        round_no: Option<i32>,
        top: usize,
    ) -> Result<Vec<Hit>, Box<dyn std::error::Error>> {
        let q_grams = gram_counts(&part.core);
        if q_grams.is_empty() {
            return Ok(Vec::new());
        }

        // query 侧 |A|² 与每个 gram 的系数
        self.epoch = self.epoch.wrapping_add(1);
        if self.epoch == 0 {
            self.stamp.iter_mut().for_each(|s| *s = 0);
            self.epoch = 1;
        }
        self.touched.clear();

        let mut na = 0.0f64;
        let mut work: Vec<(u128, f64)> = Vec::with_capacity(q_grams.len());
        for &(k, ca) in &q_grams {
            let w = idx.find_gram(k).map(|(_, _, w)| w).unwrap_or(self.default_w);
            let wa = ca as f64 * w;
            na += wa * wa;
            work.push((k, wa * w)); // 系数 = (ca·w)·w，与 Python 的 wa·cb·w 同构
        }

        // 累加：num[doc] = Σ coeff·tf
        for (k, coeff) in work {
            if let Some((start, len, _w)) = idx.find_gram(k) {
                let buf = &idx.data[start..start + len];
                let mut p = 0usize;
                let mut doc = 0u32;
                while p < buf.len() {
                    doc += get_uvar(buf, &mut p) as u32;
                    let tf = get_uvar(buf, &mut p) as f64;
                    let d = doc as usize;
                    if self.stamp[d] != self.epoch {
                        self.stamp[d] = self.epoch;
                        self.acc[d] = 0.0;
                        self.touched.push(doc);
                    }
                    self.acc[d] += coeff * tf;
                }
            }
        }

        let want_board = board.and_then(|b| idx.board_id(b));
        if board.is_some() && want_board.is_none() {
            return Ok(Vec::new()); // 板块名不认识
        }

        // 出分
        let mut scored: Vec<(f64, u32)> = Vec::with_capacity(self.touched.len());
        for &d in self.touched.iter() {
            let doc = d as usize;
            if let Some(b) = want_board {
                if idx.board(doc) != b {
                    continue;
                }
            }
            if let Some(r) = round_no {
                if idx.round(doc) as i32 != r {
                    continue;
                }
            }
            if idx.flag_boiler(doc) {
                continue; // 默认不发套话
            }
            let sim = sim_from(self.acc[doc], na, idx.nb(doc));
            if sim > 0.0 {
                scored.push((sim, d));
            }
        }
        if scored.is_empty() {
            return Ok(Vec::new());
        }
        scored.sort_unstable_by(|a, b| {
            b.0.partial_cmp(&a.0)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| a.1.cmp(&b.1))
        });

        // 多分句时只给靠前的那批补算（和 Python 的 deferred 路径同口径）
        let q_len = part.core.chars().count().max(1);
        // LCS 窗口比 Python 大：Python 按 sim 截断到 max(40, 1.5M/len²) 条，
        // 候选变多之后，会把"sim 中等但 LCS 很高"的文档挤出窗口（实测第 8~9 名
        // 有 3 例）。索引引擎这边直接开到 300，一条 90 字的候选做 LCS 约 8 微秒。
        let keep = TOP_FOR_LCS.max(40);
        let multi = part.clauses.len() > 1;
        // 多分句补算范围限 1500 条：Python 那边是 max(40, 40000/分句数) ≈ 1.3 万，
        // 但取 1.3 万行正文要 30~600 ms，把 p95 打回原形。实测差异只落在第 8~9 名
        // 且分差 < 0.005，1500 条足够覆盖真正会被分句分数提上来的候选。
        let fetch_n = if multi {
            scored.len().min(keep.max(1500))
        } else {
            scored.len().min(keep)
        };

        // 取这批候选的文本（只要 top 这些，不碰全库）
        let ids: Vec<String> = scored[..fetch_n]
            .iter()
            .map(|&(_, d)| idx.q_id(d as usize).to_string())
            .collect();
        let mut text_of: HashMap<String, (String, String)> = HashMap::with_capacity(ids.len());
        for chunk in ids.chunks(800) {
            let ph = vec!["?"; chunk.len()].join(",");
            let sql = format!(
                "SELECT q_id, norm_core, norm_loose, norm_strict FROM query WHERE q_id IN ({})",
                ph
            );
            let mut stmt = conn.prepare(&sql)?;
            let mut rows = stmt.query(rusqlite::params_from_iter(chunk.iter()))?;
            while let Some(r) = rows.next()? {
                let q: String = r.get(0)?;
                let core: String = r.get(1)?;
                let loose: String = r.get(2)?;
                let strict: String = r.get(3)?;
                text_of.insert(q, (if core.is_empty() { loose } else { core }, strict));
            }
        }

        // 分句补算
        if multi {
            let sides: Vec<(Vec<(u128, u32)>, f64)> = part
                .clauses
                .iter()
                .map(|c| {
                    let g = gram_counts(c);
                    let mut s = 0.0;
                    for &(k, ca) in &g {
                        let w = idx.find_gram(k).map(|(_, _, w)| w).unwrap_or(self.default_w);
                        s += (ca as f64 * w) * (ca as f64 * w);
                    }
                    (g, s)
                })
                .collect();
            for i in 0..fetch_n {
                let (sim, d) = scored[i];
                let qid = idx.q_id(d as usize);
                let Some((target, _)) = text_of.get(qid) else { continue };
                let cc = gram_counts(target);
                let mut nb = 0.0f64;
                let mut cmap: FxHashMap<u128, (u32, f64)> = FxHashMap::default();
                for &(k, c) in &cc {
                    let w = idx.find_gram(k).map(|(_, _, w)| w).unwrap_or(self.default_w);
                    let v = c as f64 * w;
                    nb += v * v;
                    cmap.insert(k, (c, w));
                }
                let mut best = 0.0f64;
                for (g, sna) in &sides {
                    if *sna == 0.0 || nb == 0.0 {
                        continue;
                    }
                    let mut num = 0.0f64;
                    for &(k, ca) in g {
                        if let Some(&(cnt, w)) = cmap.get(&k) {
                            num += (ca as f64 * w) * cnt as f64 * w;
                        }
                    }
                    let s = sim_from(num, *sna, nb) * CLAUSE_DISCOUNT;
                    if s > best {
                        best = s;
                    }
                }
                if best > sim {
                    scored[i].0 = best;
                }
            }
            scored.sort_unstable_by(|a, b| {
            b.0.partial_cmp(&a.0)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| a.1.cmp(&b.1))
        });
        }

        // LCS + exact + 最终分
        let q_chars: Vec<char> = part.core.chars().collect();
        let mut hits: Vec<Hit> = Vec::with_capacity(keep);
        for &(sim, d) in scored.iter().take(keep) {
            let qid = idx.q_id(d as usize);
            let Some((target, strict)) = text_of.get(qid) else { continue };
            let tc: Vec<char> = target.chars().collect();
            let ln = lcs_len(&q_chars, &tc);
            let score = final_score(sim, ln, q_chars.len(), tc.len());
            let exact = !part.strict.is_empty() && strict.contains(&part.strict);
            hits.push(Hit {
                q_id: qid.to_string(),
                score: round_to(score, 4),
                sim: round_to(sim, 3),
                lcs: ln,
                exact,
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
}
