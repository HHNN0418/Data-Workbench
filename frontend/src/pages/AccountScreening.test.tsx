// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, configure, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter} from 'react-router-dom';
import AccountScreening from './AccountScreening';

vi.mock('antd',async importOriginal=>{
 const original=await importOriginal<typeof import('antd')>();
 return {...original,Select:({options,value,onChange,id,disabled,'aria-label':label}:any)=><select id={id} aria-label={label} disabled={disabled} value={value??''} onChange={event=>onChange(event.target.value)}>{(options||[]).map((option:any)=><option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}</select>};
});
beforeAll(()=>{
 configure({asyncUtilTimeout:10000});
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 const originalStyle=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>originalStyle(element));
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn().mockImplementation(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;sessionStorage.clear();vi.unstubAllGlobals();});
const account={account_id:21,username:'check_this_account',verified:true,known_post_count:30,score:67,risk_priority_score:67,band:'优先复核',data_observed:{posts:12,timestamps:12,observed_span_hours:6,original_text_posts:10},signals:[{key:'duplicates',title:'重复内容',points:30,max_points:35,measured:'10 篇原文中重复 8 篇',post_ids:[1001]},{key:'cross_account_exact_text',title:'跨账号同文同步',points:20,max_points:20,measured:'同一时间窗口内三个账号发布相同正文',post_ids:[1001],measurements:{coordination_examples:[{post_id:1001,peer_account_ids:[31,32],peer_post_ids:[2001,2002],window_start:'2026-10-20T00:00:00Z',window_end:'2026-10-20T00:02:00Z'}]}}],examples:[{post_id:1001,text:'这里是用于复核的原帖样本。',created_at:'2026-10-20T00:00:00Z'}],account_report_eligibility:{status:'unknown',reason:'尚未取得官方隐藏记录与操作次数。'}};
const scanData={items:[account,{...account,account_id:22,username:'too_little_data',score:null,risk_priority_score:null,band:'资料不足',signals:[],examples:[],data_observed:{posts:1,timestamps:1,observed_span_hours:0}}],summary:{source_post_count:300,scanned_post_count:100,scanned_account_count:12,matched_account_count:2,insufficient_account_count:1,truncated:false},methodology:{version:'screening-v1'},caveats:['只根据已采集帖子计算，需核对原帖。']};
const judgment={account_id:21,judgment:{protocol:'typesafe',model:'jev-version',uncalibrated_for_ctn:true,answers:{evidence_sufficiency:{type:'choice',choice:'adequate',probabilities:{adequate:0.8},confidence:0.8},automation_evidence:{type:'choice',choice:'moderate'},coordinated_activity:{type:'choice',choice:'insufficient'}}}};
function setup({protocol,params={own:'all',window_minutes:30},data=scanData,scanResponse,reviewJob}:{protocol?:string;params?:Record<string,unknown>;data?:object;scanResponse?:()=>Promise<Response>;reviewJob?:Record<string,unknown>}={}){
 const provider={id:'review-provider',name:'复核服务',model:'review-model',protocol:protocol||'openai_chat',base_url:'https://review.example.com/v1',has_key:true,enabled:true,is_default:true};
 const job={id:'account-review-job',kind:'account_review',state:'completed',stage:'complete',elapsed_seconds:185,result:judgment,...reviewJob};
 const fetch=vi.fn((input:RequestInfo|URL,options?:RequestInit)=>{
  const path=String(input);
  if(path==='/api/ai/account-screening'&&scanResponse)return scanResponse();
  if(path==='/api/ai/jobs/account-review-job/cancel'&&options?.method==='POST')Object.assign(job,{state:'cancelled',result:null});
  const response=path==='/api/bootstrap'?{csrf_token:'csrf-test'}:path==='/api/ai/providers'?{providers:protocol?[provider]:[]}:path.startsWith('/api/ai/jobs?')?{items:[]}:path==='/api/ai/account-review'||path.startsWith('/api/ai/jobs/account-review-job')?job:data;
  return Promise.resolve(new Response(JSON.stringify(response),{status:path==='/api/ai/account-review'?202:200,headers:{'Content-Type':'application/json'}}));
 });
 vi.stubGlobal('fetch',fetch);
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 const tree=(scope:Record<string,unknown>)=><QueryClientProvider client={client}><MemoryRouter><AccountScreening params={scope}/></MemoryRouter></QueryClientProvider>;
 const view=render(tree(params));
 return {fetch,rerender:(scope:Record<string,unknown>)=>view.rerender(tree(scope))};
}
function posted(fetch:ReturnType<typeof vi.fn>,path:string){return fetch.mock.calls.filter(([url])=>url===path).map(([,options])=>JSON.parse(String(options.body)));}

describe('account screening workflow',()=>{
 it('scans only on request and exposes measured evidence with original post links',async()=>{
  const {fetch}=setup();await screen.findByLabelText('可选 AI 复核服务');
  expect(posted(fetch,'/api/ai/account-screening')).toHaveLength(0);expect(posted(fetch,'/api/ai/account-review')).toHaveLength(0);
  fireEvent.click(screen.getByRole('button',{name:'扫描账号'}));expect(await screen.findByText('@check_this_account')).toBeTruthy();
  expect(posted(fetch,'/api/ai/account-screening')).toEqual([{params:{own:'all'},limit:100,min_score:0}]);
  expect(screen.getByText('67')).toBeTruthy();expect(screen.getByText('优先复核')).toBeTruthy();expect(screen.getByText('样本不足，暂不评分')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'查看账号 21 依据'}));expect(await screen.findByText('10 篇原文中重复 8 篇')).toBeTruthy();
  expect(screen.getAllByRole('link',{name:'原帖 #1001 ↗'})[0].getAttribute('href')).toBe('/posts?post=1001');
  expect(screen.getByRole('link',{name:'关联账号 #31 ↗'}).getAttribute('href')).toBe('/posts?author_id=31');expect(screen.getByRole('link',{name:'关联原帖 #2002 ↗'}).getAttribute('href')).toBe('/posts?post=2002');
  expect(screen.getByText('尚未取得官方隐藏记录与操作次数。',{exact:false})).toBeTruthy();expect(posted(fetch,'/api/ai/account-review')).toHaveLength(0);
 },30000);
 it('clears obsolete results when limit or score controls change',async()=>{
  const {fetch}=setup();fireEvent.click(screen.getByRole('button',{name:'扫描账号'}));await screen.findByText('@check_this_account');
  fireEvent.change(screen.getByLabelText('筛查账号上限'),{target:{value:'200'}});await waitFor(()=>expect(screen.queryByText('@check_this_account')).toBeNull());
  fireEvent.change(screen.getByLabelText('最低排查分值'),{target:{value:'40'}});fireEvent.click(screen.getByRole('button',{name:'扫描账号'}));await screen.findByText('@check_this_account');
  expect(posted(fetch,'/api/ai/account-screening').at(-1)).toMatchObject({limit:200,min_score:40});expect(posted(fetch,'/api/ai/account-review')).toHaveLength(0);
 },30000);
 it('preserves own scope so the server can explain the unconditional own-account exclusion',async()=>{
  const {fetch}=setup({params:{own:'own',author_id:'7'},data:{...scanData,items:[],summary:{...scanData.summary,matched_account_count:0,reason:'own_scope_excluded'}}});
  fireEvent.click(screen.getByRole('button',{name:'扫描账号'}));expect(await screen.findByText('本队账号不参与筛查，可切换上方账号范围后重新扫描。')).toBeTruthy();
  expect(posted(fetch,'/api/ai/account-screening')[0].params).toEqual({author_id:'7',own:'own'});
 },30000);
 it('ignores a pending scan after the account scope changes',async()=>{
  let finish:(response:Response)=>void=()=>{};
  const {fetch,rerender}=setup({scanResponse:()=>new Promise(resolve=>{finish=resolve;})});fireEvent.click(screen.getByRole('button',{name:'扫描账号'}));
  await waitFor(()=>expect(posted(fetch,'/api/ai/account-screening')).toHaveLength(1));rerender({own:'all',author_id:'9'});finish(new Response(JSON.stringify(scanData),{status:200}));
  await waitFor(()=>expect((screen.getByRole('button',{name:/扫描账号/}) as HTMLButtonElement).disabled).toBe(false));expect(screen.queryByText('@check_this_account')).toBeNull();
 },30000);
 it.each(['openai_chat','typesafe'])('uses an optional %s model only after the per-account review action',async protocol=>{
  const {fetch}=setup({protocol});await waitFor(()=>expect((screen.getByLabelText('可选 AI 复核服务') as HTMLSelectElement).value).toBe('review-provider'));
  fireEvent.click(screen.getByRole('button',{name:'扫描账号'}));await screen.findByText('@check_this_account');expect(posted(fetch,'/api/ai/account-review')).toHaveLength(0);
  fireEvent.click(screen.getAllByRole('button',{name:'AI 复核'})[0]);expect(await screen.findByText('AI 复核结果')).toBeTruthy();
  expect(posted(fetch,'/api/ai/account-review')).toEqual([{params:{own:'all'},account_id:21,provider_id:'review-provider'}]);
  expect(screen.getByText('数据充分度')).toBeTruthy();expect(screen.getByText('充分')).toBeTruthy();expect(screen.getByText('中等')).toBeTruthy();expect(screen.queryByText('0.8')).toBeNull();
 },30000);
 it('shows waiting progress beyond the old deadline and allows explicit review cancellation',async()=>{
  const {fetch}=setup({protocol:'typesafe',reviewJob:{state:'running',stage:'thinking',elapsed_seconds:185,result:null}});
  await waitFor(()=>expect((screen.getByLabelText('可选 AI 复核服务') as HTMLSelectElement).value).toBe('review-provider'));
  fireEvent.click(screen.getByRole('button',{name:'扫描账号'}));await screen.findByText('@check_this_account');
  fireEvent.click(screen.getAllByRole('button',{name:'AI 复核'})[0]);
  expect(await screen.findByText('模型正在思考')).toBeTruthy();expect(screen.getByText(/已等待 3 分 5 秒/)).toBeTruthy();
  expect((screen.getByRole('button',{name:'扫描账号'}) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByText('AI 复核结果')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'取消账号复核'}));
  await waitFor(()=>expect(posted(fetch,'/api/ai/jobs/account-review-job/cancel')).toEqual([{}]));
  expect(await screen.findByText('AI 任务已取消')).toBeTruthy();
  expect(screen.queryByText('AI 复核结果')).toBeNull();
 },30000);
});
