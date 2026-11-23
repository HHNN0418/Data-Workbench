// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {act, cleanup, configure, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter} from 'react-router-dom';
import Agents, {AgentPerformance, DraftCard, ExperienceMemory, ObservationSummary} from './Agents';

vi.mock('antd',async importOriginal=>{
 const original=await importOriginal<typeof import('antd')>();
 return {...original,Select:({options,value,onChange,id,disabled,mode,'aria-label':label}:any)=><select id={id} aria-label={label} multiple={mode==='multiple'} disabled={disabled} value={value??(mode==='multiple'?[]:'')} onChange={event=>onChange(mode==='multiple'?Array.from(event.target.selectedOptions,option=>option.value):event.target.value)}>{(options||[]).map((option:any)=><option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}</select>};
});

describe('agent account feedback',()=>{
 const agent=()=>({...role,interests:role.interests.join('\n'),allowed_actions:undefined});
 it('shows binding as the prerequisite and does not present fabricated account results',()=>{
  render(<MemoryRouter><AgentPerformance agent={{...agent(),account_id:null,feedback:{account_id:99,metrics:{like_count:999},known_post_count:100}}}/></MemoryRouter>);
  expect(screen.getByText('待绑定账号，连接后可查看这个角色的帖子和互动反馈。')).toBeTruthy();
  expect(screen.queryByText('999')).toBeNull();expect(screen.queryByRole('link')).toBeNull();
 });
 it('links the actual bound account across records and analysis while preserving missing interaction samples',()=>{
  render(<MemoryRouter><AgentPerformance agent={{...agent(),feedback:{account_id:12,status:'observed',known_post_count:4,confirmed_action_count:2,window:{post_count:4,sampled_posts:4},metrics:{like_count:null,reply_count:0,repost_count:null,view_count:12},metric_coverage:{like_count:0,reply_count:4,repost_count:0,view_count:4},latest_sampled_at:'2026-10-20T02:00:00Z',score_estimate:{status:'unknown',is_official:false,points_decimal:null},official:{status:'unattributed',points_decimal:null}}}}/></MemoryRouter>);
  expect(screen.getByRole('link',{name:'账号 #12 ↗'}).getAttribute('href')).toBe('/accounts?q=12');
  expect(screen.getByRole('link',{name:'账号帖子'}).getAttribute('href')).toBe('/posts?feed=all&author_id=12');
  expect(screen.getByRole('link',{name:'账号分析'}).getAttribute('href')).toBe('/analysis?author_id=12&analysis_tab=cohorts');
  expect(screen.getByRole('link',{name:'窗口增长'}).getAttribute('href')).toBe('/analysis?author_id=12&analysis_tab=growth');
  expect(screen.getAllByText('待采集')).toHaveLength(2);expect(screen.getByText('0')).toBeTruthy();
  expect(screen.getByText('暂无法估算')).toBeTruthy();expect(screen.getByText('暂无可归因记录')).toBeTruthy();
 });
 it('keeps liked targets separate from role performance and labels team score evidence explicitly',async()=>{
  render(<MemoryRouter><AgentPerformance agent={{...agent(),feedback:{account_id:12,known_post_count:1,confirmed_action_count:2,window:{post_count:1,sampled_posts:1},recent_posts:[{post_id:901,action:'reply',body:'这条是角色实际发布的回复。',latest_counts:{reply_count:2},sampled_at:'2026-10-20T02:00:00Z'}],actions:[{draft_id:'like-done',action:'like',status:'published',post_id:999,target_post_id:999,performance_scope:'target_context',performance:{post_id:999,latest_counts:{like_count:777}}},{draft_id:'not-done',action:'post',status:'pending',post_id:902}],official:{status:'unattributed',points_decimal:null,reason:'官方流水暂未提供角色归属。',team_snapshot:{total_decimal:'123.5',source:'manual',observed_at:'2026-10-20T02:00:00Z'}}}}}/></MemoryRouter>);
  fireEvent.click(screen.getByText('查看发布记录与反馈依据'));
  expect(await screen.findByText('这条是角色实际发布的回复。')).toBeTruthy();
  expect(screen.getByRole('link',{name:'原帖 #901 ↗'}).getAttribute('href')).toBe('/posts?post=901');
  expect(screen.getByRole('link',{name:'分析这条帖子 ↗'}).getAttribute('href')).toBe('/analysis?author_id=12&analysis_tab=deep&focus_post_id=901');
  expect(screen.getByText('查看互动对象的反馈，不计入这个角色的发布表现。')).toBeTruthy();
  expect(screen.queryByText('777')).toBeNull();expect(screen.queryByRole('link',{name:'发布记录 #902 ↗'})).toBeNull();
  expect(screen.queryByRole('link',{name:'发布记录 #999 ↗'})).toBeNull();expect(screen.getByRole('link',{name:'互动对象 #999 ↗'})).toBeTruthy();
  expect(screen.getByText('全队官方快照：123.5 分',{exact:false})).toBeTruthy();
  expect(screen.getByText('此总分属于全队。',{exact:false})).toBeTruthy();
  expect(screen.getByRole('link',{name:'查看本队官方积分与来源 ↗'}).getAttribute('href')).toBe('/score');
 });
});

it('invalidates displayed review conclusions when the draft is edited while preserving emoji',()=>{
 const props={agent:{...role,interests:role.interests.join('\n'),allowed_actions:undefined},busy:false,onPublish:vi.fn().mockResolvedValue(true),onDiscard:vi.fn().mockResolvedValue(true),onReconcile:vi.fn().mockResolvedValue(true)};
 render(<MemoryRouter><DraftCard {...props} draft={{id:'reviewed',agent_id:role.id,body:'Thanks for sharing 🙂',action:'post',status:'draft',review_summary:'英文符合，条款复核完成。',review:{status:'pass',review_kind:'ai',language:{status:'pass'},findings:[{rule_id:'C20',status:'pass'}]}}}/></MemoryRouter>);
 const review=within(screen.getByLabelText('草稿发布前审核'));expect(review.getByText('语言：英文符合')).toBeTruthy();expect(review.getByText('条款：通过')).toBeTruthy();expect(review.getByText('执行：待审阅')).toBeTruthy();
 fireEvent.change(screen.getByRole('textbox'),{target:{value:'Thanks for sharing. 谢谢 🙂'}});
 expect(review.getByText('语言：待重新检查')).toBeTruthy();expect(review.getByText('条款：原审核已失效')).toBeTruthy();expect(review.queryByText('英文符合，条款复核完成。')).toBeNull();expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toContain('🙂');
});

describe('agent experience memory',()=>{
 const agent=()=>({...role,interests:role.interests.join('\n'),allowed_actions:undefined});
 it('shows automatic long term thoughts with evidence even when the manual note is empty',()=>{
  render(<MemoryRouter><ExperienceMemory agent={{...agent(),memory:'',memory_view:{long_term:{total:231,latest_at:'2026-10-21T00:10:00Z'},subjective_notes:[{id:'viewpoints:v1',kind:'viewpoints',title:'主观看法',excerpt:'交通政策需要照顾夜班居民。',source_ids:['post:701'],at:'2026-10-21T00:10:00Z'},{id:'questions:q1',kind:'questions',excerpt:'夜班公交如何保障换乘？',source_ids:['post:702']},{id:'account:91',kind:'people',excerpt:'这位居民的反馈具体，还想继续了解。',source_ids:['post:703'],author_id:91},{id:'audit',title:'一次决定',excerpt:'我准备回复夜班的问题。'}]}}}/></MemoryRouter>);
  expect(screen.getByText('长期观点 · 1 条')).toBeTruthy();expect(screen.getByText('持续关注的问题 · 1 条')).toBeTruthy();expect(screen.getByText('对人的印象 · 1 条')).toBeTruthy();
  expect(screen.getByText('交通政策需要照顾夜班居民。')).toBeTruthy();expect(screen.getByText('夜班公交如何保障换乘？')).toBeTruthy();expect(screen.getByText('这位居民的反馈具体，还想继续了解。')).toBeTruthy();
  expect(screen.getByRole('link',{name:'帖子 #701 ↗'}).getAttribute('href')).toBe('/posts?post=701');
  expect(screen.getByText('已保存 231 条回忆',{exact:false})).toBeTruthy();expect(screen.getByRole('button',{name:'查找历史回忆'})).toBeTruthy();
  expect(screen.getByText('当时的想法 · 1 条')).toBeTruthy();expect(screen.queryByText('我准备回复夜班的问题。')).toBeNull();
 });
 it('keeps completed actions separate from review intentions, failed attempts and uncertain results',async()=>{
  render(<MemoryRouter><ExperienceMemory agent={{...agent(),memory_view:{observed_reading:[{id:'read',source_id:'post:701',post_id:701,title:'公交与社区讨论',excerpt:'居民担心班次减少。'}],completed_actions:[{id:'done',action:'reply',state:'published',body:'已经确认的回复正文',post_id:702,target_post_id:701},{id:'bad',action:'post',state:'failed',body:'未发布的失败正文'}],pending_intentions:[{id:'plan',action:'repost',state:'pending',body:'仍在审阅的转发附言',target_post_id:703}],uncertain_actions:[{id:'unknown',action:'post',state:'uncertain',body:'远端结果尚未确认'}]}}}/></MemoryRouter>);
  const completed=screen.getByText('实际做过的事 · 1 条').closest('.ant-collapse-item') as HTMLElement;
  expect(within(completed).getByText('已经确认的回复正文')).toBeTruthy();
  expect(within(completed).queryByText('未发布的失败正文')).toBeNull();
  expect(screen.getByRole('link',{name:'回看原帖 #701 ↗'}).getAttribute('href')).toBe('/posts?post=701');
  expect(within(completed).getByRole('link',{name:'查看已发布帖子 #702 ↗'}).getAttribute('href')).toBe('/posts?post=702');
  fireEvent.click(screen.getByText('还没完成的打算 · 2 条'));
  expect(await screen.findByText('待审阅，尚未执行')).toBeTruthy();expect(screen.getByText('执行失败')).toBeTruthy();
  expect(screen.getByText('未发布的失败正文')).toBeTruthy();
  fireEvent.click(screen.getByText('结果还没确认的操作 · 1 条'));
  expect(await screen.findByText('结果待核对')).toBeTruthy();
  expect(screen.queryByRole('link',{name:'查看已发布帖子 #703 ↗'})).toBeNull();
 });
 it('shows older reading and activity records without treating an unconfirmed decision as an accomplishment',()=>{
  render(<MemoryRouter><ExperienceMemory agent={{...agent(),last_sources:[{id:'post:801',title:'旧版阅读资料',text:'记得之前读过的内容。'}],activity:[{id:'done',action:'like',state:'published',target_post_id:801,reason:'曾经认可这个观点。'},{id:'unknown',action:'post',reason:'只有一次决策，没有发布结果。'},{id:'plan',action:'reply',state:'pending',body:'仍未发出的回复'}]}}/></MemoryRouter>);
  expect(screen.getByText('记得之前读过的内容。')).toBeTruthy();
  expect(screen.getByText('实际做过的事 · 1 条')).toBeTruthy();
  expect(screen.getByText('还没完成的打算 · 1 条')).toBeTruthy();
  expect(screen.queryByText('只有一次决策，没有发布结果。')).toBeNull();
  expect(screen.getByRole('link',{name:'回看原帖 #801 ↗'}).getAttribute('href')).toBe('/posts?post=801');
 });
 it('bounds reading detail and renders relationship evidence and subjective thoughts as readable prose',async()=>{
  render(<MemoryRouter><ExperienceMemory agent={{...agent(),memory_view:{observed_reading:Array.from({length:10},(_,index)=>({id:String(index),title:`已读新闻 ${index+1}`,excerpt:`新闻摘要 ${index+1}`,source_id:`news:${index}`})),relationships:[{author_id:91,title:'与账号 91 的往来',excerpt:'读过对方两条帖子，核实过一次回复。'}],subjective_notes:'我更关注交通变化怎样影响老年居民，还需要核对预算。'}}}/></MemoryRouter>);
  expect(screen.getByText('新闻摘要 8')).toBeTruthy();expect(screen.queryByText('新闻摘要 9')).toBeNull();
  expect(screen.getByText('此处展示最近 8 条，共 10 条。')).toBeTruthy();
  fireEvent.click(screen.getByText('交流记录与关系线索 · 1 条'));
  expect(await screen.findByText('读过对方两条帖子，核实过一次回复。')).toBeTruthy();
  expect(screen.getByRole('link',{name:'查看账号 #91 ↗'}).getAttribute('href')).toBe('/accounts?q=91');
  fireEvent.click(screen.getByText('当时的想法 · 1 条'));
  expect(await screen.findByText('我更关注交通变化怎样影响老年居民，还需要核对预算。')).toBeTruthy();
  expect(screen.getByText('角色当时的理解，会随新资料修正。')).toBeTruthy();
 });
});
describe('agent reading selection evidence',()=>{
 const agent=()=>({...role,interests:role.interests.join('\n'),allowed_actions:undefined});
 it('separates preview candidates, AI selections and final reading sources with thread context',()=>{
  render(<ObservationSummary agent={{...agent(),observation:{source_count:5,new_source_count:5,reading_selection:{status:'selected',cache_hit:false,candidate_count:12,selected_count:3,reason:'选取与社区交通有关的资料。'},thread_context:{requested_count:3,included_count:3,background_count:2,missing_count:0,deferred_count:0}}}}/>);
  expect(screen.getByText('AI 已筛选资料')).toBeTruthy();expect(screen.getByText('预览候选 12 条')).toBeTruthy();expect(screen.getByText('筛选选中 3 条')).toBeTruthy();expect(screen.getByText('深读来源 5 条')).toBeTruthy();
  expect(screen.getByLabelText('对话上下文记录').textContent).toBe('阅读打包：纳入目标 3 条 · 补充背景 2 条 · 缺失上下文 0 处 · 延后目标 0 条');
  expect(screen.getByText('筛选说明：选取与社区交通有关的资料。')).toBeTruthy();expect(screen.queryByText('复用筛选结果')).toBeNull();expect(screen.queryByText(/已读.*12/)).toBeNull();
 });
 it('shows an empty cached AI choice without treating all previewed candidates as read',()=>{
  render(<ObservationSummary agent={{...agent(),observation:{source_count:0,reading_selection:{status:'empty',cache_hit:true,candidate_count:9,selected_count:0,reason:'本轮没有想继续了解的内容。'}}}}/>);
  expect(screen.getByText('AI 本轮未选中资料')).toBeTruthy();expect(screen.getByText('复用筛选结果')).toBeTruthy();expect(screen.getByText('预览候选 9 条')).toBeTruthy();expect(screen.getByText('深读来源 0 条')).toBeTruthy();expect(screen.getByText('本轮未选中深读资料。')).toBeTruthy();expect(screen.queryByText('深读来源 9 条')).toBeNull();
 });
 it('distinguishes a failed selection fallback and reports missing or deferred conversation context',()=>{
  render(<ObservationSummary agent={{...agent(),observation:{source_count:4,reading_selection:{status:'fallback',cache_hit:false,candidate_count:15,selected_count:2,error:'筛选服务暂未完成，使用已有资料。'},thread_context:{included_count:2,missing_count:1,deferred_count:1}}}}/>);
  expect(screen.getByText('筛选失败，已回退')).toBeTruthy();expect(screen.getByText('回退选用 2 条')).toBeTruthy();expect(screen.getByText('深读来源 4 条')).toBeTruthy();expect(screen.getByText('筛选服务暂未完成，使用已有资料。')).toBeTruthy();expect(screen.getByText('阅读打包：纳入目标 2 条 · 缺失上下文 1 处 · 延后目标 1 条')).toBeTruthy();expect(screen.queryByText('AI 已筛选资料')).toBeNull();
 });
 it('keeps older structured selector errors from becoming invalid React children',()=>{
  render(<ObservationSummary agent={{...agent(),observation:{source_count:1,reading_selection:{status:'fallback',candidate_count:2,selected_count:1,error:{kind:'selector_service_error',status:502}}}}}/>);
  expect(screen.getByText('筛选失败，已回退')).toBeTruthy();expect(screen.getByText('深读来源 1 条')).toBeTruthy();expect(screen.queryByText('[object Object]')).toBeNull();
 });
 it('keeps legacy observations readable and does not invent missing selection metadata',()=>{
  const view=render(<ObservationSummary agent={{...agent(),observation:{source_count:8,new_source_count:2,notifications_status:'cached'}}}/>);
  expect(screen.getByText('阅读资料 8 条 · 本轮新增 2 条')).toBeTruthy();expect(screen.queryByLabelText('资料筛选记录')).toBeNull();expect(screen.queryByLabelText('对话上下文记录')).toBeNull();
  view.rerender(<ObservationSummary agent={{...agent(),observation:{reading_selection:{candidate_count:5}}}}/>);
  expect(screen.getByText('预览候选 5 条')).toBeTruthy();expect(screen.queryByText('AI 已筛选资料')).toBeNull();expect(screen.queryByText('深读来源 0 条')).toBeNull();
 });
 it('distinguishes no available candidates from an AI decision that selected none',()=>{
  render(<ObservationSummary agent={{...agent(),observation:{source_count:0,reading_selection:{status:'empty',candidate_count:0,selected_count:0}}}}/>);
  expect(screen.getByText('本轮暂无候选资料')).toBeTruthy();expect(screen.getByText('本轮没有可供深读的候选资料。')).toBeTruthy();expect(screen.queryByText('AI 本轮未选中资料')).toBeNull();
 });
 it('labels real parent or root sources as background and preserves explicit missing context',async()=>{
  render(<MemoryRouter><DraftCard draft={{id:'thread-reading',agent_id:role.id,action:'reply',body:'拟议回复',status:'draft',sources:[{id:'post:7',post_id:7,title:'目标资料',origin:'notification',text:'目标正文',context_only:false,thread_context:{parent_post_id:5,root_post_id:3,complete:false,missing:[{post_id:3,role:'root',reason:'missing'}]}},{id:'post:5',post_id:5,title:'真实父帖',origin:'thread',text:'实际读取的父帖正文',context_only:true,context_role:'background'}]}} agent={agent()} busy={false} onPublish={vi.fn().mockResolvedValue(true)} onDiscard={vi.fn().mockResolvedValue(true)} onReconcile={vi.fn().mockResolvedValue(true)}/></MemoryRouter>);
  fireEvent.click(screen.getByText('阅读来源 · 2 条'));expect(await screen.findByText('父帖或根帖背景')).toBeTruthy();expect(screen.getByText('本条资料尚缺 1 条父帖或根帖上下文。')).toBeTruthy();expect(screen.getByText('实际读取的父帖正文')).toBeTruthy();expect(screen.getByRole('link',{name:'原帖 #5 ↗'})).toBeTruthy();
 });
});
beforeAll(()=>{
 configure({asyncUtilTimeout:10000});
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 const originalStyle=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>originalStyle(element));
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn().mockImplementation(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;sessionStorage.clear();vi.unstubAllGlobals();});
const role={id:'role-1',name:'林青',username:'lin_qing',background:'在社区工作了十年的图书管理员',stance:'支持 Vote Yes',voice:'结合日常观察，简洁表达',interests:['教育','社区'],language:'中文',provider_id:'provider',enabled:false,mode:'review' as const,daily_limit:6,interval_seconds:1800,account_id:12,account_state:'bound',memory:'记得上次讨论过的教育预算',version:3,allowed_actions:['post','reply','repost','like','skip'],freshness_minutes:90,news_freshness_hours:36,notification_interval_seconds:120};
const initial={settings:{enabled:false,team_side:'Vote Yes — 社区',max_accounts:23,global_interval_seconds:45},agents:[role],drafts:[],events:[],has_team_code:true,sdk_available:true};
const generatedPersona={name:'沈知夏',username:'shen_zhixia',background:'成长与家庭\n在海边小城长大，母亲常带她到社区图书馆。\n\n关键转折\n夜班护士的经历使她开始关注普通家庭的照护负担。\n\n现在的生活\n经营一家花店，对社区互助充满期待，也担心支持政策会给小商户增加负担。',stance:'支持改善公共照护，也会追问资金怎样用到普通家庭身上。',voice:'温和具体，常用夜班和花店里的小事解释自己的想法。',interests:['社区照护','小商户与公共服务'],language:'中文'};
function setup(overrides:Record<string,unknown>={},optionsForGeneration:{generate?:(payload:Record<string,unknown>)=>Response|Promise<Response>;providers?:unknown;action?:(path:string,payload:Record<string,unknown>)=>Response|Promise<Response>|undefined;receipts?:boolean;memory?:unknown}={}){
 let state={...initial,...overrides};
 const jobs=new Map<string,Record<string,unknown>>();
 const json=(data:unknown,status=200)=>Promise.resolve(new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}}));
 const fetch=vi.fn((input:RequestInfo|URL,options?:RequestInit)=>{
  const path=String(input);
  if(options?.body&&optionsForGeneration.action){const response=optionsForGeneration.action(path,JSON.parse(String(options.body)));if(response)return response;}
  if(path==='/api/agents/generate-persona'&&options?.body){
   const id=`persona-job-${jobs.size+1}`,headers=new Headers(options.headers);
   const job:Record<string,unknown>={id,kind:'persona',scope:decodeURIComponent(headers.get('X-CTN-Job-Scope')||''),request_key:headers.get('X-CTN-Job-Key'),state:optionsForGeneration.generate?'running':'completed',stage:optionsForGeneration.generate?'thinking':'complete',elapsed_seconds:optionsForGeneration.generate?185:0,result:optionsForGeneration.generate?null:{persona:generatedPersona,provider_id:'provider'}};
   jobs.set(id,job);
   if(optionsForGeneration.generate)void Promise.resolve(optionsForGeneration.generate(JSON.parse(String(options.body)))).then(async response=>{
    const result=await response.json();
    if(job.state==='cancelled')return;
    Object.assign(job,response.ok?{state:'completed',stage:'complete',result}:{state:'failed',error:result.error||'AI 任务未完成',result:null});
   });
   return json(job,202);
  }
  if(path.startsWith('/api/ai/jobs?')){
   const params=new URL(path,'http://localhost').searchParams;
   return json({items:[...jobs.values()].filter(job=>job.kind===params.get('kind')&&(!params.has('scope')||job.scope===params.get('scope')))});
  }
  if(path.startsWith('/api/ai/jobs/')){
   const id=path.split('/')[4],job=jobs.get(id);
   if(options?.method==='POST'&&path.endsWith('/cancel')&&job)Object.assign(job,{state:'cancelled',result:null});
   return json(job||{error:'任务不存在'},job?200:404);
  }
  if(path==='/api/agents/reset-memory'&&options?.body){const payload=JSON.parse(String(options.body));state={...state,agents:state.agents.map(agent=>agent.id===payload.agent_id?{...agent,memory:'',last_sources:[]}:agent)};}
  if(path==='/api/agents/run'&&options?.body){const payload=JSON.parse(String(options.body));state={...state,agents:state.agents.map(agent=>agent.id===payload.agent_id?{...agent,generation:{id:'job-1',status:'queued',elapsed_seconds:0}}:agent)};}
  if(path==='/api/agents/cancel-generation'&&options?.body){const payload=JSON.parse(String(options.body));state={...state,agents:state.agents.map(agent=>agent.id===payload.agent_id?{...agent,generation:{id:'job-1',status:'cancelled'}}:agent)};}
  if(path==='/api/agents/settings'&&options?.body)state={...state,settings:{...state.settings,...JSON.parse(String(options.body))}};
  if(optionsForGeneration.memory&&/^\/api\/agents\/[^/]+\/memory$/.test(path))return json(optionsForGeneration.memory);
  const data=path==='/api/bootstrap'?{csrf_token:'test'}:path==='/api/ai/providers'?optionsForGeneration.providers??{providers:[{id:'provider',name:'角色服务',model:'test-model',protocol:'openai_chat',enabled:true}]}:options?.body&&optionsForGeneration.receipts?{ok:true}:state;
  return Promise.resolve(new Response(JSON.stringify(data),{status:path==='/api/agents/run'&&options?.body?202:200,headers:{'Content-Type':'application/json'}}));
 });
 vi.stubGlobal('fetch',fetch);
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 render(<QueryClientProvider client={client}><MemoryRouter><Agents/></MemoryRouter></QueryClientProvider>);
 return {fetch,client};
}
function posted(fetch:ReturnType<typeof vi.fn>,path:string){return fetch.mock.calls.filter(([url,options])=>url===path&&options?.body).map(([,options])=>JSON.parse(String(options!.body)));}

describe('agent memory reset and background decisions',()=>{
 it('shows only the current generation error after hiding an older error during a new attempt',async()=>{
  const current={...role,error:'旧一轮额度错误。',busy:true,busy_reason:'generation',generation:{id:'new-job',status:'running',stage:'thinking',elapsed_seconds:3}};
  const {client}=setup({agents:[current]});await screen.findByText('林青');
  expect(screen.queryByText('旧一轮额度错误。')).toBeNull();expect(screen.getByText('正在思考并组织回复。 已等待 3 秒。')).toBeTruthy();
  await act(async()=>{client.setQueryData(['/agents',{}],{...initial,agents:[{...current,busy:false,status:'running',generation:{id:'new-job',status:'failed',error:'本轮连接失败。'}}],running:[role.id]});});
  expect(await screen.findByText('本轮决策失败')).toBeTruthy();expect(screen.getAllByText('本轮连接失败。')).toHaveLength(1);expect(screen.queryByText('旧一轮额度错误。')).toBeNull();
  expect((screen.getByRole('button',{name:'阅读并决策'}) as HTMLButtonElement).disabled).toBe(false);
 },30000);
 it('waits for lease recovery without a thinking spinner and restores controls only after the backend releases busy',async()=>{
  const current={...role,busy:true,busy_reason:'lease_recovery',busy_until:1792490400,generation:{id:'failed-job',status:'failed',stage:'thinking',error:'这轮任务已经失败。'}};
  const {client}=setup({agents:[current]});await screen.findByText('林青');
  expect(screen.getByText('预计释放时间：',{exact:false})).toBeTruthy();expect(screen.getAllByText('等待旧任务释放').length).toBeGreaterThan(0);expect(screen.queryByText('正在思考并组织回复。',{exact:false})).toBeNull();
  const run=screen.getByRole('button',{name:'阅读并决策'}) as HTMLButtonElement;expect(run.disabled).toBe(true);expect(run.classList.contains('ant-btn-loading')).toBe(false);
  expect(screen.getByText('本轮决策失败')).toBeTruthy();
  await act(async()=>{client.setQueryData(['/agents',{}],{...initial,agents:[{...current,busy:false,busy_reason:undefined,status:'running'}],running:[role.id]});});
  await waitFor(()=>expect((screen.getByRole('button',{name:'阅读并决策'}) as HTMLButtonElement).disabled).toBe(false));expect(screen.queryByText('等待旧任务释放')).toBeNull();
 },30000);
 it('blocks new operations during maintenance while leaving local pages and policy settings readable',async()=>{
  const {fetch}=setup({maintenance:{active:true,reason:'正在修复旧任务占用。'}});await screen.findByText('林青');
  expect(screen.getByText('系统维护中')).toBeTruthy();expect(screen.getByText('正在修复旧任务占用。',{exact:false})).toBeTruthy();
  for(const name of ['新建角色','只阅读','阅读并决策'])expect((screen.getByRole('button',{name}) as HTMLButtonElement).disabled).toBe(true);
  const continuous=screen.getByRole('switch',{name:'持续运行所有已启用角色'}) as HTMLButtonElement;expect(continuous.disabled).toBe(true);fireEvent.click(continuous);
  fireEvent.click(screen.getByRole('tab',{name:'队伍设置'}));expect(await screen.findByRole('switch',{name:'禁止 Agent 内部互动'})).toBeTruthy();expect((screen.getByRole('button',{name:'保存队伍设置'}) as HTMLButtonElement).disabled).toBe(true);
  expect(posted(fetch,'/api/agents/settings')).toHaveLength(0);expect(posted(fetch,'/api/agents/run')).toHaveLength(0);
 },30000);
 it('confirms memory reset separately and retains unsaved persona edits',async()=>{
  const {fetch,client}=setup();await screen.findByText('林青');fireEvent.click(screen.getByRole('button',{name:'编辑角色'}));
  const historyKey=['/agents/memory-history',{agent_id:role.id,q:'',kind:'all',limit:20,offset:0}],otherHistoryKey=['/agents/memory-history',{agent_id:'another-role'}];
  client.setQueryData(historyKey,{items:[{id:'old-memory'}]});client.setQueryData(otherHistoryKey,{items:[{id:'other-memory'}]});
  const drawer=await screen.findByRole('dialog'),background=within(drawer).getByLabelText('身份与历史背景');
  fireEvent.change(background,{target:{value:'尚未保存的人设修改。'}});fireEvent.click(within(drawer).getByRole('button',{name:'清空记忆与阅读历史'}));
  expect(posted(fetch,'/api/agents/reset-memory')).toHaveLength(0);fireEvent.click(within(drawer).getByRole('button',{name:'取消清空'}));
  expect((within(drawer).getByLabelText('手动补充的长期笔记') as HTMLTextAreaElement).value).toBe(role.memory);
  fireEvent.click(within(drawer).getByRole('button',{name:'清空记忆与阅读历史'}));fireEvent.click(within(drawer).getByRole('button',{name:'确认清空'}));
  await waitFor(()=>expect((within(drawer).getByLabelText('手动补充的长期笔记') as HTMLTextAreaElement).value).toBe(''));
  expect(posted(fetch,'/api/agents/reset-memory')).toEqual([{agent_id:role.id}]);expect((background as HTMLTextAreaElement).value).toBe('尚未保存的人设修改。');
  expect(client.getQueryData(historyKey)).toBeUndefined();expect(client.getQueryData(otherHistoryKey)).toEqual({items:[{id:'other-memory'}]});
  expect(posted(fetch,'/api/agents/save')).toHaveLength(0);expect(posted(fetch,'/api/agents/memory')).toHaveLength(0);
 },90000);
 it('locks reset during the request and preserves the original memory when the server refuses cleanup',async()=>{
  let finish!:(response:Response)=>void;
  const {fetch}=setup({}, {action:(path)=>path==='/api/agents/reset-memory'?new Promise(resolve=>{finish=resolve;}):undefined});
  await screen.findByText('林青');fireEvent.click(screen.getByRole('button',{name:'编辑角色'}));const drawer=await screen.findByRole('dialog');
  fireEvent.click(within(drawer).getByRole('button',{name:'清空记忆与阅读历史'}));fireEvent.click(within(drawer).getByRole('button',{name:'确认清空'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/reset-memory')).toHaveLength(1));
  expect((within(drawer).getByRole('button',{name:/确认清空/}) as HTMLButtonElement).disabled).toBe(true);
  await act(async()=>finish(new Response(JSON.stringify({error:'记忆备份失败，未清理数据。'}),{status:500,headers:{'Content-Type':'application/json'}})));
  expect(await within(drawer).findByText('记忆备份失败，未清理数据。')).toBeTruthy();
  expect((within(drawer).getByLabelText('手动补充的长期笔记') as HTMLTextAreaElement).value).toBe(role.memory);
  expect((within(drawer).getByLabelText('身份与历史背景') as HTMLTextAreaElement).value).toBe(role.background);
 },90000);
 it('keeps an accepted background decision busy without creating partial drafts and can cancel it',async()=>{
  const {fetch}=setup();await screen.findByText('林青');const run=screen.getByRole('button',{name:'阅读并决策'});fireEvent.click(run);
  await screen.findByRole('button',{name:'取消本次决策'});expect((run as HTMLButtonElement).disabled).toBe(true);expect(run.classList.contains('ant-btn-loading')).toBe(true);
  expect(screen.getByText('任务已提交，等待开始。 已等待 0 秒。')).toBeTruthy();expect(screen.getByRole('tab',{name:'操作记录 · 0 项未完成'})).toBeTruthy();
  expect(posted(fetch,'/api/agents/run')).toEqual([{agent_id:role.id}]);fireEvent.click(screen.getByRole('button',{name:'取消本次决策'}));
  await waitFor(()=>expect((run as HTMLButtonElement).disabled).toBe(false));expect(posted(fetch,'/api/agents/cancel-generation')).toEqual([{agent_id:role.id}]);
  expect(screen.getByText('本次阅读与决策已取消。')).toBeTruthy();
 },90000);
 it('restores a running job from the server snapshot after opening the page',async()=>{
  setup({agents:[{...role,generation:{id:'existing-job',status:'running',stage:'thinking',elapsed_seconds:95}}]});await screen.findByText('林青');
  expect((screen.getByRole('button',{name:/阅读并决策/}) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText('正在思考并组织回复。 已等待 95 秒。')).toBeTruthy();expect(screen.getByRole('button',{name:'取消本次决策'})).toBeTruthy();
  expect(screen.getByText('完成后自动更新。离开或刷新页面后，仍可回来查看进度。')).toBeTruthy();
 },90000);
});

describe('AI persona generation',()=>{
 it('fills a complete editable new role from a brief and waits for explicit saving',async()=>{
  const {fetch}=setup();await screen.findByText('林青');fireEvent.click(screen.getByRole('button',{name:'新建角色'}));
  const drawer=await screen.findByRole('dialog');
  expect((within(drawer).getByLabelText('主要语言') as HTMLInputElement).value).toBe('English');
  expect((within(drawer).getByLabelText('主要语言') as HTMLInputElement).readOnly).toBe(true);
  expect((within(drawer).getByLabelText('操作方式') as HTMLSelectElement).value).toBe('auto');
  expect(within(drawer).queryByLabelText('每日操作上限')).toBeNull();
  fireEvent.change(within(drawer).getByLabelText('角色构想（可选）'),{target:{value:'做过夜班护士，后来开了社区花店。'}});
  fireEvent.click(within(drawer).getByRole('button',{name:'AI 自动生成角色'}));
  await waitFor(()=>expect((within(drawer).getByLabelText('身份与历史背景') as HTMLTextAreaElement).value).toBe(generatedPersona.background));
  expect((within(drawer).getByLabelText('主要语言') as HTMLInputElement).value).toBe('English');
  expect(posted(fetch,'/api/agents/generate-persona')[0]).toMatchObject({brief:'做过夜班护士，后来开了社区花店。',provider_id:'provider',persona:{name:'',background:'',language:'English'}});
  expect(posted(fetch,'/api/agents/save')).toHaveLength(0);
  expect((within(drawer).getByLabelText('关注的话题') as HTMLTextAreaElement).value).toBe(generatedPersona.interests.join('\n'));
  fireEvent.change(within(drawer).getByLabelText('身份与历史背景'),{target:{value:generatedPersona.background+'\n还会定期去照护中心做志愿者。'}});
  fireEvent.click(within(drawer).getByRole('button',{name:'保存角色'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/save')[0]).toMatchObject({name:generatedPersona.name,username:generatedPersona.username,background:generatedPersona.background+'\n还会定期去照护中心做志愿者。',enabled:false,mode:'auto',interval_seconds:1800}));
  expect(posted(fetch,'/api/agents/save')[0]).not.toHaveProperty('daily_limit');
 },90000);
 it('enriches existing identity from an optional brief without changing its bound username, memory or action settings',async()=>{
  const {fetch}=setup();await screen.findByText('林青');fireEvent.click(screen.getByRole('button',{name:'编辑角色'}));
  const drawer=await screen.findByRole('dialog');fireEvent.click(within(drawer).getByRole('button',{name:'AI 丰富角色背景'}));
  await waitFor(()=>expect((within(drawer).getByLabelText('身份与历史背景') as HTMLTextAreaElement).value).toBe(generatedPersona.background));
  expect(posted(fetch,'/api/agents/generate-persona')).toEqual([{brief:'',provider_id:'provider',existing_agent_id:role.id,persona:{name:role.name,username:role.username,background:role.background,stance:role.stance,voice:role.voice,interests:role.interests.join('\n'),language:'English'}}]);
  expect((within(drawer).getByLabelText('账号用户名') as HTMLInputElement).value).toBe(role.username);
  expect((within(drawer).getByLabelText('手动补充的长期笔记') as HTMLTextAreaElement).value).toBe(role.memory);
  fireEvent.click(within(drawer).getByRole('button',{name:'保存角色'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/save')[0]).toMatchObject({id:role.id,version:role.version,username:role.username,background:generatedPersona.background,enabled:role.enabled,mode:role.mode,interval_seconds:role.interval_seconds,allowed_actions:role.allowed_actions,freshness_minutes:role.freshness_minutes,notification_interval_seconds:role.notification_interval_seconds}));
  expect(posted(fetch,'/api/agents/save')[0]).not.toHaveProperty('daily_limit');
  expect(posted(fetch,'/api/agents/memory')).toHaveLength(0);expect(posted(fetch,'/api/agents/register')).toHaveLength(0);expect(posted(fetch,'/api/agents/bind')).toHaveLength(0);
 },90000);
 it('keeps hand edits made while AI is generating',async()=>{
  let finish!:(response:Response)=>void;
  const {fetch}=setup({}, {generate:()=>new Promise(resolve=>{finish=resolve;})});await screen.findByText('林青');fireEvent.click(screen.getByRole('button',{name:'编辑角色'}));
  const drawer=await screen.findByRole('dialog');fireEvent.click(within(drawer).getByRole('button',{name:'AI 丰富角色背景'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/generate-persona')).toHaveLength(1));
  fireEvent.change(within(drawer).getByLabelText('身份与历史背景'),{target:{value:'生成期间写下的个人经历。'}});
  finish(new Response(JSON.stringify({persona:generatedPersona}),{status:200,headers:{'Content-Type':'application/json'}}));
  expect(await within(drawer).findByText('生成期间你修改了角色设定，本次结果未覆盖填写内容。可按当前内容重新生成。')).toBeTruthy();
  expect((within(drawer).getByLabelText('身份与历史背景') as HTMLTextAreaElement).value).toBe('生成期间写下的个人经历。');
 },90000);
 it('shows long-running persona progress and cancels explicitly without applying a late result',async()=>{
  let finish!:(response:Response)=>void;
  const {fetch}=setup({}, {generate:()=>new Promise(resolve=>{finish=resolve;})});await screen.findByText('林青');fireEvent.click(screen.getByRole('button',{name:'编辑角色'}));
  const drawer=await screen.findByRole('dialog');fireEvent.click(within(drawer).getByRole('button',{name:'AI 丰富角色背景'}));
  expect(await within(drawer).findByText('模型正在思考')).toBeTruthy();
  expect(within(drawer).getByText(/已等待 185 秒/)).toBeTruthy();
  expect((within(drawer).getByRole('button',{name:'保存角色'}) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(within(drawer).getByRole('button',{name:'取消人设生成'}));
  await waitFor(()=>expect(posted(fetch,'/api/ai/jobs/persona-job-1/cancel')).toEqual([{}]));
  await act(async()=>finish(new Response(JSON.stringify({persona:generatedPersona}),{status:200,headers:{'Content-Type':'application/json'}})));
  expect(await within(drawer).findByText('AI 任务已取消')).toBeTruthy();
  expect((within(drawer).getByLabelText('身份与历史背景') as HTMLTextAreaElement).value).toBe(role.background);
  expect(posted(fetch,'/api/agents/save')).toHaveLength(0);
 },90000);
 it('ignores late results from a closed editor when a different role form is opened',async()=>{
  let finish!:(response:Response)=>void;
  const {fetch}=setup({}, {generate:()=>new Promise(resolve=>{finish=resolve;})});await screen.findByText('林青');fireEvent.click(screen.getByRole('button',{name:'编辑角色'}));
  const drawer=await screen.findByRole('dialog');fireEvent.click(within(drawer).getByRole('button',{name:'AI 丰富角色背景'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/generate-persona')).toHaveLength(1));fireEvent.click(within(drawer).getByRole('button',{name:'Close'}));
  fireEvent.click(screen.getByRole('button',{name:'新建角色'}));
  const nextDrawer=await screen.findByRole('dialog');fireEvent.change(within(nextDrawer).getByLabelText('角色名称'),{target:{value:'手动新建的角色'}});
  await act(async()=>{finish(new Response(JSON.stringify({persona:generatedPersona}),{status:200,headers:{'Content-Type':'application/json'}}));});
  expect((within(nextDrawer).getByLabelText('角色名称') as HTMLInputElement).value).toBe('手动新建的角色');
  expect((within(nextDrawer).getByLabelText('身份与历史背景') as HTMLTextAreaElement).value).toBe('');expect(posted(fetch,'/api/agents/save')).toHaveLength(0);
  expect(posted(fetch,'/api/ai/jobs/persona-job-1/cancel')).toHaveLength(0);
 },90000);
 it('shows generation failures without clearing identity and permits retry',async()=>{
  let attempts=0;
  setup({}, {generate:()=>new Response(JSON.stringify(++attempts===1?{error:'AI 服务暂时不可用'}:{persona:generatedPersona}),{status:attempts===1?503:200,headers:{'Content-Type':'application/json'}})});await screen.findByText('林青');fireEvent.click(screen.getByRole('button',{name:'编辑角色'}));
  const drawer=await screen.findByRole('dialog');fireEvent.click(within(drawer).getByRole('button',{name:'AI 丰富角色背景'}));
  expect(await within(drawer).findByText('AI 服务暂时不可用')).toBeTruthy();expect((within(drawer).getByLabelText('身份与历史背景') as HTMLTextAreaElement).value).toBe(role.background);
  fireEvent.click(within(drawer).getByRole('button',{name:'AI 丰富角色背景'}));
  await waitFor(()=>expect((within(drawer).getByLabelText('身份与历史背景') as HTMLTextAreaElement).value).toBe(generatedPersona.background));
 },90000);
 it('leaves manual creation available when no enabled AI service exists',async()=>{
  setup({}, {providers:{providers:[]}});await screen.findByText('林青');fireEvent.click(screen.getByRole('button',{name:'新建角色'}));
  const drawer=await screen.findByRole('dialog');expect((within(drawer).getByRole('button',{name:'AI 自动生成角色'}) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(within(drawer).getByLabelText('角色名称'),{target:{value:'自己填写'}});expect((within(drawer).getByLabelText('角色名称') as HTMLInputElement).value).toBe('自己填写');
  expect(within(drawer).getByRole('link',{name:'AI 配置'}).getAttribute('href')).toBe('/ai/settings');
 },90000);
});

describe('independent role and background AI services',()=>{
 const providers={providers:[
  {id:'provider',name:'角色服务',model:'test-model',protocol:'openai_chat',enabled:true},
  {id:'alternate-provider',name:'另一个服务',model:'alternate-model',protocol:'openai_chat',enabled:true},
 ],default_provider_id:'provider'};
 it('uses the background selection for enrichment without changing the saved role service',async()=>{
  const {fetch}=setup({}, {providers,generate:()=>new Response(JSON.stringify({persona:generatedPersona,provider_id:'alternate-provider'}),{status:200,headers:{'Content-Type':'application/json'}})});
  await screen.findByText('林青');fireEvent.click(screen.getByRole('button',{name:'编辑角色'}));
  const drawer=await screen.findByRole('dialog');
  const runtimeService=within(drawer).getByLabelText('角色决策写作 AI') as HTMLSelectElement;
  const backgroundService=within(drawer).getByLabelText('背景生成 AI') as HTMLSelectElement;
  expect(runtimeService.value).toBe('provider');
  fireEvent.change(backgroundService,{target:{value:'alternate-provider'}});
  expect(runtimeService.value).toBe('provider');expect(posted(fetch,'/api/agents/save')).toHaveLength(0);
  fireEvent.click(within(drawer).getByRole('button',{name:'AI 丰富角色背景'}));
  await waitFor(()=>expect((within(drawer).getByLabelText('身份与历史背景') as HTMLTextAreaElement).value).toBe(generatedPersona.background));
  expect(posted(fetch,'/api/agents/generate-persona')).toHaveLength(1);
  expect(posted(fetch,'/api/agents/generate-persona')[0]).toMatchObject({provider_id:'alternate-provider',existing_agent_id:role.id});
  expect(runtimeService.value).toBe('provider');expect(backgroundService.value).toBe('alternate-provider');
  expect(posted(fetch,'/api/agents/save')).toHaveLength(0);
  fireEvent.click(within(drawer).getByRole('button',{name:'保存角色'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/save')[0]).toMatchObject({id:role.id,provider_id:'provider',background:generatedPersona.background}));
 },90000);
 it('saves a changed runtime service while enrichment keeps using its separate selection',async()=>{
  const {fetch}=setup({}, {providers});
  await screen.findByText('林青');fireEvent.click(screen.getByRole('button',{name:'编辑角色'}));
  const drawer=await screen.findByRole('dialog');
  const runtimeService=within(drawer).getByLabelText('角色决策写作 AI') as HTMLSelectElement;
  const backgroundService=within(drawer).getByLabelText('背景生成 AI') as HTMLSelectElement;
  expect(backgroundService.value).toBe('provider');
  fireEvent.change(runtimeService,{target:{value:'alternate-provider'}});
  expect(backgroundService.value).toBe('provider');expect(posted(fetch,'/api/agents/generate-persona')).toHaveLength(0);
  fireEvent.click(within(drawer).getByRole('button',{name:'AI 丰富角色背景'}));
  await waitFor(()=>expect((within(drawer).getByLabelText('身份与历史背景') as HTMLTextAreaElement).value).toBe(generatedPersona.background));
  expect(posted(fetch,'/api/agents/generate-persona')[0]).toMatchObject({provider_id:'provider',existing_agent_id:role.id});
  expect(runtimeService.value).toBe('alternate-provider');expect(backgroundService.value).toBe('provider');
  fireEvent.click(within(drawer).getByRole('button',{name:'保存角色'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/save')[0]).toMatchObject({id:role.id,provider_id:'alternate-provider',background:generatedPersona.background}));
 },90000);
});

describe('agent autonomous action workspace',()=>{
 it('shows platform route budgets while keeping AI service budgets off the page',async()=>{
  const {fetch}=setup({budgets:{'llm:ctn-official':{remaining:0,limit:3,retry_after:20},'llm:provider':{retry_after:10},'auth:login':{remaining:4,limit:5},'auth:register':{remaining:5,limit:5},'read:get_post':{remaining:299,limit:300},'read:my_likes':{remaining:298,limit:300},'read:following':{remaining:297,limit:300},unlike:{remaining:29,limit:30},unfollow:{remaining:28,limit:30}}});
  const toggle=await screen.findByText('赛事平台额度与冷却');fireEvent.click(toggle);
  const section=toggle.closest('.ant-collapse-item') as HTMLElement;
  for(const text of ['登录账号','注册账号','帖子详情','核对点赞关系','关注列表','取消点赞','取消关注'])expect(await within(section).findByText(text)).toBeTruthy();
  expect(within(section).queryByText('AI 决策服务')).toBeNull();expect(within(section).queryByText('约 20 秒后继续')).toBeNull();
  expect(posted(fetch,'/api/agents/settings')).toHaveLength(0);
 },30000);
 it('hides the platform budget panel when only AI budgets exist',async()=>{
  setup({budgets:{'llm:ctn-official':{remaining:0,limit:3,retry_after:20}}});await screen.findByText('林青');
  expect(screen.queryByText('赛事平台额度与冷却')).toBeNull();
 },30000);
 it('keeps style guidance optional for both the original and edited draft',()=>{
  const onPublish=vi.fn().mockResolvedValue(true),props={agent:{...role,interests:role.interests.join('\n'),allowed_actions:undefined},busy:false,onPublish,onDiscard:vi.fn().mockResolvedValue(true),onReconcile:vi.fn().mockResolvedValue(true)};
  const draft={id:'style-1',agent_id:role.id,action:'post' as const,body:'We need action—now. **Vote Yes**',status:'draft',style_warnings:['不要使用破折号。','去掉 Markdown 加粗符号。']};
  const view=render(<MemoryRouter><DraftCard {...props} draft={draft}/></MemoryRouter>);
  expect(screen.getByText('表达风格提示')).toBeTruthy();expect(screen.getByText('不要使用破折号。')).toBeTruthy();
  expect(screen.getByText('可按需要调整，确认后可执行。')).toBeTruthy();
  const publish=screen.getByRole('button',{name:/发\s*布/});expect((publish as HTMLButtonElement).disabled).toBe(false);fireEvent.click(publish);
  expect(onPublish).toHaveBeenCalledWith('style-1',draft.body);
  fireEvent.change(screen.getByRole('textbox'),{target:{value:'I keep thinking about the late bus after my shift. Can we make it more reliable?'}});
  expect(screen.getByText('原稿的表达提示')).toBeTruthy();expect(screen.getByText('正文已修改，可按当前内容确认执行。')).toBeTruthy();
  expect((publish as HTMLButtonElement).disabled).toBe(false);fireEvent.click(publish);
  expect(onPublish).toHaveBeenCalledWith('style-1','I keep thinking about the late bus after my shift. Can we make it more reliable?');
  view.rerender(<MemoryRouter><DraftCard {...props} draft={{...draft,body:'I keep thinking about the late bus after my shift. Can we make it more reliable?',style_warnings:[]}}/></MemoryRouter>);
  expect(screen.queryByLabelText('表达风格提示')).toBeNull();
 });
 it('publishes a pending like with an empty body, preserving its target and decision reason',async()=>{
  const {fetch}=setup({drafts:[{id:'like-1',agent_id:role.id,action:'like',body:'',state:'pending',target_post_id:501,reason:'与角色长期关注的社区教育有关',source_ids:['post:501'],notification_ids:[17]}]});
  await screen.findByText('林青');fireEvent.click(screen.getByRole('tab',{name:'操作记录 · 1 项未完成'}));
  expect(await screen.findByRole('link',{name:'目标帖子 #501 ↗'})).toBeTruthy();
  expect(screen.getByText('与角色长期关注的社区教育有关',{exact:false})).toBeTruthy();
  expect(screen.queryByRole('textbox')).toBeNull();
  const button=screen.getByRole('button',{name:'确认点赞'});expect((button as HTMLButtonElement).disabled).toBe(false);fireEvent.click(button);
  await waitFor(()=>expect(posted(fetch,'/api/agents/publish')).toEqual([{draft_id:'like-1',body:''}]));
 },30000);
 it('reconciles an uncertain like directly against the account without asking for a new post id',async()=>{
  const {fetch}=setup({drafts:[{id:'like-2',agent_id:role.id,action:'like',body:'',status:'uncertain',target_post_id:502}]});
  await screen.findByText('林青');fireEvent.click(screen.getByRole('tab',{name:'操作记录 · 1 项未完成'}));
  expect(screen.queryByLabelText('核对帖子 ID')).toBeNull();fireEvent.click(await screen.findByRole('button',{name:'核对远端操作'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/reconcile')).toEqual([{draft_id:'like-2'}]));
 },30000);
 it('requires a reason and explicit confirmation before releasing an uncertain action',async()=>{
  const {fetch}=setup({drafts:[{id:'like-unknown',agent_id:role.id,account_id:12,version:4,action:'like',body:'',status:'uncertain',target_post_id:502}]});
  await screen.findByText('林青');fireEvent.click(screen.getByRole('tab',{name:'操作记录 · 1 项未完成'}));
  fireEvent.click(await screen.findByRole('button',{name:'人工确认未执行'}));
  const dialog=await screen.findByRole('dialog');
  const confirm=within(dialog).getByRole('button',{name:'确认未执行并放弃'}) as HTMLButtonElement;
  expect(confirm.disabled).toBe(true);
  fireEvent.change(within(dialog).getByLabelText('未执行确认依据'),{target:{value:'已检查账号记录，未点赞该目标'}});
  expect(confirm.disabled).toBe(true);
  fireEvent.click(within(dialog).getByRole('checkbox',{name:'我已检查远端记录，确认这项操作未执行'}));
  expect(confirm.disabled).toBe(false);fireEvent.click(confirm);
  await waitFor(()=>expect(posted(fetch,'/api/agents/discard-unexecuted')).toEqual([{draft_id:'like-unknown',reason:'已检查账号记录，未点赞该目标',expected_version:4,confirmed_not_executed:true}]));
  expect(posted(fetch,'/api/agents/publish')).toEqual([]);
 },30000);
 it('keeps the confirmed version from when the uncertain-action dialog was opened',async()=>{
  const onConfirmUnexecuted=vi.fn().mockResolvedValue(false),props={busy:false,onPublish:vi.fn(),onDiscard:vi.fn(),onReconcile:vi.fn(),onConfirmUnexecuted};
  const draft={id:'uncertain-version',agent_id:role.id,version:3,action:'post' as const,body:'Original attempt.',status:'uncertain'};
  const view=render(<MemoryRouter><DraftCard {...props} draft={draft}/></MemoryRouter>);
  fireEvent.click(screen.getByRole('button',{name:'人工确认未执行'}));
  view.rerender(<MemoryRouter><DraftCard {...props} draft={{...draft,version:4}}/></MemoryRouter>);
  const dialog=await screen.findByRole('dialog');
  fireEvent.change(within(dialog).getByLabelText('未执行确认依据'),{target:{value:'检查过发帖记录'}});
  fireEvent.click(within(dialog).getByRole('checkbox'));
  fireEvent.click(within(dialog).getByRole('button',{name:'确认未执行并放弃'}));
  await waitFor(()=>expect(onConfirmUnexecuted).toHaveBeenCalledWith(draft.id,'检查过发帖记录',3));
  expect(screen.getByRole('dialog')).toBeTruthy();
 },30000);
 it('accepts a plain repost while requiring text for a reply and limiting edited text to 500 characters',()=>{
  const onPublish=vi.fn().mockResolvedValue(true),onDiscard=vi.fn().mockResolvedValue(true),onReconcile=vi.fn().mockResolvedValue(true);
  const props={agent:{...role,interests:role.interests.join('\n'),allowed_actions:undefined},busy:false,onPublish,onDiscard,onReconcile};
  const view=render(<MemoryRouter><DraftCard {...props} draft={{id:'repost',agent_id:role.id,action:'repost',body:'',status:'draft',target_post_id:503}}/></MemoryRouter>);
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).maxLength).toBe(500);
  fireEvent.click(screen.getByRole('button',{name:'确认转发'}));expect(onPublish).toHaveBeenCalledWith('repost','');
  view.rerender(<MemoryRouter><DraftCard {...props} draft={{id:'reply',agent_id:role.id,action:'reply',body:'',status:'draft',target_post_id:503}}/></MemoryRouter>);
  expect((screen.getByRole('button',{name:'确认回复'}) as HTMLButtonElement).disabled).toBe(true);
 });
 it('shows observation freshness and notification resolution with traceable original posts',async()=>{
  setup({agents:[{...role,observation:{observed_at:'2026-10-20T01:00:00Z',source_count:8,new_source_count:2,notifications_status:'cached',home_status:'deferred',latest_post_at:'2026-10-20T00:55:00Z'},inbox:[{id:11,post_id:601,kind:'reply',status:'unresolved',source_id:'post:601'},{id:12,post_id:602,kind:'mention',status:'ack_pending',source_id:'post:602'},{id:13,post_id:603,kind:'reply',status:'resolved',resolved_at:'2026-10-20T01:00:00Z'}]}]});
  expect(await screen.findByText('通知 · 使用缓存')).toBeTruthy();expect(screen.getByText('首页 · 等待调度')).toBeTruthy();
  expect(screen.getByText('帖子 90 分钟 · 新闻采集 36 小时')).toBeTruthy();
  fireEvent.click(screen.getByRole('tab',{name:'收到的通知 · 2 条未确认'}));
  expect(await screen.findByText('未确认')).toBeTruthy();expect(screen.getByText('确认中')).toBeTruthy();expect(screen.getByText('已确认')).toBeTruthy();
  expect(screen.getByRole('link',{name:'通知原帖 #601 ↗'}).getAttribute('href')).toBe('/posts?post=601');expect(screen.getByText('资料 post:602')).toBeTruthy();
 },30000);
 it('distinguishes incoming replies from the associated like action and platform confirmation',async()=>{
  setup({agents:[{...role,inbox:[{id:14,post_id:604,kind:'reply',status:'resolved',local_progress:'completed',related_draft_id:'like-done',related_action:'like',related_draft_state:'published'}]}]});
  await screen.findByText('林青');fireEvent.click(screen.getByRole('tab',{name:'收到的通知 · 0 条未确认'}));
  expect(await screen.findByText('收到回复')).toBeTruthy();expect(screen.getByText('关联操作已完成')).toBeTruthy();expect(screen.getByText('已确认')).toBeTruthy();
  expect(screen.getByRole('link',{name:'点赞 · 已完成'}).getAttribute('href')).toBe('/ai/agents/role-1?tab=drafts&draft=like-done');
 },30000);
 it('confirms selected skipped notifications and reopens discarded notices without publishing or running a decision',async()=>{
  const {fetch}=setup({agents:[{...role,inbox:[{id:15,kind:'reply',status:'unresolved',local_progress:'skipped_awaiting_confirmation',related_draft_id:'skip-1',related_action:'skip',related_draft_state:'skipped',can_confirm_skip:true},{id:16,kind:'reply',status:'unresolved',local_progress:'discarded',related_draft_id:'discarded-1',related_action:'reply',related_draft_state:'discarded',can_reconsider:true}]}]});
  await screen.findByText('林青');fireEvent.click(screen.getByRole('tab',{name:'收到的通知 · 2 条未确认'}));
  expect(await screen.findByText('已决定跳过，待确认')).toBeTruthy();expect(screen.getByText('操作已放弃，待重新判断')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'确认跳过通知'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/notifications/confirm-skip')).toEqual([{draft_id:'skip-1'}]));
  await waitFor(()=>expect((screen.getByRole('button',{name:'重新判断通知'}) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button',{name:'重新判断通知'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/notifications/reconsider')).toEqual([{agent_id:role.id,notification_id:16}]));
  expect(posted(fetch,'/api/agents/publish')).toHaveLength(0);expect(posted(fetch,'/api/agents/run')).toHaveLength(0);
 },30000);
 it('counts an unconfirmed review skip as unfinished while completed actions stay out of the tab count',async()=>{
  setup({agents:[{...role,inbox:[{id:17,kind:'reply',status:'unresolved'}]}],drafts:[{id:'skip-review',agent_id:role.id,action:'skip',body:'',state:'skipped',notification_ids:[17]},{id:'published-old',agent_id:role.id,action:'reply',body:'已经发布的旧回复',state:'published',post_id:701}]});
  await screen.findByText('林青');expect(screen.getByRole('tab',{name:'收到的通知 · 1 条未确认'})).toBeTruthy();
  fireEvent.click(screen.getByRole('tab',{name:'操作记录 · 1 项未完成'}));
  expect(await screen.findByRole('link',{name:'前往确认关联通知'})).toBeTruthy();expect(screen.queryByText('已经发布的旧回复')).toBeNull();
  expect(screen.getByText('计数为待审阅、待执行、执行失败、待核对及待确认跳过的未完成项目，不是累计完成次数。')).toBeTruthy();
 },30000);
 it('shows the review hold on a cancelled AI draft without classifying it as a manual instruction',()=>{
  render(<MemoryRouter><DraftCard draft={{id:'cancelled-draft',agent_id:role.id,action:'reply',body:'尚未发出的回复',status:'draft',requires_review:true,review_reason:'生成已取消，已保留草稿待审阅。'}} agent={{...role,interests:role.interests.join('\n'),allowed_actions:undefined}} busy={false} onPublish={vi.fn().mockResolvedValue(true)} onDiscard={vi.fn().mockResolvedValue(true)} onReconcile={vi.fn().mockResolvedValue(true)}/></MemoryRouter>);
  expect(screen.getByText('生成已取消，已保留草稿待审阅。')).toBeTruthy();expect(screen.queryByText('手动操作')).toBeNull();expect(screen.getByRole('button',{name:'确认回复'})).toBeTruthy();
 });
 it('sends only enabled from the global switch and keeps unsaved team settings intact',async()=>{
  const {fetch}=setup();await screen.findByText('林青');fireEvent.click(screen.getByRole('tab',{name:'队伍设置'}));
  const limit=await screen.findByLabelText('角色数量上限');fireEvent.change(limit,{target:{value:'17'}});
  const policy=screen.getByRole('switch',{name:'禁止 Agent 内部互动'});expect(policy.getAttribute('aria-checked')).toBe('true');fireEvent.click(policy);
  expect(screen.queryByLabelText('全队写操作最短间隔（秒）')).toBeNull();
  fireEvent.click(screen.getByRole('switch',{name:'持续运行所有已启用角色'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/settings')).toEqual([{enabled:true}]));
  expect((limit as HTMLInputElement).value).toBe('17');expect((screen.getByLabelText('队伍立场') as HTMLInputElement).value).toBe('Vote Yes — 社区');
  await waitFor(()=>expect((screen.getByRole('button',{name:'保存队伍设置'}) as HTMLButtonElement).disabled).toBe(false));fireEvent.click(screen.getByRole('button',{name:'保存队伍设置'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/settings').at(-1)).toEqual({enabled:true,team_side:'Vote Yes — 社区',max_accounts:17,block_agent_interactions:false}));
 },30000);
 it('loads a saved disabled interaction policy and persists enabling it',async()=>{
  const {fetch}=setup({settings:{...initial.settings,block_agent_interactions:false},interaction_policy:{enabled:false,blocked_today:4,managed_accounts:2}});await screen.findByText('林青');fireEvent.click(screen.getByRole('tab',{name:'队伍设置'}));
  const policy=await screen.findByRole('switch',{name:'禁止 Agent 内部互动'});expect(policy.getAttribute('aria-checked')).toBe('false');
  expect(screen.getByText('禁止受管 Agent 之间回复、点赞、关注、转发及正文 @提及；人工取消点赞和取消关注仍可用于清理历史关系。',{exact:false})).toBeTruthy();expect(screen.getByText('今日已拦截 4 次。',{exact:false})).toBeTruthy();
  fireEvent.click(policy);fireEvent.click(screen.getByRole('button',{name:'保存队伍设置'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/settings')).toEqual([{enabled:false,team_side:initial.settings.team_side,max_accounts:23,block_agent_interactions:true}]));
 },30000);
 it('shows policy cancellations without claiming an action completed or allowing confirmation',()=>{
  render(<MemoryRouter><DraftCard draft={{id:'blocked-like',agent_id:role.id,action:'like',body:'',status:'skipped',policy_blocked:true,policy_reason:'目标账号属于受管 Agent。',requires_review:true,target_post_id:720,notification_ids:[20]}} agent={{...role,interests:role.interests.join('\n'),allowed_actions:undefined}} busy={false} onPublish={vi.fn().mockResolvedValue(true)} onDiscard={vi.fn().mockResolvedValue(true)} onReconcile={vi.fn().mockResolvedValue(true)}/></MemoryRouter>);
  expect(screen.getByText('策略已拦截')).toBeTruthy();expect(screen.getByText('目标账号属于受管 Agent。',{exact:false})).toBeTruthy();expect(screen.getByText('操作结果：策略已拦截')).toBeTruthy();
  expect(screen.queryByText('点赞已完成',{exact:false})).toBeNull();expect(screen.queryByText('此操作已暂停自动执行，请审阅后手动确认。')).toBeNull();expect(screen.queryByRole('button',{name:'确认点赞'})).toBeNull();expect(screen.queryByRole('link',{name:'前往确认关联通知'})).toBeNull();
 });
 it('keeps ignored internal notifications visible while excluding them from unfinished counts and controls',async()=>{
  setup({agents:[{...role,inbox:[{id:21,kind:'reply',status:'unresolved',policy_ignored:true,policy_reason:'内部互动通知。',related_draft_id:'blocked-skip',can_confirm_skip:true,can_reconsider:true},{id:22,kind:'mention',status:'unresolved'}]}],drafts:[{id:'blocked-skip',agent_id:role.id,action:'skip',body:'',state:'skipped',policy_blocked:true,notification_ids:[21]}]});
  await screen.findByText('林青');expect(screen.getByRole('tab',{name:'操作记录 · 0 项未完成'})).toBeTruthy();fireEvent.click(screen.getByRole('tab',{name:'收到的通知 · 1 条未确认'}));
  expect(await screen.findByText('内部互动，已忽略')).toBeTruthy();expect(screen.getByText('无需处理')).toBeTruthy();expect(screen.getByText('#21')).toBeTruthy();expect(screen.getByText('#22')).toBeTruthy();
  expect(screen.queryByRole('button',{name:'确认跳过通知'})).toBeNull();expect(screen.queryByRole('button',{name:'重新判断通知'})).toBeNull();
 },30000);
 it('retains an existing role identity, memory and action settings when the editor saves',async()=>{
  const {fetch}=setup();await screen.findByText('林青');fireEvent.click(screen.getByRole('button',{name:'编辑角色'}));
  const drawer=await screen.findByRole('dialog');expect((within(drawer).getByLabelText('手动补充的长期笔记') as HTMLTextAreaElement).value).toBe(role.memory);
  expect((within(drawer).getByLabelText('近期帖子窗口（分钟）') as HTMLInputElement).value).toBe('90');
  expect((within(drawer).getByLabelText('操作方式') as HTMLSelectElement).value).toBe('review');
  expect(within(drawer).queryByLabelText('每日操作上限')).toBeNull();
  expect((within(drawer).getByLabelText('无新消息时的检查间隔（秒）') as HTMLInputElement).value).toBe('1800');
  fireEvent.click(within(drawer).getByRole('button',{name:'保存角色'}));
  await waitFor(()=>expect(posted(fetch,'/api/agents/save')[0]).toMatchObject({id:role.id,version:3,name:role.name,background:role.background,stance:role.stance,enabled:false,mode:'review',allowed_actions:role.allowed_actions,freshness_minutes:90,news_freshness_hours:36,notification_interval_seconds:120}));
  expect(posted(fetch,'/api/agents/save')[0]).not.toHaveProperty('daily_limit');
  expect(posted(fetch,'/api/agents/memory')).toHaveLength(0);
 },30000);
});
describe('compact team snapshot',()=>{
 it('refetches the team snapshot after a write that answers with a receipt',async()=>{
  const {fetch,client}=setup({}, {receipts:true});await screen.findByText('林青');
  let receiptCached=false;const unsubscribe=client.getQueryCache().subscribe(event=>{if(event.query.queryKey[0]==='/agents'&&(event.query.state.data as {ok?:boolean}|undefined)?.ok)receiptCached=true;});
  const reads=()=>fetch.mock.calls.filter(([url,options])=>url==='/api/agents'&&!options?.body).length,before=reads();
  const continuous=screen.getByRole('switch',{name:'持续运行所有已启用角色'});fireEvent.click(continuous);
  await waitFor(()=>expect(posted(fetch,'/api/agents/settings')).toEqual([{enabled:true}]));
  await waitFor(()=>expect(continuous.getAttribute('aria-checked')).toBe('true'));unsubscribe();
  expect(reads()).toBeGreaterThan(before);expect(receiptCached).toBe(false);expect(screen.getByText('林青')).toBeTruthy();
 },30000);
 it('loads the omitted sources of a finished draft only when they are opened',async()=>{
  const fetch=vi.fn((_input:RequestInfo|URL)=>Promise.resolve(new Response(JSON.stringify({draft:{id:'done-1',sources:[{id:'post:7',post_id:7,title:'目标资料',origin:'notification',text:'延迟载入的原帖内容'}]}}),{status:200,headers:{'Content-Type':'application/json'}})));
  vi.stubGlobal('fetch',fetch);
  const client=new QueryClient({defaultOptions:{queries:{retry:false}}});clients.push(client);
  render(<QueryClientProvider client={client}><MemoryRouter><DraftCard draft={{id:'done-1',agent_id:role.id,action:'reply',body:'已发出的回复',status:'published',post_id:9,source_ids:['post:7'],sources_omitted:1}} agent={{...role,interests:role.interests.join('\n'),allowed_actions:undefined}} busy={false} onPublish={vi.fn().mockResolvedValue(true)} onDiscard={vi.fn().mockResolvedValue(true)} onReconcile={vi.fn().mockResolvedValue(true)}/></MemoryRouter></QueryClientProvider>);
  expect(screen.queryByText('依据资料：post:7')).toBeNull();expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('阅读来源 · 1 条'));
  expect(await screen.findByText('延迟载入的原帖内容')).toBeTruthy();expect(String(fetch.mock.calls[0][0])).toBe('/api/agents/drafts/done-1');
 });
 it('loads the edited role memory on demand',async()=>{
  const {fetch}=setup({}, {memory:{agent_id:role.id,memory_view:{long_term:{total:3},subjective_notes:[{id:'viewpoints:v1',kind:'viewpoints',title:'主观看法',excerpt:'按需载入的观点',source_ids:['post:7'],evidence:[]}]},last_sources:[],activity:[]}});
  await screen.findByText('林青');fireEvent.click(screen.getByRole('button',{name:'编辑角色'}));
  const drawer=await screen.findByRole('dialog');
  expect(await within(drawer).findByText('按需载入的观点')).toBeTruthy();
  expect(fetch.mock.calls.some(([url])=>url===`/api/agents/${role.id}/memory`)).toBe(true);
 },30000);
});
