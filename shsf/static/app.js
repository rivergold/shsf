'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name=shsf-token]').content;
let state = null, mode = 'all', page = 1, results = null, busy = false, searchRun = 0;
let zstart = 0, ztotal = 0, zchosen = new Set(), zrun = 0, zbusy = false;
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
const notice = message => { $('notice').textContent = message; };
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-SHSF-Token':token}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) { if (data.state) { state = data.state; render(); } throw new Error(data.error || '请求失败，请重试。'); }
  return data;
}
function setBusy(value) {
  busy = value;
  document.querySelectorAll('[data-mutation]').forEach(n => { n.disabled = value; });
  $('review').disabled = value || !state?.items.length;
  $('confirm-save').disabled = value;
  $('import').disabled = value || zbusy || !zchosen.size;
}
async function mutate(path, body = {}) {
  if (busy || !state) return false;
  setBusy(true);
  try { const data = await api(path, {...body, expected_revision:state.revision}); state = data.state; render(); return data; }
  catch (e) { notice(e.message); return false; }
  finally { setBusy(false); }
}
const statusText = item => item.match_status === 'matched' ? '已匹配本地论文' : item.match_status === 'ambiguous' ? '匹配存疑 · 待处理' : item.match_status === 'source_deleted' ? '源记录已撤回' : '未入本地库 · 待处理';
function render() {
  $('count').textContent = state.items.length;
  const box = $('selected-list'); box.replaceChildren();
  if (!state.items.length) { const empty = el('div','empty'); empty.append(el('div','symbol','✧'), el('div','','还没有选择论文'), el('p','hint','搜索你喜欢的工作，或导入 Zotero 收藏。')); box.append(empty); }
  for (const item of state.items) {
    const row = el('article','seed'), content = el('div','seed-content');
    content.append(el('div','seed-title',item.title), el('div','authors',`${item.authors.slice(0,2).join(', ')}${item.authors.length>2?' et al.':''} · ${item.published_year || '年份未知'}`));
    if (item.sources.some(s => s.source_kind === 'zotero')) content.append(el('span','tag','Zotero'));
    content.append(el('span',`tag ${item.match_status==='matched'?'':'pending'}`,statusText(item)));
    const remove = el('button','','×'); remove.title = '移除这篇论文'; remove.setAttribute('aria-label',`移除 ${item.title}`); remove.dataset.mutation = ''; remove.disabled = busy;
    remove.onclick = async () => { if (await mutate('/api/seeds/remove',{seed_id:item.seed_id})) notice('已更新种子草稿。'); };
    row.append(content, remove); box.append(row);
  }
  $('selection-stats').textContent = `${state.matched} 篇已匹配 · ${state.pending} 篇待处理`;
  $('footer-title').textContent = state.confirmed ? '当前兴趣论文已确认' : state.items.length ? `已选择 ${state.items.length} 篇兴趣论文` : '从几篇启发你的论文开始';
  $('footer-sub').textContent = state.confirmed ? '快照已保存。调整选择后可重新确认。' : '草稿已自动保存在本机数据库。';
  $('review').textContent = state.confirmed ? '查看并重新确认 →' : '确认兴趣论文 →';
  setBusy(busy); if (results) renderResults();
}
async function search() {
  const run = ++searchRun;
  $('results-info').textContent = '检索中…'; $('prev').disabled = $('next').disabled = true;
  try {
    const query = new URLSearchParams({q:$('query').value.trim(), mode, sort:$('sort').value, page});
    const data = await api('/api/papers?' + query);
    if (run !== searchRun) return;
    results = data; renderResults();
  } catch(e) { if(run===searchRun) { $('results-info').textContent = '检索失败'; $('results').replaceChildren(el('p','empty',e.message)); } }
}
function renderResults() {
  $('results-info').textContent = `共 ${results.total.toLocaleString()} 篇论文`;
  $('page-info').textContent = `第 ${results.page} / ${Math.max(1,Math.ceil(results.total/results.page_size))} 页`;
  $('prev').disabled = page <= 1; $('next').disabled = page * results.page_size >= results.total;
  const box = $('results'); box.replaceChildren();
  if (!results.items.length) box.append(el('div','empty','没有找到论文。试试其他关键词、作者或 arXiv ID。'));
  for (const item of results.items) {
    const selected = state?.items.find(s => s.arxiv_id === item.arxiv_id);
    const row = el('article','paper'), check = el('button',`check ${selected?'chosen':''}`,selected?'✓':'+');
    check.setAttribute('aria-label',`${selected?'移除':'添加'} ${item.title}`); check.setAttribute('aria-pressed',Boolean(selected)); check.dataset.mutation = ''; check.disabled = busy;
    check.onclick = async () => { if (await mutate(selected?'/api/seeds/remove':'/api/seeds/add',selected?{seed_id:selected.seed_id}:{arxiv_id:item.arxiv_id})) notice('已自动保存种子草稿。'); };
    const content = el('div','paper-content'); content.append(el('h3','',item.title),el('p','authors',`${item.authors.slice(0,3).join(', ')}${item.authors.length>3?' et al.':''} · ${item.published_at.slice(0,4)}`));
    for(const cat of item.categories.slice(0,3)) content.append(el('span','tag',cat));
    content.append(el('span','tag',`arXiv:${item.arxiv_id}`));
    const abstract = el('p','abstract',item.abstract), details = el('button','details','展开摘要 ↓');
    details.onclick = () => { const expanded = abstract.classList.toggle('expanded'); details.textContent = expanded?'收起摘要 ↑':'展开摘要 ↓'; };
    content.append(abstract,details); row.append(check,content); box.append(row);
  }
}
$('search-form').onsubmit = event => { event.preventDefault(); page=1; search(); };
document.querySelectorAll('[data-mode]').forEach(button => button.onclick=()=>{ mode=button.dataset.mode; document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('active',b===button)); page=1; search(); });
$('sort').onchange = () => {page=1; search();};
$('prev').onclick = () => {page--;search();}; $('next').onclick = () => {page++;search();};
document.querySelectorAll('dialog .close').forEach(b => b.onclick = () => b.closest('dialog').close());
$('review').onclick = () => { $('confirm-summary').textContent=`共 ${state.items.length} 篇：${state.matched} 篇已匹配本地库，${state.pending} 篇待处理。${state.items.length<20?'当前少于建议的 20 篇，仍可确认保存。':''}`; $('confirm-dialog').showModal(); };
$('confirm-save').onclick = async () => { if(await mutate('/api/seeds/confirm')) { $('confirm-dialog').close(); notice('兴趣论文已确认。补齐向量后可生成相似度推荐。'); } };
$('theme').onclick = () => { document.body.classList.toggle('dark'); try{localStorage.setItem('shsf-theme',document.body.classList.contains('dark')?'dark':'light');}catch{} };
try{document.body.classList.toggle('dark',localStorage.getItem('shsf-theme')==='dark');}catch{}
function zmessage(text){$('zotero-message').textContent=text;}
function zstatus(status){$('connect-form').hidden=status.connected; $('zotero-library').hidden=!status.connected; if(status.connected)$('zotero-account').textContent=`${status.username} · 个人文献库`;}
function zcount(){
  $('zcount').textContent=`已勾选 ${zchosen.size} 篇`;
  $('import').disabled=busy||zbusy||!zchosen.size;
  const checks=[...$('zotero-items').querySelectorAll('input[type="checkbox"]')];
  $('zselect-all').disabled=zbusy||!checks.length;
  $('zselect-all').textContent=checks.length&&checks.every(check=>check.checked)?'取消本页全选':'全选本页';
}
async function loadCollections(){
  const select=$('collections'); select.replaceChildren(new Option('全部文献',''));
  let start=0, total=1;
  while(start<total){const data=await api('/api/zotero/collections?start='+start); for(const c of data.items)select.add(new Option((c.parent?'↳ ':'')+c.name,c.key)); total=data.total; start+=100;}
}
async function loadZitems(){
  const run=++zrun; zmessage('正在读取 Zotero 条目…'); zbusy=true; zcount(); $('zprev').disabled=$('znext').disabled=true;
  try{
    const data=await api('/api/zotero/items?'+new URLSearchParams({collection:$('collections').value,start:zstart})); if(run!==zrun)return;
    ztotal=data.total; const box=$('zotero-items'); box.replaceChildren();
    if(!data.items.length)box.append(el('p','empty','当前页没有可导入的论文条目。'));
    for(const item of data.items){const row=el('div','zitem'),check=el('input');check.type='checkbox';check.id='zitem-'+item.external_key;check.checked=zchosen.has(item.external_key);check.onchange=()=>{if(check.checked)zchosen.add(item.external_key);else zchosen.delete(item.external_key);zcount();};const label=el('label','',item.title);label.htmlFor=check.id;label.append(el('span','hint',`${item.authors.slice(0,2).join(', ')} · ${item.published_year||'年份未知'}`),el('span',`tag ${item.match_status==='matched'?'':'pending'}`,statusText(item)));row.append(check,label);box.append(row);}
    $('zpage').textContent=`${ztotal?Math.floor(zstart/50)+1:1} / ${Math.max(1,Math.ceil(ztotal/50))} 页`;
    $('zprev').disabled=zstart===0; $('znext').disabled=zstart+50>=ztotal; zmessage('勾选希望用于兴趣建模的论文。');
  }catch(e){if(run===zrun)zmessage(e.message);}finally{if(run===zrun){zbusy=false;zcount();}}
}
async function openZotero(){ $('zotero-dialog').showModal(); try{const data=await api('/api/bootstrap');zstatus(data.zotero);if(data.zotero.connected){await loadCollections();zstart=0;zchosen.clear();await loadZitems();}}catch(e){zmessage(e.message);} }
$('zotero-open').onclick=openZotero;
$('connect-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;zmessage('正在验证只读权限…');try{const data=await api('/api/zotero/connect',{api_key:$('zotero-key').value.trim(),user_id:$('zotero-user').value.trim()});$('zotero-key').value='';zstatus(data);await loadCollections();zstart=0;zchosen.clear();await loadZitems();}catch(e){$('zotero-key').value='';zmessage(e.message);}finally{button.disabled=false;}};
$('disconnect').onclick=async()=>{try{zstatus(await api('/api/zotero/disconnect',{}));++zrun;zchosen.clear();$('zotero-items').replaceChildren();zmessage('已断开连接，服务内存中的 API Key 已清除。');}catch(e){zmessage(e.message);}};
$('collections').onchange=()=>{zchosen.clear();zstart=0;zcount();loadZitems();};
$('zselect-all').onclick=()=>{
  if(zbusy)return;
  const checks=[...$('zotero-items').querySelectorAll('input[type="checkbox"]')];
  const select=!checks.every(check=>check.checked);
  if(select&&zchosen.size+checks.filter(check=>!check.checked).length>100){zmessage('每次最多勾选 100 篇，请先导入已选论文或取消部分勾选。');return;}
  for(const check of checks){check.checked=select;check.onchange();}
};
$('zprev').onclick=()=>{zstart-=50;loadZitems();};$('znext').onclick=()=>{zstart+=50;loadZitems();};
$('import').onclick=async()=>{const data=await mutate('/api/zotero/import',{item_keys:[...zchosen]});if(data){zchosen.clear();zcount();$('zotero-dialog').close();notice(`已处理 ${data.imported} 篇论文，重复项已合并。当前 ${state.pending} 篇待匹配。`);}else zmessage($('notice').textContent);};
(async()=>{try{const data=await api('/api/bootstrap');state=data.state;render();await search();}catch(e){notice(e.message);$('results-info').textContent='无法读取数据库';}})();

let feed = null, feedBusy = false, feedRun = 0;
function feedMessage(text){$('feed-notice').textContent=text;}
function feedControls(){
  $('feed-generate').disabled=feedBusy||feed?.update?.status==='running'||!feed?.context.ready_seeds||Boolean(feed?.batch);
  $('feed-generate').textContent=feedBusy?'正在生成…':feed?.update?.status==='running'?'每日更新中…':feed?.batch?'今日推荐已保存':'生成今日推荐';
  $('feed-refresh').disabled=feedBusy;
}
function sortedFeedItems(items, order){
  return [...items].sort((a,b)=>{
    const difference=order==='submitted'
      ? Date.parse(b.published_at)-Date.parse(a.published_at)
      : b.similarity-a.similarity;
    return difference||a.rank-b.rank;
  });
}
const abstractLanguages=new Map();
function abstractView(paper,expandable=false,paragraphId=null){
  const box=el('div','abstract-block');
  let expanded=!expandable;
  function render(){
    const language=abstractLanguages.get(paper.arxiv_id)||'en';
    const chinese=language==='zh'&&Boolean(paper.abstract_zh);
    const toolbar=el('div','abstract-toolbar'),languages=el('div','abstract-languages');
    languages.setAttribute('role','group');languages.setAttribute('aria-label','摘要语言');
    for(const [value,label] of [['en','英文'],['zh','中文']]){
      const button=el('button',language===value?'active':'',label);button.type='button';
      button.setAttribute('aria-pressed',String(language===value));
      button.onclick=()=>{abstractLanguages.set(paper.arxiv_id,value);render();};
      languages.append(button);
    }
    toolbar.append(el('span','hint','摘要'),languages);
    const paragraph=el('p',expanded?'abstract expanded':'abstract',chinese?paper.abstract_zh:paper.abstract);
    paragraph.lang=chinese?'zh-CN':'en';
    if(paragraphId)paragraph.id=paragraphId;
    box.replaceChildren(toolbar,paragraph);
    if(expandable){
      const label=chinese?'中文摘要':'英文摘要';
      const more=el('button','details',`${expanded?'收起':'展开'}${label} ${expanded?'↑':'↓'}`);
      more.type='button';more.onclick=()=>{expanded=!expanded;render();};box.append(more);
    }
    if(language==='zh')box.append(el('p','hint',chinese?'Agent 中文译文':paper.translation_status==='failed'?'中文摘要暂不可用，当前显示英文。':'中文摘要尚未准备好，当前显示英文。'));
  }
  render();return box;
}
function renderFeed(){
  const context=feed.context, batch=feed.batch;
  $('feed-day').textContent=`${feed.day} · ${batch?batch.items.length:0} 篇研究`;
  $('feed-stats').textContent=`${context.ready_seeds} / ${context.confirmed_seeds} 篇种子向量就绪 · 候选库 ${context.corpus_vectors.toLocaleString()} / ${context.corpus_papers.toLocaleString()} 篇具备向量`;
  const releaseContext=batch?.context||context;
  $('feed-release').textContent=releaseContext.latest_release_day
    ? `${batch?'本批使用的公告日期':'已完整导入的公告日期'} ${releaseContext.latest_release_day} · ${batch?`从该公告批次选入 ${releaseContext.latest_selected??0} 篇，目标 ${releaseContext.latest_target??6} 篇。${(releaseContext.latest_selected??0)<(releaseContext.latest_target??6)?'可用新论文不足，已由其他相关研究补齐。':''}`:'生成推荐时优先安排该批次的 6 篇新论文，其余从相关研究中选择。'}`
    : '尚无完整导入的官方公告批次；同步完成后展示可用公告日期和实际选入数量。';
  const update=feed.update, stages={announcements:'同步最新公告',incremental:'同步论文更新',embeddings:'计算论文向量',recommendations:'生成今日推荐'};
  $('feed-update').textContent=update?update.status==='completed'
    ? `最近自动更新：${new Date(update.finished_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}。`
    :update.status==='running'?`每日更新中：${stages[update.stage]||'正在处理'}。`
    :update.stage==='announcements'
      ? `最近自动更新在公告同步阶段未完成，将稍后重试；${releaseContext.latest_release_day?'仍沿用已完整导入的公告批次，所示日期不代表 arXiv 当前最新公告日期。':'完整同步后才会显示可用公告日期。'}已保存的推荐仍可阅读。`
      :'最近自动更新未完成，将稍后重试；已保存的推荐及其公告日期保持不变。'
    :'每日 08:00（北京时间）自动更新。';
  $('feed-coverage').textContent=`当前向量覆盖率 ${(context.coverage*100).toFixed(1)}%。${batch?`本批生成时覆盖率 ${(batch.context.coverage*100).toFixed(1)}%，结果保持固定。`:''}`;
  if(!context.confirmed_seeds)feedMessage('请先选择并确认兴趣论文。');
  else if(!context.ready_seeds)feedMessage('种子尚无有效向量，请先完成向量计算。');
  else feedMessage(`${context.draft_changed?'兴趣论文草稿有改动，本页仍使用上次确认的种子。 ':''}${context.pending_seeds?`${context.pending_seeds} 篇种子暂未就绪，不参与本次排序。 `:''}${context.coverage<.99?'候选向量仍在补齐中，当前结果仅覆盖已有有效向量的论文。':''}`);
  const box=$('feed-cards');box.replaceChildren();
  if(!batch){box.append(el('div','panel empty','每日更新完成后会自动生成推荐，你也可以点击“生成今日推荐”。'));feedControls();return;}
  if(!batch.items.length)box.append(el('div','panel empty','今天没有新的可用候选。候选向量仍在补齐中，也可以调整兴趣论文后重新确认。'));
  for(const [index,paper] of sortedFeedItems(batch.items,$('feed-sort').value).entries()){
    const card=el('article',`feed-card ${paper.feedback.is_read?'read':''}`), top=el('div','card-top');
    const latest=Boolean(paper.is_latest_release);
    top.append(el('span','',`#${String(index+1).padStart(2,'0')} · ${latest?'最新批次 · 公告 '+paper.announcement_day:'其他研究'} · 首次提交 ${paper.published_at.slice(0,10)}`),el('span','',`余弦相似度 ${paper.similarity.toFixed(3)}`));
    card.append(top,el('h2','',paper.title),el('p','authors',paper.authors.join(', ')));
    for(const category of [...new Set(paper.categories)])card.append(el('span','tag',category));
    card.append(abstractView(paper,true));
    const reasons=el('div','reasons');reasons.append(el('strong','','与你的这些兴趣论文相近'));
    for(const reason of paper.reasons)reasons.append(el('p','',`${reason.title} · ${reason.similarity.toFixed(3)}`));
    card.append(reasons);
    if(paper.source_deleted)card.append(el('p','hint','该论文的源记录已撤回，以下为生成推荐时保存的元数据。'));
    else{const link=el('a','paper-link','在 arXiv 查看论文 ↗');link.href='https://arxiv.org/abs/'+paper.arxiv_id;link.target='_blank';link.rel='noreferrer';card.append(link);}
    const actions=el('div','actions');
    for(const [label,value] of [['感兴趣','like'],['不感兴趣','dislike'],[paper.feedback.is_read?'已读 ✓':'标记已读','read']]){
      const active=value==='read'?paper.feedback.is_read:paper.feedback.interest===value;
      const button=el('button',active?'active':'',label);button.setAttribute('aria-pressed',active);
      button.onclick=async()=>{
        [...actions.querySelectorAll('button')].forEach(b=>b.disabled=true);
        try{const body={arxiv_id:paper.arxiv_id,...(value==='read'?{is_read:!paper.feedback.is_read}:{interest:active?'none':value})};paper.feedback=await api('/api/recommendations/feedback',body);renderFeed();}
        catch(e){feedMessage(e.message);[...actions.querySelectorAll('button')].forEach(b=>b.disabled=false);}
      };actions.append(button);
    }
    card.append(actions);
    const analysisActions=el('div','analysis-actions'),explain=el('button','outline','推荐解读'),read=el('button','outline','单篇精读 →');
    const panel=el('div','inline-analysis');panel.hidden=true;
    explain.onclick=()=>{panel.hidden=!panel.hidden;if(!panel.hidden)loadAnalysisPanel(panel,paper.arxiv_id,'explain',batch.batch_id);};
    read.onclick=()=>{location.hash='paper/'+encodeURIComponent(paper.arxiv_id);};
    analysisActions.append(explain,read);card.append(analysisActions,panel);box.append(card);
  }
  feedControls();
}
async function loadFeed(){const run=++feedRun;try{const data=await api('/api/recommendations');if(run!==feedRun)return;feed=data;renderFeed();}catch(e){feedMessage(e.message);}}
function showView(view){
  for(const name of ['feed','seeds','agent','paper','readings']){
    $(name==='seeds'?'seed-view':name+'-view').hidden=view!==name;
    const nav=$('nav-'+name);if(nav)nav.classList.toggle('nav-active',view===name||(name==='readings'&&view==='paper'&&paperFromRecords())||(name==='feed'&&view==='paper'&&!paperFromRecords()));
  }
  document.title='SHSF · '+({feed:'每日推荐',seeds:'选择兴趣论文',agent:'Agent 设置',paper:'单篇精读',readings:'精读记录'}[view]);
  clearTimeout(readingsTimer);++readingsRun;
  if(view==='feed')loadFeed();
  if(view==='agent')loadAgent();
  if(view==='paper')loadPaper();
  if(view==='readings')loadReadings(true);
}
$('nav-feed').onclick=()=>{location.hash='feed';};$('nav-seeds').onclick=$('feed-edit-seeds').onclick=()=>{location.hash='seeds';};
$('feed-refresh').onclick=loadFeed;
$('feed-sort').onchange=()=>{if(feed)renderFeed();};
$('feed-generate').onclick=async()=>{
  if(feedBusy)return;feedBusy=true;feedControls();feedMessage('正在为你排序并保存今日推荐，请稍候…');
  try{feed=await api('/api/recommendations/generate',{});renderFeed();}catch(e){feedMessage(e.message);}finally{feedBusy=false;feedControls();}
};


let agentState=null, agentChosenMode=null, agentModels=[], agentAccount=null, agentRun=0, agentPoll=null, agentBusy=false;
function agentMessage(text){$('agent-notice').textContent=text;}
function chosenAgentMode(){return document.querySelector('input[name="agent-mode"]:checked').value;}
function agentPanels(){const chat=chosenAgentMode()==='chatgpt';$('agent-api').hidden=chat;$('agent-chatgpt').hidden=!chat;}
function renderAgent(){
  const data=agentState, chat=data.chatgpt, mode=agentChosenMode||data.mode;
  document.querySelectorAll('input[name="agent-mode"]').forEach(r=>r.checked=r.value===mode);agentPanels();
  $('agent-current').textContent=`当前已保存通道：${data.mode==='api'?'API':'ChatGPT 套餐'}。API 配置以 .env 为准；页面保存的通道选择优先。`;
  const info=$('agent-api-info');info.replaceChildren();
  for(const [label,value] of [['服务地址',data.api.base_url],['协议',data.api.protocol],['密钥',data.api.api_key_configured?'已配置（隐藏）':'未配置；本机模型可不设置'],['推荐解读模型',data.api.explain_model||'未配置'],['单篇精读模型',data.api.read_model||'未配置'],['配置状态',data.api.error||(data.api.ready?'配置完整，尚需测试':data.api.missing.join('、'))]])info.append(el('dt','',label),el('dd','',value));
  const select=$('agent-profiles'), selected=select.value;
  select.replaceChildren(new Option('选择账户',''));
  for(const profile of chat.profiles)select.add(new Option(`${profile.label} · ${profile.connected?'已连接':'需登录'}`,profile.id));
  select.value=chat.active||selected;
  if(agentAccount!==chat.active){agentAccount=chat.active;agentModels=[];renderAgentModels();}
  let authStatus=chat.error||(!chat.connected?'尚未连接 ChatGPT。':chat.sharing?'已连接，已授权套餐使用。':'已完成身份登录，尚未授权套餐使用。');
  if(chat.pending)authStatus=chat.connected?authStatus+'另一次登录正在等待授权。':'等待在官方页面完成授权…';
  $('agent-auth-status').textContent=authStatus;
  const missingModels=[!data.chatgpt_explain_model?'推荐解读':null,!data.chatgpt_read_model?'单篇精读':null].filter(Boolean);
  $('agent-model-readiness').textContent=missingModels.length
    ? `尚未保存${missingModels.join('、')}模型。读取模型列表并选择后，点击“保存模型选择”。`
    : `已保存：推荐解读 ${data.chatgpt_explain_model} · 单篇精读 ${data.chatgpt_read_model}`;
  if(!chat.pending)$('agent-auth-link').hidden=true;
  $('agent-remote-auth').hidden=!chat.pending;
  $('agent-complete-login').disabled=!chat.pending||agentBusy;
  if(!chat.pending)$('agent-callback-url').value='';
  $('agent-models').disabled=!chat.sharing||agentBusy;
  $('agent-relogin').disabled=!select.value||agentBusy;
  $('agent-logout').disabled=(!select.value&&!chat.pending)||agentBusy;
  if(data.last_test)$('agent-test-result').textContent=`上次测试成功 · ${data.last_test.mode==='api'?'API':'ChatGPT 套餐'} · ${data.last_test.model} · ${data.last_test.reply}`;
}
function renderAgentModels(){
  for(const task of ['explain','read']){
    const select=$('agent-'+task+'-model'), old=select.value||agentState?.['chatgpt_'+task+'_model'];
    select.replaceChildren(new Option(agentModels.length?'选择模型':'请先读取模型列表',''));
    for(const model of agentModels)select.add(new Option(model.name,model.id));
    if(old&&!agentModels.length){select.add(new Option(`${old}（已保存，待读取列表）`,old));select.value=old;}
    else if(agentModels.some(m=>m.id===old))select.value=old;
  }
}
async function loadAgent(){
  clearTimeout(agentPoll);const run=++agentRun;
  try{const data=await api('/api/agent');if(run!==agentRun)return;agentState=data;renderAgent();
    if(data.chatgpt.pending)agentPoll=setTimeout(()=>{if(!$('agent-view').hidden)loadAgent();},2000);
  }catch(e){agentMessage(e.message);}
}
async function agentAction(action){
  if(agentBusy)return;agentBusy=true;
  document.querySelectorAll('#agent-view button').forEach(b=>b.disabled=true);
  try{await action();}catch(e){agentMessage(e.message);}finally{agentBusy=false;document.querySelectorAll('#agent-view button').forEach(b=>b.disabled=false);if(agentState)renderAgent();}
}
document.querySelectorAll('input[name="agent-mode"]').forEach(r=>r.onchange=()=>{agentChosenMode=r.value;agentPanels();});
$('agent-reload').onclick=()=>agentAction(async()=>{agentState=await api('/api/agent/reload',{});$('agent-test-result').textContent='';agentMessage('已重新读取 .env。系统环境变量优先于文件配置。');});
$('agent-save').onclick=()=>agentAction(async()=>{
  agentState=await api('/api/agent/settings',{mode:chosenAgentMode(),chatgpt_explain_model:$('agent-explain-model').value,chatgpt_read_model:$('agent-read-model').value});agentChosenMode=null;$('agent-test-result').textContent='';agentMessage('通道设置已保存在本机。不会自动切换计费通道。');
});
$('agent-save-models').onclick=$('agent-save').onclick;
async function beginAgentLogin(profileId){
  const data=await api('/api/agent/chatgpt/start',{profile_id:profileId||null});
  const link=$('agent-auth-link');link.href=data.authorization_url;link.hidden=false;
  $('agent-callback-url').value='';
  $('agent-callback-status').textContent='';
  agentMessage('请点击“前往 ChatGPT 官方授权页面”完成登录。此操作不会读取你的 ChatGPT 历史对话。');
  await loadAgent();
}
$('agent-login').onclick=()=>agentAction(()=>beginAgentLogin(null));
$('agent-remote-auth').onsubmit=event=>{
  event.preventDefault();
  if(agentBusy)return;
  const callbackUrl=$('agent-callback-url').value.trim();
  if(!callbackUrl)return;
  clearTimeout(agentPoll);
  $('agent-callback-url').value='';
  $('agent-callback-status').textContent='正在验证授权，请稍候…';
  agentMessage('正在验证 ChatGPT 授权，请稍候…');
  agentAction(async()=>{
    try{
      await api('/api/agent/chatgpt/complete',{callback_url:callbackUrl});
    }catch(error){
      $('agent-callback-status').textContent=error.message;
      await loadAgent();
      throw error;
    }
    await loadAgent();
    agentMessage('ChatGPT 授权已完成，可以读取模型列表并保存模型选择。');
  });
};
$('agent-relogin').onclick=()=>agentAction(()=>beginAgentLogin($('agent-profiles').value));
$('agent-profiles').onchange=()=>agentAction(async()=>{if(!$('agent-profiles').value)return;await api('/api/agent/chatgpt/select',{profile_id:$('agent-profiles').value});await loadAgent();});
$('agent-logout').onclick=()=>agentAction(async()=>{const data=await api('/api/agent/chatgpt/disconnect',{profile_id:$('agent-profiles').value||null});agentMessage(data.message);agentModels=[];renderAgentModels();await loadAgent();});
$('agent-models').onclick=()=>agentAction(async()=>{agentModels=(await api('/api/agent/chatgpt/models',{})).models;renderAgentModels();agentMessage('模型列表已更新。选择两种任务的模型后保存当前通道。');});
$('agent-test').onclick=()=>agentAction(async()=>{
  if(chosenAgentMode()!==agentState.mode)throw new Error('请先保存当前通道，再发送连接测试。');
  agentState.last_test=null;$('agent-test-result').textContent='正在发送固定短文本，请稍候…';
  try{const result=await api('/api/agent/test',{task:$('agent-test-task').value});agentState.last_test=result;agentMessage('连接测试成功。');}
  catch(e){$('agent-test-result').textContent='连接测试未完成。';throw e;}
});
$('nav-agent').onclick=()=>{location.hash='agent';};
function currentView(){return location.hash.startsWith('#readings')?'readings':location.hash.startsWith('#paper/')?'paper':location.hash==='#agent'?'agent':location.hash==='#seeds'?'seeds':'feed';}


let paperRun=0;
const analysisPanels=new WeakMap();
function reportView(report){
  const box=el('div','analysis-report'),meta=report.metadata;
  box.append(el('div','report-basis',meta.basis==='abstract_only'?'摘要解读 · 未阅读全文':'官方 HTML 正文精读 · 不含图片视觉核验'),el('h3','',report.title),el('p','hint',`${meta.channel==='api'?'API':'ChatGPT 套餐'} · ${meta.model} · arXiv v${meta.version} · ${new Date(meta.generated_at).toLocaleString('zh-CN')}`));
  if(meta.basis==='official_html_text')box.append(el('p','hint',`已处理 ${meta.source_blocks} 个正文资料块，${meta.source_characters.toLocaleString()} 字符，${meta.chunks} 个分段。`));
  const sources=new Map(report.sources.map(s=>[s.id,s]));
  for(const section of report.sections){
    const part=el('section','report-section');part.append(el('h4','',section.heading));
    for(const claim of section.claims){
      const paragraph=el('div','report-claim');paragraph.append(el('span',`claim-kind ${claim.kind}`,claim.kind==='paper'?'作者陈述':claim.kind==='inference'?'分析推断':'证据不足'),el('p','',claim.text));
      if(claim.citations.length){const details=el('details','report-evidence');details.append(el('summary','',`查看原文依据（${claim.citations.length}）`));
        for(const citation of claim.citations){const source=sources.get(citation.source_id);if(!source)continue;const link=el('a','',`${citation.source_id} · ${source.label} ↗`);link.href=source.url;link.target='_blank';link.rel='noreferrer';details.append(link,el('blockquote','',citation.quote));}paragraph.append(details);}
      part.append(paragraph);
    }box.append(part);
  }
  box.append(el('p','hint','摘录已与输入文本核对；引用的存在不等于结论已被独立验证，请结合原文判断。'));
  return box;
}
function renderAnalysisPanel(container,info){
  container.replaceChildren();const job=info.job,running=job&&['queued','running'].includes(job.status),label=info.kind==='explain'?'推荐解读':'单篇精读';
  if(job){
    const status=el('div','analysis-status');status.append(el('strong','',job.stage));
    if(running){const progress=el('progress');progress.max=100;progress.value=job.progress;progress.setAttribute('aria-label',`${label}进度`);status.append(progress,el('span','hint',`${job.progress}% · ${job.model}`));}
    container.append(status);
    if(job.status==='failed')container.append(el('p','analysis-error',job.error||'生成未完成，请手动重试。'));
    if(job.report){
      if(job.paper_input_hash!==info.inputHash)container.append(el('p','analysis-error','该报告基于较早的论文元数据，请按当前配置重新分析。'));
      container.append(reportView(job.report));
    }
  }else container.append(el('p','hint',info.kind==='explain'?'使用标题、摘要、相近种子和研究画像生成，不能替代全文阅读。':'按需获取官方 HTML 并生成报告；没有可用 HTML 时会说明原因。'));
  if(!job?.report&&info.savedReport){container.append(el('p','hint','上次成功生成的报告（保留供查阅）'),reportView(info.savedReport));}
  const controls=el('div','analysis-actions'),button=el('button','primary',running?'正在生成…':job?.status==='failed'?'重试':job?.report?'按当前配置分析':`生成${label}`);
  button.disabled=running;
  button.onclick=async()=>{
    button.disabled=true;button.textContent='正在提交…';
    try{info.job=await api('/api/analysis/start',{arxiv_id:info.arxivId,kind:info.kind,batch_id:info.batchId||null,retry:job?.status==='failed'});renderAnalysisPanel(container,info);pollAnalysis(container,info);}
    catch(e){container.append(el('p','analysis-error',e.message));button.disabled=false;button.textContent=`生成${label}`;}
  };
  const settings=el('a','', 'Agent 设置 ↗');settings.href='/#agent';controls.append(button,settings);if(info.kind==='read'){const records=el('a','','查看精读记录 →');records.href='#readings';controls.append(records);}container.append(controls);
  container.append(el('p','hint',running?'已加入精读记录，离开页面后任务继续，可稍后查看。':'生成会消耗所选模型通道的用量；相同输入与模型复用缓存。报告内容是模型分析。'));
}
function pollAnalysis(container,info){
  clearTimeout(info.timer);
  if(!info.job||!['queued','running'].includes(info.job.status))return;
  info.timer=setTimeout(async()=>{
    if(!container.isConnected||analysisPanels.get(container)!==info)return;
    try{info.job=await api('/api/analysis/job?'+new URLSearchParams({job_id:info.job.job_id}));renderAnalysisPanel(container,info);pollAnalysis(container,info);}
    catch(e){container.append(el('p','analysis-error',e.message+' 可重新打开此论文查看任务状态。'));}
  },1500);
}
async function loadAnalysisPanel(container,arxivId,kind,batchId=null){
  const old=analysisPanels.get(container);if(old)clearTimeout(old.timer);
  const info={arxivId,kind,batchId,job:null,inputHash:null,timer:null};analysisPanels.set(container,info);
  container.replaceChildren(el('p','hint','正在读取已有报告…'));
  try{const data=await api('/api/analysis/paper?'+new URLSearchParams({arxiv_id:arxivId}));if(analysisPanels.get(container)!==info)return;
    info.inputHash=data.paper.input_hash;
    const matching=data.jobs.filter(j=>j.kind===kind);info.job=matching[0]||null;info.savedReport=kind==='read'?data.saved_read?.report:null;
    renderAnalysisPanel(container,info);pollAnalysis(container,info);
  }catch(e){container.replaceChildren(el('p','analysis-error',e.message));}
}
async function loadPaper(){
  $('paper-back').textContent=paperFromRecords()?'← 返回精读记录':'← 返回每日推荐';
  const run=++paperRun;const panel=$('paper-analysis');const old=analysisPanels.get(panel);if(old)clearTimeout(old.timer);analysisPanels.delete(panel);$('paper-arxiv').hidden=true;$('paper-error').textContent='';$('paper-title').textContent='正在读取论文…';$('paper-meta').textContent='';$('paper-abstract-view').replaceChildren();panel.replaceChildren();
  try{const aid=decodeURIComponent(location.hash.slice('#paper/'.length).split('?')[0]);const data=await api('/api/analysis/paper?'+new URLSearchParams({arxiv_id:aid}));if(run!==paperRun)return;
    $('paper-title').textContent=data.paper.title;$('paper-meta').textContent=`${data.authors.join(', ')} · ${data.paper.published_at.slice(0,10)} · arXiv:${data.paper.arxiv_id}v${data.paper.version}`;
    $('paper-abstract-view').replaceChildren(abstractView(data.paper,false,'paper-abstract'));$('paper-arxiv').hidden=false;$('paper-arxiv').href='https://arxiv.org/abs/'+data.paper.arxiv_id;
    await loadAnalysisPanel(panel,data.paper.arxiv_id,'read');
  }catch(e){$('paper-title').textContent='论文暂不可用';$('paper-error').textContent=e.message;}
}
$('paper-back').onclick=()=>{location.hash=paperFromRecords()||'feed';};
let readingsRun=0,readingsTimer=null,readingsSearchTimer=null,readingsScroll=0;
function paperFromRecords(){const from=new URLSearchParams(location.hash.split('?')[1]||'').get('from');return from&&/^readings(?:\?|$)/.test(from)?from:null;}
function readingParams(){return new URLSearchParams(location.hash.split('?')[1]||'');}
function readingFilter(page=1){
  clearTimeout(readingsSearchTimer);
  const params=new URLSearchParams({q:$('readings-query').value,status:$('readings-status').value,favorites:String($('readings-favorites').checked),page:String(page)});
  history.replaceState(null,'','#readings?'+params);readingsScroll=0;loadReadings();
}
function bookmarkButton(item){
  const button=el('button','reading-bookmark'+(item.favorite?' selected':''));
  button.title=item.favorite?'取消收藏':'收藏';button.setAttribute('aria-label',button.title);button.setAttribute('aria-pressed',String(item.favorite));
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');
  const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d','M6 4.5A1.5 1.5 0 0 1 7.5 3h9A1.5 1.5 0 0 1 18 4.5V21l-6-4-6 4Z');svg.append(path);button.append(svg);
  button.onclick=async()=>{button.disabled=true;try{await api('/api/analysis/favorite',{arxiv_id:item.arxiv_id,favorite:!item.favorite});await loadReadings();}catch(e){$('readings-notice').textContent=e.message;button.disabled=false;}};
  return button;
}
function readingRow(item){
  const row=el('article','reading-row'),body=el('div','reading-row-body');
  const open=()=>{readingsScroll=window.scrollY;location.hash='paper/'+encodeURIComponent(item.arxiv_id)+'?'+new URLSearchParams({from:location.hash.slice(1)});};
  const title=el('a','reading-title',item.title);title.href='#paper/'+encodeURIComponent(item.arxiv_id)+'?'+new URLSearchParams({from:location.hash.slice(1)});title.onclick=()=>{readingsScroll=window.scrollY;};
  const meta=el('div','reading-meta'),labels={queued:'排队中',running:'精读中',succeeded:'已完成',failed:'失败'};
  meta.append(el('span','reading-state '+item.status,labels[item.status]),el('span','', '· '+new Date(item.updated_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',year:'numeric'})),bookmarkButton(item));
  body.append(title,meta);
  if(['queued','running'].includes(item.status)){body.append(el('p','hint',item.stage));const progress=el('progress');progress.max=100;progress.value=item.progress;progress.setAttribute('aria-label','精读进度');body.append(progress);}
  if(item.status==='failed')body.append(el('p','reading-failure',item.error||'生成未完成'));
  const button=el('button','outline',item.status==='succeeded'?'查看报告':item.status==='failed'?'查看 / 重试':'查看进度');button.onclick=open;row.append(body,button);return row;
}
async function loadReadings(restore=false){
  clearTimeout(readingsTimer);const run=++readingsRun,params=readingParams();
  $('readings-query').value=params.get('q')||'';$('readings-status').value=params.get('status')||'all';$('readings-favorites').checked=params.get('favorites')==='true';
  $('readings-notice').textContent='正在读取精读记录…';
  try{
    const data=await api('/api/analysis/records?'+params);
    if(run!==readingsRun||currentView()!=='readings')return;
    $('readings-notice').textContent='';$('readings-active-panel').hidden=!data.active.length;$('readings-active-count').textContent=data.active.length;
    $('readings-active').replaceChildren(...data.active.map(readingRow));
    $('readings-list').replaceChildren(...data.items.map(readingRow));
    if(!data.items.length)$('readings-list').append(el('p','empty',params.get('q')||params.get('favorites')==='true'||params.get('status')&&params.get('status')!=='all'?'没有符合条件的历史记录。':'还没有历史报告。从每日推荐进入论文详情，即可发起精读。'));
    $('readings-page').textContent=`共 ${data.total} 篇 · ${data.page} / ${data.pages} 页`;
    $('readings-prev').disabled=data.page<=1;$('readings-next').disabled=data.page>=data.pages;
    $('readings-prev').onclick=()=>readingFilter(data.page-1);$('readings-next').onclick=()=>readingFilter(data.page+1);
    if(String(data.page)!==(params.get('page')||'1')){params.set('page',data.page);history.replaceState(null,'','#readings?'+params);}
    if(restore)window.scrollTo(0,readingsScroll);
    readingsTimer=setTimeout(()=>loadReadings(),data.active.length?2500:15000);
  }catch(e){if(run===readingsRun&&currentView()==='readings'){$('readings-notice').textContent=e.message;const retry=el('button','outline','重新加载');retry.onclick=()=>loadReadings();$('readings-notice').append(retry);}}
}
$('nav-readings').onclick=()=>{location.hash='readings';};
$('readings-query').oninput=()=>{clearTimeout(readingsTimer);clearTimeout(readingsSearchTimer);++readingsRun;readingsSearchTimer=setTimeout(()=>{if(currentView()==='readings')readingFilter();},300);};
$('readings-status').onchange=$('readings-favorites').onchange=()=>readingFilter();
window.addEventListener('hashchange',()=>showView(currentView()));
showView(currentView());
