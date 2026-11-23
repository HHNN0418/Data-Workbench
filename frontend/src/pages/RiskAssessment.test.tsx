// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, render, screen, within} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import RiskAssessment from './RiskAssessment';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
let client:QueryClient;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});

const overview={
 generated_at:'2026-10-24T03:00:00Z',
 rules:{confirm_minutes:20,segment_hours:6,fast_interval_minutes:20,full_hours:48,full_interval_minutes:180,account_report_min_hidden:1,account_report_min_actions:100},
 monitor:{last_unit:'fast',last_at:'2026-10-24T02:59:00Z',last_error:null,
  fast:{at:'2026-10-24T02:58:00Z',pages:97,listed:19300,collected:19340,missing:40,suspect:12,requests:137},
  full:{started_at:'2026-10-24T01:00:00Z',finished_at:null,segments:[{missing:30,suspect:8,requests:120},{missing:20,suspect:5,requests:110}]},
  recheck:{at:'2026-10-24T02:50:00Z',due:9,hidden:7,visible:1}},
 totals:{suspect:12,hidden:540,visible:3,hidden_24h:200,accounts_with_hidden:310,others_report_ready:1},
 own_risks:[{account_id:10243,name:'Tomás',hidden:2,actions:180,level:'suspend_next',text:'Tomás（10243）已有 2 条帖子被隐藏，再被隐藏 1 条就可能封号'}],
 ours:[{account_id:10243,name:'Tomás',username:'tomas_a',role_state:'active',hidden:2,suspect:0,collected_actions:180,last_post_at:'2026-10-24T02:00:00Z',active:true},
  {account_id:6494,name:'Nora Bennett',username:'nora_bennett',role_state:'suspended',hidden:1,suspect:0,collected_actions:303,last_post_at:'2026-10-23T18:00:00Z',active:false},
  {account_id:5554,name:'Maya Collins',username:'maya_collins',role_state:'disabled',hidden:3,suspect:0,collected_actions:400,last_post_at:'2026-10-22T11:04:00Z',active:false}],
 teammates:[{account_id:6386,username:'mate_one',hidden:1,suspect:0,collected_actions:40,last_post_at:null,active:false}],
 others:[{account_id:8000,username:'bot_a',hidden:1,suspect:1,collected_actions:150,last_post_at:'2026-10-24T02:30:00Z',active:true,is_bot:true,account_report:{ready:true,missing:[]}},
  {account_id:8001,username:'bot_b',hidden:0,suspect:2,collected_actions:20,last_post_at:'2026-10-23T20:00:00Z',active:false,is_bot:false,account_report:{ready:false,missing:['hidden','actions']}}],
 others_total:2,
 recent:[{post_id:311001,account_id:8000,username:'bot_a',group:'others',status:'suspect',first_404_at:'2026-10-24T02:58:00Z',confirmed_at:null,excerpt:'copy text'},
  {post_id:310900,account_id:10243,username:'tomas_a',group:'ours',status:'hidden',first_404_at:'2026-10-24T02:00:00Z',confirmed_at:'2026-10-24T02:21:00Z',excerpt:''}],
 caveats:['公开数据分不清版主隐藏和作者自己删帖；我方角色自己删的帖子已排除'],
};

// copy_patterns.py's view, as risk_assessment.overview passes it on.
const groupsOf=(ours:number[],teammates:number[],others:number[],npc:number[])=>({ours:{accounts:ours[0],posts:ours[1]},teammates:{accounts:teammates[0],posts:teammates[1]},others:{accounts:others[0],posts:others[1]},npc:{accounts:npc[0],posts:npc[1]}});
const copyPatterns={
 status:'ready',generated_at:'2026-10-24T02:30:00Z',window:{hours:48},posts:138837,last_error:null,
 rules:{gram_words:6,min_accounts:3,fact_words:8,opening_words:4,frame_words:3,refresh_minutes:30},
 run:{cpu_seconds:19.4},
 shared:{grams:87685,phrases:69481,posts:90358,share:0.651},
 by_group:{ours:{accounts:30,share:0.992,coverage:0.61},teammates:{accounts:15,share:0.506,coverage:0.2},others:{accounts:5951,share:0.669,coverage:0.25},npc:{accounts:2059,share:0.403,coverage:0.08}},
 phrases:[{phrase:'every eligible kingstonian has had a fair chance',grams:4,posts:835,accounts:128,npc_free:true,variants:3,groups:groupsOf([30,700],[0,0],[98,135],[0,0]),
  examples:[{post_id:340001,account_id:10242,username:'grace_whitaker',group:'ours',excerpt:'Every eligible Kingstonian has had a fair chance first.'}],
  members:[{account_id:10242,username:'grace_whitaker',group:'ours',posts:39},{account_id:8800,username:'other_a',group:'others',posts:4}],members_total:128},
  {phrase:'what i keep coming back to',grams:1,posts:304,accounts:186,npc_free:false,variants:0,groups:groupsOf([2,3],[0,0],[180,290],[4,11]),examples:[],members:[],members_total:186}],
 openings:[{text:'the government says every',posts:400,accounts:40,groups:groupsOf([25,380],[0,0],[15,20],[0,0])}],
 frames:[{text:'the government says',posts:900,accounts:150,groups:groupsOf([30,600],[2,3],[110,290],[8,7])}],
 facts:{facts:317,posts:1832,facts_copied:40,groups:groupsOf([30,860],[3,28],[300,942],[2,2]),
  top:[{fact_id:'phased_after_access',claim:'If Yes wins, the government says the rule is phased in only after every eligible Kingstonian…',posts:431,accounts:60,groups:groupsOf([28,400],[0,0],[32,31],[0,0])}]},
 accounts_view:{teammates:[{account_id:6390,username:'mate_two',posts:20,shared_posts:10,share:0.5,coverage:0.2,fact_posts:1,phrases:12}],
  npc:{accounts:2059,with_shared:1534,share:0.403,coverage:0.08,top:[{account_id:1500,username:'npc_one',posts:12,shared_posts:3,share:0.25,coverage:0.05,fact_posts:0,phrases:2}]},
  others:{accounts:5951,with_shared:4684,share:0.669,coverage:0.25,top:[{account_id:8800,username:'other_a',posts:200,shared_posts:190,share:0.95,coverage:0.7,fact_posts:3,phrases:300}]}},
 ours:{posts_total:1660,
  hidden_rates:[{split:'shared',key:'template',label:'带共用短语或照抄事实',posts:1660,hidden:20,suspect:0,hidden_rate:0.012},{split:'shared',key:'plain',label:'都不带',posts:14,hidden:0,suspect:0,hidden_rate:0},
   {split:'coverage',key:'c75',label:'≥ 75%',posts:300,hidden:9,suspect:1,hidden_rate:0.03}],
  roles:[{account_id:10242,name:'Grace',username:'grace_whitaker',posts:104,shared_posts:104,share:1,coverage:0.66,fact_posts:50,phrases:400,hidden:2}],
  phrases:[{phrase:'every eligible kingstonian has had a fair chance',accounts:128,variants:2,groups:groupsOf([30,700],[0,0],[98,135],[0,0])}],
  posts:[{post_id:339999,account_id:10242,name:'Grace',status:'hidden',coverage:0.8,phrase:'every eligible kingstonian has had a fair chance',phrase_accounts:128,fact_ids:['phased_after_access'],excerpt:'The government says…'}]},
 caveats:['只做识别和风险提示，不接进生成或写作流程（D47）'],
};

function mount(data:object) {
 const fetch=vi.fn(async(_input:RequestInfo|URL)=>new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}}));
 vi.stubGlobal('fetch',fetch);
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><MemoryRouter><RiskAssessment/></MemoryRouter></QueryClientProvider>);
 return fetch;
}

describe('risk assessment page',()=>{
 it('shows the scan, the totals and every group of accounts',async()=>{
  const fetch=mount(overview);
  expect(await screen.findByText('别队可举报账号')).toBeTruthy();
  expect(new URL(String(fetch.mock.calls[0][0]),'http://local').pathname).toBe('/api/risk/overview');
  expect(screen.getByText('24 小时内 200 条')).toBeTruthy();
  expect(screen.getByText(/翻 97 页 · 帖子流 19,300 条 \/ 库里 19,340 条 · 缺 40 条 · 新待复核 12 条/)).toBeTruthy();
  expect(screen.getByText(/进行中 2\/7 段 · 缺 50 条 · 新待复核 13 条 · 请求 230 次/)).toBeTruthy();
  expect(screen.getByText(/到期 9 条 · 确认 7 条 · 恢复可见 1 条/)).toBeTruthy();
  // Our roles' risk comes from the shared ownAccounts summary.
  expect(screen.getByText('1 个账号有封号风险')).toBeTruthy();
  expect(screen.getByText('队友在管，我们只监控')).toBeTruthy();
  expect(screen.getByRole('link',{name:'@mate_one'}).getAttribute('href')).toBe('/accounts?q=6386');
 });

 it('keeps suspended and disabled roles in the roles table with their status',async()=>{
  mount(overview);
  const roles=(await screen.findByText('我方账号')).closest('section') as HTMLElement;
  const status=(name:string)=>within(within(roles).getByRole('link',{name}).closest('tr') as HTMLElement);
  expect(status('@nora_bennett').getByText('已封号')).toBeTruthy();
  expect(status('@maya_collins').getByText('已停用')).toBeTruthy();
  expect(status('@tomas_a').getByText('在用')).toBeTruthy();
  const teammates=screen.getByText('队友账号').closest('section') as HTMLElement;
  expect(within(teammates).queryByRole('link',{name:'@nora_bennett'})).toBeNull();
 });

 it('marks which other accounts meet the account report rule and lists recent posts',async()=>{
  mount(overview);
  const others=(await screen.findByText('别队账号 · 2')).closest('section') as HTMLElement;
  const ready=within(others).getByRole('link',{name:'@bot_a'}).closest('tr') as HTMLElement;
  expect(within(ready).getByText('满足')).toBeTruthy();
  expect(within(ready).getByText('机器人')).toBeTruthy();
  const waiting=within(others).getByRole('link',{name:'@bot_b'}).closest('tr') as HTMLElement;
  expect(within(waiting).getByText('还没有确认隐藏的帖子；采集到的动作不到 100 次')).toBeTruthy();
  const recent=screen.getByText('最近的隐藏和待复核帖子').closest('section') as HTMLElement;
  expect(within(recent).getByRole('link',{name:'#311001'}).getAttribute('href')).toBe('/posts?post=311001');
  expect(within(recent).getByText('待复核')).toBeTruthy();
  expect(within(recent).getByText('已确认隐藏')).toBeTruthy();
  expect(within(recent).getByText('无文本')).toBeTruthy();
 });

 it('says when nothing has run yet and shows the last error',async()=>{
  mount({...overview,monitor:{last_error:{at:'2026-10-24T02:00:00Z',error:'ReadTimeout: timed out'}},own_risks:[],ours:[],teammates:[],others:[],others_total:0,recent:[]});
  expect(await screen.findByText('ReadTimeout: timed out')).toBeTruthy();
  expect(screen.getAllByText(/还没有运行/).length).toBe(2);
  expect(screen.getByText('没有角色接近封号线。')).toBeTruthy();
  expect(screen.queryByText('队友在管，我们只监控')).toBeNull();
 });

 it('shows the copy patterns of every account: shared phrases, openings, fact copies and accounts',async()=>{
  mount({...overview,copy_patterns:copyPatterns});
  const phrases=(await screen.findByText('文案识别 · 共用短语')).closest('section') as HTMLElement;
  expect(within(phrases).getByText('every eligible kingstonian has had a fair chance')).toBeTruthy();
  expect(within(phrases).getByText('NPC 不用')).toBeTruthy();
  expect(within(phrases).getByText('NPC 也用')).toBeTruthy();
  expect(within(phrases).getByText(/另有 3 个相近片段/)).toBeTruthy();
  expect(within(phrases).getByText('30 / 0 / 98 / 0')).toBeTruthy();
  expect(within(phrases).getByText('90,358 / 138,837 条 · 69,481 个短语')).toBeTruthy();
  expect(within(phrases).getByRole('link',{name:'#340001'}).getAttribute('href')).toBe('/posts?post=340001');
  const lines=screen.getByText('开头和固定句式').closest('section') as HTMLElement;
  expect(within(lines).getByText('the government says every')).toBeTruthy();
  expect(within(lines).getByText('the government says')).toBeTruthy();
  expect(within(lines).getByText('phased_after_access')).toBeTruthy();
  const accounts=screen.getByText('账号：带共用短语的帖子占比').closest('section') as HTMLElement;
  expect(within(accounts).getByText(/2,059 个账号里 1,534 个用过共用短语/)).toBeTruthy();
  expect(within(accounts).getByRole('link',{name:'@other_a'}).getAttribute('href')).toBe('/accounts?q=8800');
  expect(within(accounts).getByRole('link',{name:'@mate_two'})).toBeTruthy();
  expect(screen.getByText('只做识别和风险提示，不接进生成或写作流程（D47）')).toBeTruthy();
 });

 it('compares our template posts with hidden posts',async()=>{
  mount({...overview,copy_patterns:copyPatterns});
  const ours=(await screen.findByText('我方：模板帖和隐藏率')).closest('section') as HTMLElement;
  expect(within(ours).getByText('带共用短语或照抄事实')).toBeTruthy();
  expect(within(ours).getByText('1.2%')).toBeTruthy();
  expect(within(ours).getByText('3.0%')).toBeTruthy();
  const post=within(ours).getByRole('link',{name:'#339999'}).closest('tr') as HTMLElement;
  expect(within(post).getByText('已确认隐藏')).toBeTruthy();
  expect(within(post).getByText('80%')).toBeTruthy();
  expect(within(ours).getByText('我方带共用短语或照抄事实的帖子 · 1,660')).toBeTruthy();
 });

 it('says when the copy patterns are not ready and shows their error',async()=>{
  mount({...overview,copy_patterns:{status:'pending',rules:{refresh_minutes:30},last_error:null}});
  expect(await screen.findByText(/后台每 30 分钟算一次近 48 小时全部帖子的文案/)).toBeTruthy();
  cleanup();client.clear();
  mount({...overview,copy_patterns:{status:'pending',last_error:{at:'2026-10-24T02:00:00Z',error:'RuntimeError: database is down'}}});
  expect(await screen.findByText('RuntimeError: database is down')).toBeTruthy();
 });
});
