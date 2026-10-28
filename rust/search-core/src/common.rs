// 公共类型与算法常量。和 src/search.py 的常量必须一一对应。
use serde::{Deserialize, Serialize};

pub const GRAM_N: usize = 4;
pub const W_GRAM: f64 = 0.70;
pub const W_LCS: f64 = 0.20;
pub const W_COS: f64 = 0.5;
pub const W_COV: f64 = 0.5;
pub const CLAUSE_DISCOUNT: f64 = 0.9;
pub const CLAUSE_WORK_BUDGET: usize = 40_000;
pub const TOP_FOR_LCS: usize = 300;
pub const LCS_CHAR_BUDGET: usize = 1_500_000;

#[derive(Deserialize)]
pub struct Part {
    pub text: String,
    pub core: String,
    pub loose: String,
    pub strict: String,
    #[serde(default)]
    pub clauses: Vec<String>,
}

#[derive(Serialize)]
pub struct Hit {
    pub q_id: String,
    pub score: f64,
    pub sim: f64,
    pub lcs: usize,
    pub exact: bool,
}

/// 4 个 char 打包成 u128（键用整数比字符串快，也省内存）
#[inline]
pub fn pack_chars(c: &[char]) -> u128 {
    let mut v: u128 = 0;
    for &ch in c {
        v = (v << 32) | (ch as u128);
    }
    v
}

/// 4-gram 计数：先收集再排序合并，比 HashMap 快，也不分配键。
pub fn gram_counts(s: &str) -> Vec<(u128, u32)> {
    let chars: Vec<char> = s.chars().collect();
    if chars.is_empty() {
        return Vec::new();
    }
    if chars.len() < GRAM_N {
        return vec![(pack_chars(&chars), 1)];
    }
    let mut keys: Vec<u128> = (0..=chars.len() - GRAM_N)
        .map(|i| pack_chars(&chars[i..i + GRAM_N]))
        .collect();
    keys.sort_unstable();
    let mut out: Vec<(u128, u32)> = Vec::with_capacity(keys.len());
    for k in keys {
        match out.last_mut() {
            Some(last) if last.0 == k => last.1 += 1,
            _ => out.push((k, 1)),
        }
    }
    out
}

/// 最长公共子串长度（滚动数组 DP，按 char 比较，不是字节）。
pub fn lcs_len(a: &[char], b: &[char]) -> usize {
    if a.is_empty() || b.is_empty() {
        return 0;
    }
    let mut prev = vec![0u32; b.len() + 1];
    let mut cur = vec![0u32; b.len() + 1];
    let mut best = 0u32;
    for &ca in a {
        for j in 1..=b.len() {
            if ca == b[j - 1] {
                let v = prev[j - 1] + 1;
                cur[j] = v;
                if v > best {
                    best = v;
                }
            } else {
                cur[j] = 0;
            }
        }
        std::mem::swap(&mut prev, &mut cur);
        for x in cur.iter_mut() {
            *x = 0;
        }
    }
    best as usize
}

/// 按 Python 的口径保留小数位（Rust 的浮点格式化同为 round-half-to-even）。
pub fn round_to(x: f64, digits: usize) -> f64 {
    format!("{:.*}", digits, x).parse().unwrap_or(x)
}

/// 最终分：0.70·sim + 0.20·lcs_ratio
pub fn final_score(sim: f64, lcs: usize, q_len: usize, t_len: usize) -> f64 {
    let denom = q_len.min(if t_len == 0 { 1 } else { t_len }).max(1);
    let ratio = lcs as f64 / denom as f64;
    W_GRAM * sim + W_LCS * ratio
}

/// sim = 0.5·余弦 + 0.5·覆盖度（A 侧已预加权）
pub fn sim_from(num: f64, na: f64, nb: f64) -> f64 {
    if na == 0.0 || nb == 0.0 || num == 0.0 {
        return 0.0;
    }
    W_COS * (num / (na * nb).sqrt()) + W_COV * (num / na)
}
