// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {App} from 'antd';
import type {ReactElement} from 'react';
import {stagePlans} from './StagePlaceholder';
import {matchNav} from '../nav/navConfig';
import Exposure from './Exposure';
import Attribution from './Attribution';
import AgentPipeline from './AgentPipeline';
import Processing from './Processing';
import Strategy from './Strategy';
import SettingsIndex from './SettingsIndex';
import Maintenance from './Maintenance';

vi.mock('../common',async importOriginal=>({...await importOriginal<typeof import('../common')>(),Chart:({ariaLabel}:{ariaLabel?:string})=><section aria-label={ariaLabel}/>}));

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
let client:QueryClient;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});

const hour=(end:string,extra:Record<string,unknown>={})=>({start:'',end,reports:0,unparsed:[],points:0,views:0,likes:0,reposts:0,replies:0,follows:0,toward:0,away:0,neither:0,
 primary_toward:0,our_posts:0,our_replies:0,our_other:0,our_trending:0,status:'no_report',outage:false,post_share:null,...extra});
const summary={points:352,views:78,likes:10,reposts:1,replies:7,follows:1,toward:1,away:1,neither:2,primary_toward:1,our_posts:2,our_replies:2,our_other:1,our_trending:1,hours:48,outage_hours:3,views_per_original:62};
const payloads:Record<string,unknown>={
 '/api/results/exposure':{generated_at:'2026-10-22T11:00:00Z',window:{hours:48},imported_until:'2026-10-22T09:20:00Z',summary_hours:6,read_only:true,stale:false,
  outage:{note:'平台故障：10 月 22 日悉尼时间约 16:00–18:36 计数冻结'},summary:{recent:{...summary,views:24},window:summary},notes:['官方小时按悉尼时间 (hh-1):47 到 hh:47 计'],
  hours:[hour('2026-10-22T11:47:00Z',{status:'in_progress',our_posts:1}),hour('2026-10-22T09:47:00Z',{status:'not_imported',our_replies:3}),
   hour('2026-10-22T08:47:00Z',{status:'reported',reports:2,points:91,views:24,away:1,our_posts:1,our_replies:2,our_trending:1,post_share:0.333}),
   hour('2026-10-22T06:47:00Z',{status:'reported',reports:2,points:87,views:16,toward:1,neither:2,outage:true})]},
 '/api/results/attribution':{generated_at:'2026-10-22T11:00:00Z',window:{hours:48},read_only:true,stale:false,success_reward:15,min_arm_posts:10,
  notes:['回报是代理指标：官方的说服只按队伍和小时公布，不能落到单条帖子'],
  totals:{posts:16,settled:14,pending:2,next_settle_at:'2026-10-22T15:00:00Z',mean_reward:7.14,success_rate:0.286,npc_replies_per_post:0.29,reply_yes:1},
  groups:{action:[{key:'reply',label:'reply',posts:14,settled:12,mean_reward:5,success_rate:0.25,npc_replies_per_post:0.25,likes_per_post:0.25,reposts_per_post:0,reply_yes:0},
   {key:'post',label:'post',posts:2,settled:2,mean_reward:20,success_rate:0.5,npc_replies_per_post:0.5,likes_per_post:2.5,reposts_per_post:0,reply_yes:1}],
   agent:[{key:'r2',label:'Ethan Brooks',posts:2,settled:2,mean_reward:20,success_rate:0.5}],concern:[],hour:[],target_stance:[],depth:[],pipeline:[]},
  arms:[{concern:'hospital',action:'reply',posts:12,successes:3,success_rate:0.25,mean_reward:5,npc_replies:3,reply_yes:0,ranked:true},
   {concern:'tourism',action:'post',posts:1,successes:1,success_rate:1,mean_reward:35,npc_replies:1,reply_yes:1,ranked:false}],
  latest:[{post_id:200,agent_id:'r2',agent_name:'Ethan Brooks',published_at:'2026-10-22T03:00:00Z',action:'post',concern:'tourism',target_stance:null,thread_depth:null,body_chars:240,
   npc_replies:1,reply_yes:1,reply_undecided:0,reply_no:0,likes:4,reposts:0,reward:35,fact_ids:['f1']}]},
 '/api/pipeline/steps':{generated_at:'2026-10-22T11:00:00Z',window:{hours:6},read_only:true,stale:false,notes:['发布耗时从「生成动作」算到「发布成功」'],
  totals:{completed:169,failed:63,failure_rate:0.272,published:171,round_seconds:{median:118.9,p90:198}},
  queue:{pending_actions:4,uncertain_actions:1,inbox_unresolved:2,maintenance:{active:false}},other_model_calls:{radar:3},
  steps:[{key:'reading',label:'选读',runs:236,rules_runs:172,model_runs:64,model_calls:64,ok:64,errors:0,error_rate:0,median_ms:2863,p90_ms:4715,top_errors:[]},
   {key:'decide',label:'决策',runs:235,model_calls:235,ok:225,errors:10,error_rate:0.043,median_ms:20983,p90_ms:48175,top_errors:[{reason:'output_truncated',count:9},{reason:'provider_error',count:1}]},
   {key:'write',label:'写作',runs:376,model_calls:376,ok:362,errors:14,error_rate:0.037,median_ms:15142,p90_ms:38227,top_errors:[{reason:'output_truncated',count:12},{reason:'reasoning_only',count:2}]},
   {key:'check',label:'检查',runs:271,model_calls:271,ok:270,errors:1,error_rate:0.004,median_ms:9350,p90_ms:23956,top_errors:[]},
   {key:'publish',label:'发布',runs:171,actions:{reply:136,post:35},median_ms:485420,p90_ms:817535,by_action:{reply:{count:136,median_ms:519722},post:{count:35,median_ms:22431}}}],
  failures:[{reason:'AI 正文两次未通过方向与事实检查，本轮未创建动作',count:48,examples:[{detail:'You copied a fact word for word.',count:3}]},{reason:'请先保存并选择一个 AI 连接',count:7,examples:[]}],
  failure_phases:{complete:38,reading:8},hourly:[{hour:'2026-10-22T11:00:00.000000Z',completed:12,failed:3,post:2,reply:9,other:0}],
  roles:[{agent_id:'r1',name:'Maya Collins',completed:20,failed:2,post:4,reply:10,other:0,model_calls:60,model_errors:1}]},
 '/api/processing/annotations':{generated_at:'2026-10-22T11:00:00Z',window:{hours:24},read_only:true,stale:false,max_reply_depth:7,notes:['立场和顾虑是关键词规则'],
  progress:{posts_total:211205,annotated:211205,current_classifier:211000,backlog:205,classifier:'902a96a1c40f',last_annotated_at:'2026-10-22T10:59:00Z',coverage:0.999,bot_accounts:699,last_pass:{posts:120}},
  window_totals:{all:70536,citizen:32406,own:400,bot:5000,posts:70600,with_concern:{all:40000,citizen:20000}},
  stance:{citizen:[{key:'yes',count:2335,share:0.0721},{key:'no',count:2925,share:0.0903},{key:'undecided',count:11800,share:0.3641},{key:'unrelated',count:15346,share:0.4736}],
   all:[{key:'yes',count:6589,share:0.0934},{key:'no',count:5756,share:0.0816},{key:'undecided',count:27333,share:0.3875},{key:'unrelated',count:30858,share:0.4375}]},
  depth:{citizen:[{key:'0',count:14776,share:0.456},{key:'7+',count:1086,share:0.0335}],all:[{key:'0',count:30000,share:0.425}]},
  concerns:[{concern:'work_business',all:15289,citizen:6291,citizen_yes:557,citizen_no:953,citizen_undecided:2872,citizen_unrelated:1909}]},
 '/api/strategy/rules':{generated_at:'2026-10-22T11:00:00Z',read_only:true,stale:false,
  mix:[{key:'originals_per_reply',title:'原创与回复配比',value:2,text:'除此之外，每个角色每写 2 条原创，配 1 条回复'}],
  targeting:[{key:'max_reply_depth',title:'线程层数上限',value:7,text:'原帖是第 0 层；对方帖子在第 7 层或更深时不回复'}],
  public_figures:{rule:'原创帖 @ 一位公众人物',figures:[{username:'DrAmaraSingh',who:'Dr Amara Singh, GP',how:'thank her',first_for:['health'],second_for:['economy'],concerns:['hospital']}]},
  schedule:{timezone:'Australia/Sydney',custom:false,peak_now:false,publish_grace_seconds:900,peaks:[{start:'06:00',end:'08:00'}],unscheduled:['Loose Role'],
   groups:[{group:'B',members:[{account_id:5832,name:'Ethan Brooks'}],on_shift:true,current_limit:4,hour_limits:[4,4,4,4,4,4,null,null,null,null,null,null,8,8,6,6,6,null,null,null,null,4,4,4]},
    {group:'A',members:[{account_id:5554,name:'Maya Collins'}],on_shift:false,current_limit:null,hour_limits:Array(24).fill(null)}]},
  allowed_actions:{roles_enabled:15,counts:{post:15,reply:15}},recent_mix:{actions:50,posts:17,replies:33,post_share:0.34,originals_per_reply:0.52}},
 '/api/settings/summary':{generated_at:'2026-10-22T11:00:00Z',read_only:true,stale:false,
  team:{enabled:true,team_side:'Vote Yes',max_accounts:15,global_interval_seconds:25,block_agent_interactions:true},
  review:{enabled:false,provider_id:null},fact_bank:{review_mode:'auto',extract_enabled:true,pipeline:'staged',provider_name:'openrouter1'},
  reading_selection:{provider_name:'openroute'},providers:{count:2,default:{provider_name:'openroute',provider_model:'deepseek'}},
  summary:{roles:2,enabled:2,reading_modes:{rules:2}},status:{ai:{default_ok:true},collection:{scheduler_enabled:true,healthy:true},agents:{maintenance:{active:false}}},
  roles:[{id:'r1',name:'Maya Collins',username:'maya_collins',account_id:5554,enabled:true,group:'A',reading_selection_mode:'rules',provider_name:'openroute',provider_missing:false,allowed_actions:['post','reply'],daily_limit:6,interval_seconds:1800},
   {id:'r2',name:'Simon Mercer',username:'simon_mercer',account_id:6503,enabled:true,group:'B',reading_selection_mode:'rules',provider_id:'gone',provider_missing:true,has_error:true,allowed_actions:['post','reply'],daily_limit:5,interval_seconds:1800}]},
 '/api/maintenance/history':{generated_at:'2026-10-22T11:00:00Z',read_only:true,stale:false,
  state:{active:false,phase:'completed',reason:'服务更新，正在等待已有任务结束',started_at:'2026-10-22T10:36:38Z',finished_at:'2026-10-22T10:39:18Z'},
  history_summary:{reloads:2,results:{ready:1,aborted_existing_backend_kept:1},last_ready_at:'2026-10-22T10:39:18Z'},
  history:[{name:'unified-20261022n',started_at:'2026-10-22T10:36:21Z',purpose:'Unified reload 13: shared snapshot',result:'ready',ok:true,drain_seconds:132.3,total_seconds:176.9,finished_at:'2026-10-22T10:39:18Z'},
   {name:'unified-20261022i',started_at:'2026-10-22T08:28:06Z',purpose:'Unified reload 9: account identification',result:'aborted_existing_backend_kept',failed_stage:'drain_existing_work',ok:false,total_seconds:315,finished_at:'2026-10-22T08:33:21Z'}],
  events:[{at:'2026-10-22T10:39:18Z',kind:'maintenance_completed',message:'服务更新已完成，已恢复原运行设置'}],
  retention:{retention_days:30,interval_seconds:300,last_result:{counts:{api_call_logs:12}}},
  backups:[{name:'ctn-pg-20261022T100000000000Z.sqlite3',bytes:2500000,url:'/api/backups/ctn-pg-20261022T100000000000Z.sqlite3',modified_at:'2026-10-22T10:00:00Z'}]},
};

function setup(page:ReactElement,path:string) {
 const fetch=vi.fn(async(input:RequestInfo|URL,_options?:RequestInit)=>{
  const url=new URL(String(input),'http://local');
  return new Response(JSON.stringify(payloads[url.pathname]??{error:'not found'}),{status:url.pathname in payloads?200:404,headers:{'Content-Type':'application/json'}});
 });
 vi.stubGlobal('fetch',fetch);
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={[path]}>{page}</MemoryRouter></App></QueryClientProvider>);
 return fetch;
}
const onlyReads=(fetch:ReturnType<typeof setup>,endpoint:string)=>
 expect(fetch.mock.calls.every(([input,options])=>String(input).startsWith(endpoint)&&!(options as RequestInit|undefined)?.method)).toBe(true);

describe('stage pages',()=>{
 it('replaces every placeholder with its own page and keeps a sidebar entry for each',()=>{
  expect(stagePlans).toEqual({});
  for(const path of ['/processing','/strategy','/agents/pipeline','/results/attribution','/results/exposure','/settings','/system/maintenance'])expect(matchNav(path)?.to,path).toBe(path);
 });

 it('shows official engagement per official hour next to our output, with the outage marked',async()=>{
  const fetch=setup(<Exposure/>,'/results/exposure');
  expect(await screen.findByText('平台故障：10 月 22 日悉尼时间约 16:00–18:36 计数冻结')).toBeTruthy();
  expect(screen.getByText('每条原创带来的浏览')).toBeTruthy();
  expect(screen.getByRole('region',{name:'每个官方小时的浏览和我方发布'})).toBeTruthy();
  const rows=screen.getAllByRole('row');
  const reported=rows.find(row=>within(row).queryByText('91'))!;
  expect(within(reported).getByText('已报告')).toBeTruthy();
  expect(within(reported).getByText('2 份报告')).toBeTruthy();
  expect(within(reported).getByText('向我方 0 · 中立 0 · 远离 1')).toBeTruthy();
  expect(screen.getByText('平台故障')).toBeTruthy();
  expect(screen.getByText('未导入')).toBeTruthy();
  expect(screen.getByText('进行中')).toBeTruthy();
  onlyReads(fetch,'/api/results/exposure');
 });

 it('groups settled outcomes and switches to concern × action arms',async()=>{
  const fetch=setup(<Attribution/>,'/results/attribution');
  const reply=(await screen.findByText('回复')).closest('tr') as HTMLElement;
  expect(within(reply).getByText('12 / 14')).toBeTruthy();
  expect(within(reply).getByText('25%')).toBeTruthy();
  expect(screen.getByText('回报是代理指标：官方的说服只按队伍和小时公布，不能落到单条帖子')).toBeTruthy();
  fireEvent.click(screen.getByText('顾虑 × 动作'));
  expect(await screen.findByText('医院承载')).toBeTruthy();
  expect(screen.getByText('参与排序')).toBeTruthy();
  expect(screen.getByText('不足 10 条')).toBeTruthy();
  const latest=screen.getAllByRole('link').find(link=>link.getAttribute('href')==='/posts?post=200');
  expect(latest).toBeTruthy();
  onlyReads(fetch,'/api/results/attribution');
 });

 it('shows each pipeline step with durations, errors and the failure reasons',async()=>{
  const fetch=setup(<AgentPipeline/>,'/agents/pipeline');
  const decide=(await screen.findByText('2. 决策')).closest('tr') as HTMLElement;
  expect(within(decide).getByText('225 / 10')).toBeTruthy();
  expect(within(decide).getByText('21.0 秒')).toBeTruthy();
  expect(within(decide).getByText('输出被截断 9 · 模型服务出错 1')).toBeTruthy();
  const write=screen.getByText('3. 写作').closest('tr') as HTMLElement;
  expect(within(write).getByText('输出被截断 12 · 只返回推理、没有正文 2')).toBeTruthy();
  const publish=screen.getByText('5. 发布').closest('tr') as HTMLElement;
  expect(within(publish).getByText('不调用模型')).toBeTruthy();
  expect(within(publish).getByText('8.1 分钟')).toBeTruthy();
  expect(screen.getByText('AI 正文两次未通过方向与事实检查，本轮未创建动作 · 48 次')).toBeTruthy();
  expect(screen.getByText('You copied a fact word for word.（3）')).toBeTruthy();
  expect(screen.getByRole('link',{name:'Maya Collins'}).getAttribute('href')).toBe('/ai/agents/r1');
  onlyReads(fetch,'/api/pipeline/steps');
 });

 it('shows annotation progress and switches the distributions between citizens and all posts',async()=>{
  const fetch=setup(<Processing/>,'/processing');
  expect(await screen.findByText('积压 205 条')).toBeTruthy();
  expect(screen.getByText('11,800 · 36.4%')).toBeTruthy();
  expect(screen.getByText('第 7 层及更深')).toBeTruthy();
  expect(screen.getByText('工作与生意')).toBeTruthy();
  fireEvent.click(screen.getByText('全部帖子'));
  expect(await screen.findByText('27,333 · 38.8%')).toBeTruthy();
  onlyReads(fetch,'/api/processing/annotations');
 });

 it('lists the rules in force, the public figures and the shift grid',async()=>{
  const fetch=setup(<Strategy/>,'/strategy');
  expect(await screen.findByText('原创与回复配比')).toBeTruthy();
  expect(screen.getByText('原帖是第 0 层；对方帖子在第 7 层或更深时不回复')).toBeTruthy();
  expect(screen.getByText('@DrAmaraSingh')).toBeTruthy();
  expect(screen.getByText('医院承载')).toBeTruthy();
  expect(screen.getByText('在班 · 每小时 4 条')).toBeTruthy();
  expect(screen.getByText('休息')).toBeTruthy();
  expect(screen.getByRole('img',{name:'在班 14 个小时'})).toBeTruthy();
  expect(screen.getByText(/原创 17、回复 33/)).toBeTruthy();
  onlyReads(fetch,'/api/strategy/rules');
 });

 it('indexes the settings with their edit locations and flags a role whose connection is gone',async()=>{
  const fetch=setup(<SettingsIndex/>,'/settings');
  expect(await screen.findByText('1 个角色选的 AI 连接已不存在')).toBeTruthy();
  expect(screen.getByRole('link',{name:'在「队伍设置」修改'}).getAttribute('href')).toBe('/ai/agents?tab=settings');
  expect(screen.getByRole('link',{name:'在「AI 连接」修改'}).getAttribute('href')).toBe('/ai/settings');
  const simon=screen.getByRole('link',{name:'Simon Mercer'}).closest('tr') as HTMLElement;
  expect(within(simon).getByText('连接已不存在')).toBeTruthy();
  expect(within(simon).getByText('有报错')).toBeTruthy();
  expect(screen.getByText('分步流程（决策 → 写作 → 检查）')).toBeTruthy();
  onlyReads(fetch,'/api/settings/summary');
 });

 it('shows maintenance state and reload history without any control that acts',async()=>{
  const fetch=setup(<Maintenance/>,'/system/maintenance');
  expect(await screen.findByText('正常运行')).toBeTruthy();
  expect(screen.getByText('Unified reload 9: account identification')).toBeTruthy();
  expect(screen.getByText('中止，保留原后端')).toBeTruthy();
  expect(screen.getByText('停在 drain_existing_work')).toBeTruthy();
  expect(screen.getByText('2.2 分钟')).toBeTruthy();
  expect(screen.getByRole('link',{name:'ctn-pg-20261022T100000000000Z.sqlite3'}).getAttribute('href')).toBe('/api/backups/ctn-pg-20261022T100000000000Z.sqlite3');
  expect(screen.getByText(/业务数据不会自动删除/)).toBeTruthy();
  await waitFor(()=>expect(screen.queryAllByRole('button')).toEqual([]));
  onlyReads(fetch,'/api/maintenance/history');
 });
});
