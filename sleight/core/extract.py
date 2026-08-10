"""正文清洗与字段提取（L0）：在**页面内**做，零 Python 依赖。

Sleight 手里就有渲染后的真 DOM —— 提取直接在页面里跑一段 JS，既不引入 trafilatura/
readability 这类 Python/Node 依赖（核心运行依赖仍只有 websocket-client），又天然吃到 JS
执行后的最终 DOM（登录后、动态渲染的内容都在），这正是纯静态 HTML 抽取库吃不到的。

正文用 readability 式的密度打分（结构标签优先 article/main，其次按文本长度×低链接密度
打分，中英文标点都计入）—— 调研里 Readability 在中文/混合页面上综合更稳，这里的内建引擎
就是这一路，对中英文都适用。

明确边界：这是 L0。想要 trafilatura / Mozilla Readability 这类**外部引擎**（尤其对纯静态
HTML 批量抽取、或要跑离线基准）的，把它们做成可选 Extra 适配到 :class:`ExtractedDocument`
即可 —— ``engine`` 字段就是留给它们的插点。本模块不强依赖任何外部库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .session import Session

__all__ = ["ExtractedDocument"]


@dataclass
class ExtractedDocument:
    """一次正文 + 字段提取的结果。

    :param url: 提取时的 ``location.href``
    :param title: og:title / twitter:title / ``document.title``
    :param lang: ``<html lang>``
    :param byline: 作者（meta author / article:author / twitter:creator）
    :param excerpt: 摘要（og:description / description）
    :param site_name: og:site_name
    :param published: 发布时间（article:published_time）
    :param image: 主图（og:image / twitter:image）
    :param canonical: ``<link rel=canonical>``
    :param text: 清洗后的正文纯文本
    :param html: 清洗后的正文 HTML（去掉 script/style/noscript/iframe/svg）
    :param links: 正文里的链接 ``[{href, text}]``
    :param metadata: 所有 ``<meta name|property>`` 的原始键值
    :param json_ld: 页面里所有 ``application/ld+json`` 解析后的对象
    :param char_count: 正文字符数（中日韩文本看这个）
    :param word_count: 正文空白分词数（英文类看这个）
    :param link_density: 选中正文块的链接文字占比（0~1，越高越像导航/列表）
    :param quality: **工程质量分**（0~1），未校准，不是概率；低分建议回退或换引擎
    :param engine: 产出该结果的引擎名（内建为 ``"builtin"``，外部适配器可覆盖）
    """

    url: str
    title: str
    lang: str | None
    byline: str | None
    excerpt: str | None
    site_name: str | None
    published: str | None
    image: str | None
    canonical: str | None
    text: str
    html: str
    links: list[dict[str, str]] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    json_ld: list[Any] = field(default_factory=list)
    char_count: int = 0
    word_count: int = 0
    link_density: float = 0.0
    quality: float = 0.0
    engine: str = "builtin"

    @property
    def low_quality(self) -> bool:
        """质量分偏低 —— 正文太短或链接密度过高，多半没抽准。"""
        return self.quality < 0.3


# 页面内提取脚本：返回一个 JSON-可序列化对象。structural(article/main) 优先，其次密度打分。
_EXTRACT_JS = r"""
(() => {
  const meta = {};
  for (const m of document.querySelectorAll('meta[name], meta[property]')) {
    const key = m.getAttribute('name') || m.getAttribute('property');
    const val = m.getAttribute('content');
    if (key && val && !(key in meta)) meta[key] = val;
  }
  const pick = (...keys) => { for (const k of keys) if (meta[k]) return meta[k]; return null; };

  const jsonld = [];
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { jsonld.push(JSON.parse(s.textContent)); } catch (e) { /* 跳过坏的 */ }
  }
  const canonEl = document.querySelector('link[rel=canonical]');

  const textLen = el => (el.innerText || '').length;
  const linkDensity = el => {
    const t = (el.innerText || '').length; if (!t) return 1;
    let l = 0; el.querySelectorAll('a').forEach(a => l += (a.innerText || '').length);
    return l / t;
  };

  let main = null;
  for (const sel of ['article', 'main', '[role=main]']) {
    let best = null;
    for (const el of document.querySelectorAll(sel)) {
      if (textLen(el) > 200 && (!best || textLen(el) > textLen(best))) best = el;
    }
    if (best) { main = best; break; }
  }
  if (!main) {
    let bestScore = 0;
    for (const el of document.querySelectorAll('article,section,div,td,li,main')) {
      const len = textLen(el); if (len < 200) continue;
      const ld = linkDensity(el); if (ld > 0.5) continue;
      const punct = ((el.innerText || '').match(/[，,、。．\.；;：:！!？?]/g) || []).length;
      const score = len * (1 - ld) + punct * 10;
      if (score > bestScore) { bestScore = score; main = el; }
    }
  }
  if (!main) main = document.body;

  const clone = main.cloneNode(true);
  clone.querySelectorAll('script,style,noscript,iframe,svg').forEach(n => n.remove());

  const links = [];
  main.querySelectorAll('a[href]').forEach(a => {
    const t = (a.innerText || '').trim();
    if (t) links.push({ href: a.href, text: t.slice(0, 200) });
  });

  const contentText = (main.innerText || '').trim();
  return {
    url: location.href,
    title: pick('og:title', 'twitter:title') || document.title || '',
    lang: document.documentElement.lang || null,
    byline: pick('author', 'article:author', 'og:article:author', 'twitter:creator'),
    excerpt: pick('og:description', 'twitter:description', 'description'),
    site_name: pick('og:site_name'),
    published: pick('article:published_time', 'og:article:published_time', 'date'),
    image: pick('og:image', 'twitter:image'),
    canonical: canonEl ? canonEl.href : null,
    meta: meta,
    jsonld: jsonld,
    content_text: contentText,
    content_html: clone.innerHTML,
    links: links.slice(0, 200),
    link_density: linkDensity(main),
  };
})()
"""


def extract_document(session: Session, *, min_length: int = 200) -> ExtractedDocument:
    """在当前页面里做提取，产出 :class:`ExtractedDocument`。

    :param min_length: 正文短于这个字符数时，回退到整页 ``innerText``（宁可多给上下文，
        也不给一段抽错的短正文）
    """
    data = session._evaluate(
        {"expression": _EXTRACT_JS, "returnByValue": True, "awaitPromise": True}
    ) or {}

    text = data.get("content_text") or ""
    if len(text) < min_length:
        # 没抽到像样正文：回退整页可见文本，别返回一段抽错的短内容
        fallback = session.text()
        if len(fallback) > len(text):
            text = fallback

    link_density = float(data.get("link_density") or 0.0)
    char_count = len(text)
    word_count = len(text.split())
    # 工程质量分（非概率）：长度饱和 × 低链接密度
    length_factor = min(1.0, char_count / 800.0)
    quality = round(length_factor * (1.0 - link_density), 3)

    return ExtractedDocument(
        url=data.get("url") or session.url(),
        title=data.get("title") or "",
        lang=data.get("lang"),
        byline=data.get("byline"),
        excerpt=data.get("excerpt"),
        site_name=data.get("site_name"),
        published=data.get("published"),
        image=data.get("image"),
        canonical=data.get("canonical"),
        text=text,
        html=data.get("content_html") or "",
        links=list(data.get("links") or []),
        metadata=dict(data.get("meta") or {}),
        json_ld=list(data.get("jsonld") or []),
        char_count=char_count,
        word_count=word_count,
        link_density=link_density,
        quality=quality,
    )
