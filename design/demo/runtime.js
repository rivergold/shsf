/* Offline fixture adapter for the real SHSF UI. Never calls a backend. */
(() => {
  const data = JSON.parse(document.getElementById('demo-data').textContent);
  const clone = value => JSON.parse(JSON.stringify(value));
  const agent = {mode:'api',api:{base_url:'https://api.openai.com/v1',protocol:'chat_completions',api_key_configured:false,explain_model:'',read_model:'',ready:false,missing:['演示模式不连接模型服务']},chatgpt:{profiles:[],active:null,connected:false,sharing:false,pending:false,error:''}};
  const response = (value, status=200) => new Response(JSON.stringify(clone(value)), {status,headers:{'Content-Type':'application/json'}});
  const state = () => {
    const value=data.state;
    value.matched=value.items.length;value.pending=0;
    return value;
  };
  window.fetch = async (input, options={}) => {
    const url=new URL(String(input),'https://demo.invalid');
    const path=url.pathname, query=url.searchParams, body=JSON.parse(options.body||'{}');
    if(path==='/api/bootstrap')return response({state:state(),zotero:{connected:false}});
    if(path==='/api/papers'){
      const q=(query.get('q')||'').toLowerCase();
      const items=data.papers.filter(p=>(query.get('mode')==='author'?p.authors.join(' '):query.get('mode')==='identifier'?p.arxiv_id:p.title+' '+p.abstract).toLowerCase().includes(q));
      if(query.get('sort')==='recent')items.sort((a,b)=>b.published_at.localeCompare(a.published_at));
      return response({items,total:items.length,page:1,page_size:20});
    }
    if(path==='/api/seeds/add'){
      const paper=data.papers.find(p=>p.arxiv_id===body.arxiv_id);
      if(paper&&!data.state.items.some(p=>p.arxiv_id===paper.arxiv_id))data.state.items.push({...paper,seed_id:paper.arxiv_id,published_year:2026,match_status:'matched',sources:[]});
      data.state.revision++;data.state.confirmed=false;return response({state:state()});
    }
    if(path==='/api/seeds/remove'){
      data.state.items=data.state.items.filter(p=>p.seed_id!==body.seed_id);
      data.state.revision++;data.state.confirmed=false;return response({state:state()});
    }
    if(path==='/api/seeds/confirm'){data.state.confirmed=true;return response({state:state()});}
    if(path==='/api/recommendations')return response(data.feed);
    if(path==='/api/recommendations/feedback'){
      const paper=data.feed.batch.items.find(p=>p.arxiv_id===body.arxiv_id);
      if(paper){if('interest' in body)paper.feedback.interest=body.interest;if('is_read' in body)paper.feedback.is_read=body.is_read;return response(paper.feedback);}
    }
    if(path==='/api/analysis/records'){
      let items=data.records.filter(p=>p.title.toLowerCase().includes((query.get('q')||'').toLowerCase()));
      if(query.get('favorites')==='true')items=items.filter(p=>p.favorite);
      if(query.get('status')&&query.get('status')!=='all')items=items.filter(p=>p.status===query.get('status'));
      return response({active:[],items,total:items.length,page:1,pages:1,page_size:20});
    }
    if(path==='/api/analysis/favorite'){
      const record=data.records.find(p=>p.arxiv_id===body.arxiv_id);
      if(record){record.favorite=body.favorite;return response({arxiv_id:record.arxiv_id,favorite:record.favorite});}
    }
    if(path==='/api/analysis/paper'){
      const paper=data.papers.find(p=>p.arxiv_id===query.get('arxiv_id'));
      if(paper){const job={...data.job,arxiv_id:paper.arxiv_id};return response({paper:{...paper,version:1,input_hash:'demo'},authors:paper.authors,jobs:[job],saved_read:job});}
    }
    if(path==='/api/agent'||path==='/api/agent/reload')return response(agent);
    return response({error:'演示模式：此操作需要安装并运行 SHSF，本页不会连接模型、账户或数据库。'},400);
  };
  // Account and key entry are unnecessary in a public, offline demonstration.
  document.querySelectorAll('input[type="password"],#zotero-user').forEach(input=>{input.disabled=true;input.placeholder='演示页面无需填写凭据';});
  document.addEventListener('submit',event=>{
    if(['connect-form','agent-remote-auth'].includes(event.target.id)){event.preventDefault();event.stopImmediatePropagation();}
  },true);
  document.addEventListener('click',event=>{
    const link=event.target.closest('a');
    if(link?.href.includes('arxiv.org/')){event.preventDefault();document.getElementById('demo-message').textContent='论文为虚构示例，未关联真实 arXiv 页面。';}
  });
})();
