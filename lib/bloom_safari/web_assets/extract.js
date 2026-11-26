// Readability is loaded into the same lexical closure, never a remote script.
const options = BLOOM_OPTIONS;
const norm = s => (s || '').replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').replace(/\n[ \t]+/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
const visible = e => !e.closest('[hidden],[aria-hidden="true"]') && getComputedStyle(e).display !== 'none';
const metrics = () => ({url:location.href, title:document.title, ready:document.readyState,
    width:innerWidth, height:innerHeight, outerWidth, outerHeight, dpr:devicePixelRatio,
    scroll_y:scrollY, page_height:Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0),
    occlusion_top:Math.ceil([...document.querySelectorAll('header,nav,[role=navigation],[class*=sticky],[class*=fixed]')].reduce((bottom,e)=>{
        const style=getComputedStyle(e), r=e.getBoundingClientRect();
        return (style.position==='fixed'||style.position==='sticky') && r.top<=1 && r.bottom>0 && r.bottom<innerHeight/3 && r.width>innerWidth/2 ? Math.max(bottom,r.bottom) : bottom;
    },0))});
if (options.mode === 'metrics') return JSON.stringify(metrics());
if (options.mode === 'scroll') {
    scrollTo({top:options.start, left:0, behavior:'instant'});
    return JSON.stringify(metrics());
}
if (options.mode === 'search') {
    const seen = new Set(), results = [];
    for (const a of document.querySelectorAll('a:has(h3)')) {
        const url = new URL(a.href, location.href);
        if (!/^https?:$/.test(url.protocol) || /(^|\.)google\.[a-z.]+$/.test(url.hostname) || seen.has(url.href)) continue;
        const title = norm(a.querySelector('h3').innerText);
        if (!title) continue;
        seen.add(url.href);
        const container = a.closest('div.MjjYud') || a.parentElement.parentElement;
        results.push({url:url.href, title, snippet:norm(container.innerText).slice(0, 1600)});
    }
    return JSON.stringify({...metrics(), results, page_notice:norm(document.body?.innerText).slice(0, 1500)});
}
if (!document.body) return JSON.stringify({...metrics(), status:'empty', text:'', tables:[], links:[], visual_regions:[]});
const bodyText = norm(document.body.innerText);
const challengeTitle = /^(just a moment|access denied|attention required|security check|verify you are human|verifying your browser|checking your browser|robot check|403 forbidden)/i.test(document.title);
const challengeBody = bodyText.length < 5000 && /^(.*\n){0,5}(verify (you are|you're) human|checking your browser|our systems have detected unusual traffic|please enable js and disable any ad blocker)/i.test(bodyText);
const pdf = document.contentType === 'application/pdf' || !!document.querySelector('embed[type="application/pdf"]');
const login = bodyText.length < 6000 && !!document.querySelector('input[type=password]') && /sign in|log in|login/i.test(bodyText);
if (challengeTitle || challengeBody || pdf || login) return JSON.stringify({...metrics(), status:pdf?'unsupported_pdf':login?'login_required':'blocked', text:'', notice:pdf?'Safari PDF text extraction is unavailable; no PDF contents were reviewed.':bodyText.slice(0,1500), tables:[], links:[], visual_regions:[]});
const root = document.querySelector('main,[role=main]') || document.querySelector('article') || document.body;
const originalText = norm(root.innerText);
let article = null, parseError = null;
try { article = new Readability(document.cloneNode(true), {maxElemsToParse:50000}).parse(); }
catch (e) { parseError = String(e).slice(0,300); }
// Produce one document. Capture inventories below are retained on disk only.
// A URL is defined once, and only when an actual content anchor uses it.
const references = new Map();
function contentLink(a, text) {
    let url;
    try { url = new URL(a.getAttribute('href'), location.href); }
    catch (_) { return text; }
    if (!text.trim() || !/^https?:$/.test(url.protocol)) return text;
    if (a.matches('[role=button],[aria-controls]') || a.closest('nav,[role=navigation],header,footer')) return text;
    const control = /^(reply|vote|upvote|downvote|hide|favorite|log ?in|sign ?in|sign ?up|log ?out|share|subscribe)$/i;
    if (control.test(norm(text))) return text;
    if (url.hostname === 'news.ycombinator.com' &&
        (/^\/(user|reply|vote|hide|fave|login)$/.test(url.pathname) ||
         (url.pathname === '/item' && a.closest('.age,.subtext,.comhead')))) return text;
    if (!references.has(url.href)) references.set(url.href, 'ref-' + (references.size + 1));
    const ref = references.get(url.href);
    const label = norm(text).replace(/\\/g, '\\\\').replace(/([\[\]])/g, '\\$1');
    return label === url.href ? '[' + ref + ']' : '[' + label + '][' + ref + ']';
}
function dataTable(table) {
    if (table.matches('[role=presentation],[role=none]') || table.querySelector('table')) return false;
    const rows = [...table.rows], cells = rows.flatMap(row => [...row.cells]);
    if (!cells.length) return false;
    if (table.caption || table.matches('[role=table],[role=grid]') || cells.some(c => c.tagName === 'TH')) return true;
    // Rectangular headerless data is still useful. Nested/one-row layout tables
    // take the ordinary text path and are never expanded a second time.
    return rows.length >= 2 && rows.every(row => row.cells.length >= 2 && row.cells.length === rows[0].cells.length);
}
function tableText(table) {
    const rows = [...table.rows], occupied = [], grid = [];
    for (let r = 0; r < rows.length; r++) {
        const line = []; let column = 0;
        for (const cell of rows[r].cells) {
            while ((occupied[column] || 0) > r) { line.push('↳'); column++; }
            let value = norm(plain(cell)).replace(/\|/g, '\\|').replace(/\n/g, '<br>');
            if (cell.tagName === 'TH') value = '**' + value + '**';
            const colspan = cell.colSpan, rowspan = cell.rowSpan;
            const spans = [];
            if (rowspan !== 1) spans.push('rowspan=' + rowspan);
            if (colspan !== 1) spans.push('colspan=' + colspan);
            if (spans.length) value += ' {' + spans.join(', ') + '}';
            line.push(value);
            const groupRows = [...rows[r].parentElement.rows];
            const height = rowspan || groupRows.length - groupRows.indexOf(rows[r]);
            for (let c = 0; c < colspan; c++) {
                occupied[column + c] = r + height;
                if (c) line.push('↳');
            }
            column += colspan;
        }
        while (column < occupied.length) {
            line.push((occupied[column] || 0) > r ? '↳' : ''); column++;
        }
        grid.push(line);
    }
    const width = grid.reduce((width, row) => Math.max(width, row.length), 0);
    const lines = grid.map(row => '| ' + [...row, ...Array(width - row.length).fill('')].join(' | ') + ' |');
    if ([...rows[0].cells].some(c => c.tagName === 'TH')) lines.splice(1, 0, '| ' + Array(width).fill('---').join(' | ') + ' |');
    const caption = table.caption ? norm([...table.caption.childNodes].map(plain).join('')) + '\n' : '';
    return '\n\n' + caption + lines.join('\n') + '\n\n';
}
// Serialize block boundaries explicitly; textContent alone joins paragraphs/cells.
function plain(n) {
    if (n.nodeType === Node.TEXT_NODE) return n.textContent;
    if (n.nodeType !== Node.ELEMENT_NODE) return '';
    if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|NAV|FORM|BUTTON|INPUT|SELECT|TEXTAREA)$/.test(n.tagName) || n.hasAttribute('hidden') || n.getAttribute('aria-hidden') === 'true') return '';
    if (n.tagName === 'BR') return '\n';
    if (n.tagName === 'TABLE' && dataTable(n)) return tableText(n);
    const text = [...n.childNodes].map(plain).join('');
    if (n.tagName === 'A' && n.hasAttribute('href')) return contentLink(n, text);
    if (n.tagName === 'TR' && n.classList.contains('comtr') && /^\d+$/.test(n.id)) return '\nComment ' + n.id + ':\n' + text + '\n';
    if (/^(TD|TH)$/.test(n.tagName)) return text + '\t';
    return /^(H[1-6]|P|DIV|SECTION|ARTICLE|TR|LI|PRE|BLOCKQUOTE|FIGCAPTION|TABLE)$/.test(n.tagName) ? '\n' + text + '\n' : text;
}
// Use the full semantic root when an article heuristic omits a substantial portion.
let text, method;
// Readability flattens HN rows and strips their identity/control context.
// Retain discussion structure so authors/timestamps/IDs stay attached and
// permalink controls do not become hundreds of apparent source references.
const discussion = !!root.querySelector('tr.comtr[id]');
if (!discussion && article && article.length >= 500 && article.length >= originalText.length * 0.8) {
    const parsed = new DOMParser().parseFromString(article.content, 'text/html');
    text = norm(plain(parsed.body)); method = 'readability';
} else {
    const cloned = root.cloneNode(true);
    if (root === document.body) cloned.querySelectorAll('header,footer,nav,[role=navigation],[role=banner]').forEach(e => e.remove());
    text = norm(plain(cloned)); method = root === document.body ? 'body_without_navigation' : 'semantic_root';
}
if (references.size) text += '\n\n' + [...references].map(([url, ref]) => '[' + ref + ']: <' + url.replace(/</g, '%3C').replace(/>/g, '%3E') + '>').join('\n');
const tables = [...root.querySelectorAll('table')].filter(visible).map((table,i) => ({id:'table-'+(i+1),
    caption:norm(table.caption?.innerText), top:Math.round(table.getBoundingClientRect().top+scrollY),
    rows:[...table.rows].map(row => [...row.cells].map(cell=>({text:norm(cell.innerText),colspan:cell.colSpan,rowspan:cell.rowSpan,header:cell.tagName==='TH'})))}));
const links = [...root.querySelectorAll('a[href]')].filter(visible).map(a=>({text:norm(a.innerText),url:a.href})).filter(a=>a.text && /^https?:/.test(a.url));
const regions = [...root.querySelectorAll('img,svg,canvas,video')].filter(visible).map(e=>{
    const r=e.getBoundingClientRect(); return {kind:e.tagName.toLowerCase(),top:Math.round(r.top+scrollY),left:Math.round(r.left),width:Math.round(r.width),height:Math.round(r.height),
        description:norm(e.getAttribute('alt') || e.getAttribute('aria-label') || e.closest('figure')?.querySelector('figcaption')?.innerText).slice(0,500)};
}).filter(r=>r.width>60 && r.height>40);
const iframeCount = root.querySelectorAll('iframe').length;
const dialogs = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')].filter(visible).map(e=>norm(e.innerText).slice(0,300));
return JSON.stringify({...metrics(), status:text.length?'ok':'empty', text, text_format:'markdown-v1', method, parse_error:parseError,
    author:article?.byline || '', published:article?.publishedTime || document.querySelector('meta[property="article:published_time"]')?.content || '',
    headings:[...root.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(visible).map(e=>({text:norm(e.innerText),level:Number(e.tagName[1]),top:Math.round(e.getBoundingClientRect().top+scrollY)})),
    tables, links, visual_regions:regions.slice(0,100), visual_regions_total:regions.length,
    coverage:{text:'Complete extracted substantive text from the currently loaded DOM; unopened tabs/accordions, unloaded content and child frames are not claimed.',iframe_count:iframeCount,dialogs}});
