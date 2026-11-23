// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {act, cleanup, configure, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter, Route, Routes, useLocation, useSearchParams} from 'react-router-dom';
import Analysis, {AnalysisResults} from './Analysis';
import AIContentAnalysis from './AIContentAnalysis';
import {useApi} from '../api';
import {QueryState} from '../common';
import {analysisRequestParams, readAnalysisParams, updateAnalysisSearch} from './analysisModel';

vi.mock('antd',async importOriginal=>{
 const original=await importOriginal<typeof import('antd')>();
 return {...original,Collapse:()=>null,Select:({options,value,onChange,'aria-label':label}:any)=><select aria-label={label} value={value??''} onChange={event=>onChange(/^\d+$/.test(event.target.value)?Number(event.target.value):event.target.value)}><option value="">请选择</option>{(options||[]).map((option:any)=><option key={option.value} value={option.value}>{option.label}</option>)}</select>,Drawer:({open,title,children}:any)=>open?<aside role="dialog" aria-label={title}>{children}</aside>:null,Table:({dataSource,columns,pagination,rowClassName}:any)=><table><tbody>{(pagination?dataSource.slice(((pagination.current||1)-1)*pagination.pageSize,(pagination.current||1)*pagination.pageSize):dataSource).map((row:any,index:number)=><tr key={row.post_id??index} className={rowClassName?.(row)}>{columns.map((column:any,columnIndex:number)=><td key={column.key??column.dataIndex??columnIndex}>{column.render?column.render(row[column.dataIndex],row,index):row[column.dataIndex]}</td>)}</tr>)}</tbody></table>};
});

vi.mock('../common',async importOriginal=>{
 const original=await importOriginal<typeof import('../common')>();
 return {...original,Chart:({ariaLabel,onEvents}:any)=><section aria-label={ariaLabel}>{ariaLabel==='热门排名轨迹'?<button onClick={()=>onEvents.click({data:{post_id:2}})}>轨迹选择 #2</button>:ariaLabel==='回复传播树'?<button onClick={()=>onEvents.click({data:{post_id:3}})}>树选择 #3</button>:ariaLabel==='采样覆盖漏斗'?<><button onClick={()=>onEvents.click({data:{stage:'early_baseline'}})}>漏斗阶段</button><button onClick={()=>onEvents.click({data:{status:'missing_metrics'}})}>漏斗原因</button></>:ariaLabel==='采样窗口覆盖热力图'?<button onClick={()=>onEvents.click({data:{batch:'2026-10-19T00:00:00Z',minutes:30}})}>覆盖单元</button>:null}</section>};
});
beforeAll(()=>{
 configure({asyncUtilTimeout:10000});
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 const originalStyle=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>originalStyle(element));
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn().mockImplementation(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
function Location(){const location=useLocation();return <output data-testid="location" data-pathname={location.pathname} data-hash={location.hash}>{location.search}</output>;}
function Harness(){const [search,setSearch]=useSearchParams(),params=readAnalysisParams(search),query=useApi('/analysis',analysisRequestParams(params),60000,true);return <QueryState query={query}>{data=><AnalysisResults data={data} params={params} updating={query.isFetching} onUpdate={values=>setSearch(updateAnalysisSearch(search,values))}/>}</QueryState>;}
const fixture={generated_at:'2026-10-19T00:00:00Z',parameters:{bucket_timezone:'Australia/Sydney'},summary:{posts:3},quality:{freshness_reference:'2026-10-19T00:00:00Z'},trending:{summary:{posts:2,observations:3,repeated_posts:1},trajectories:[{post_id:2,username:'Alice',body_preview:'热榜正文',first_rank:10,last_rank:2,best_rank:2,observations:2,points:[{observed_at:'2026-10-19T00:00:00Z',rank:2}]}]},replies:{summary:{threads:1,reply_posts:1,participants:2,max_depth:1},threads:[{root_id:2,username:'Alice',posts:2,max_depth:1}],selected_root_id:2,total_nodes:2,nodes:[{post_id:2,username:'Alice',depth:0},{post_id:3,parent_id:2,username:'Bob',body_preview:'回复正文',depth:1}]},sampling:{stages:[{key:'total',label:'全部帖子',count:3},{key:'early_baseline',label:'早期基线',count:2}],statuses:[{key:'eligible',label:'合资格',count:1},{key:'missing_metrics',label:'字段不全',count:2}],posts_total:3,posts:[{post_id:2,username:'Alice',body_preview:'热榜正文',status:'eligible'},{post_id:3,username:'Bob',body_preview:'回复正文',status:'missing_metrics'}]}};
function setup(initial='/analysis?analysis_tab=deep&own=own&window_minutes=30',analysisData=fixture,fullPage=false){
 const fetch=vi.fn((input:RequestInfo|URL,_options?:RequestInit)=>Promise.resolve(new Response(JSON.stringify(String(input)==='/api/bootstrap'?{csrf_token:'test-token'}:String(input)==='/api/ai/providers'?{providers:[],default_provider_id:null}:String(input).startsWith('/api/posts/')?{post:{post_id:Number(String(input).split('/').at(-1)),root_post_id:2,body:'已读取的原帖正文',username:'Alice'},counters:[]}:analysisData),{status:200,headers:{'Content-Type':'application/json'}})));
 vi.stubGlobal('fetch',fetch);
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[initial]}>{fullPage?<Routes><Route path="/analysis" element={<Analysis/>}/><Route path="/ai/analysis" element={<AIContentAnalysis/>}/></Routes>:<Harness/>}<Location/></MemoryRouter></QueryClientProvider>);
 return fetch;
}

describe('analysis cross-chart selection',()=>{
 it('immediately refreshes unchanged parameters when requested and reuses the returned result',async()=>{
  const fetch=setup('/analysis?own=own',fixture,true);
  await screen.findByRole('tab',{name:'趋势'});
  expect(fetch.mock.calls.filter(([path])=>String(path).startsWith('/api/analysis?'))).toHaveLength(1);
  fireEvent.click(screen.getByRole('button',{name:'更新分析'}));
  await waitFor(()=>expect(fetch.mock.calls.filter(([path])=>String(path).includes('refresh=true'))).toHaveLength(1));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toContain('window_minutes=60'));
  expect(fetch.mock.calls.filter(([path])=>String(path).startsWith('/api/analysis?'))).toHaveLength(2);
 },30000);
 it('keeps charts visible while updating and after a background update fails',async()=>{
  const fetch=setup('/analysis',fixture,true);
  await screen.findByRole('tab',{name:'趋势'});
  expect(screen.getByText('页面每分钟自动更新分析；点击「更新分析」可立即纳入最新数据。')).toBeTruthy();
  expect(screen.getByText(/分析更新于/)).toBeTruthy();
  let complete!:(value:Response)=>void;
  const pending=new Promise<Response>(resolve=>{complete=resolve;});
  fetch.mockImplementationOnce(()=>pending);
  let reload!:Promise<void>;
  await act(async()=>{reload=clients.at(-1)!.invalidateQueries({queryKey:['/analysis']});});
  expect(await screen.findByText('正在更新分析…')).toBeTruthy();
  expect(screen.getByRole('tab',{name:'趋势'})).toBeTruthy();
  expect(screen.getByText(/分析更新于/)).toBeTruthy();
  await act(async()=>{complete(new Response(JSON.stringify({error:'采集服务暂时不可用'}),{status:503,headers:{'Content-Type':'application/json'}}));await reload;});
  expect(await screen.findByText('自动更新失败，暂时显示上次分析结果')).toBeTruthy();
  expect(screen.getByRole('tab',{name:'趋势'})).toBeTruthy();
  expect(screen.getByText(/分析更新于/)).toBeTruthy();
  expect(screen.queryByText('正在更新分析…')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:/重\s*试/}));
  await waitFor(()=>expect(screen.queryByText('自动更新失败，暂时显示上次分析结果')).toBeNull());
 },30000);
 it('labels both the analysis update time and latest collection time',async()=>{
  setup('/analysis',{...fixture,quality:{...fixture.quality,last_counter_observed_at:'2026-10-19T01:00:00Z'}} as typeof fixture,true);
  expect(await screen.findByText('分析更新于 2026/10/19 11:00')).toBeTruthy();
  expect(screen.getByText('最近采集于 2026/10/19 12:00')).toBeTruthy();
 },30000);
 it('refreshes a changed scope once before showing the saved result for that scope',async()=>{
  const fetch=setup('/analysis?own=own',fixture,true);
  await screen.findByRole('tab',{name:'趋势'});
  fireEvent.change(screen.getByLabelText('作者 ID'),{target:{value:'44'}});
  fireEvent.click(screen.getByRole('button',{name:'更新分析'}));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toContain('author_id=44'));
  const calls=fetch.mock.calls.filter(([path])=>String(path).startsWith('/api/analysis?'));
  expect(calls).toHaveLength(2);
  expect(String(calls[1][0])).toContain('author_id=44');
  expect(String(calls[1][0])).toContain('refresh=true');
 },30000);
 it('keeps the selected tab when an earlier manual refresh finishes',async()=>{
  const fetch=setup('/analysis?own=own',fixture,true);
  await screen.findByRole('tab',{name:'趋势'});
  let complete!:(value:Response)=>void;
  const pending=new Promise<Response>(resolve=>{complete=resolve;});
  fetch.mockImplementation(input=>String(input).includes('refresh=true')?pending:Promise.resolve(new Response(JSON.stringify(fixture),{status:200,headers:{'Content-Type':'application/json'}})));
  fireEvent.click(screen.getByRole('button',{name:'更新分析'}));
  await waitFor(()=>expect(fetch.mock.calls.some(([path])=>String(path).includes('refresh=true'))).toBe(true));
  fireEvent.click(screen.getByRole('tab',{name:'深度分析'}));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toContain('analysis_tab=deep'));
  await act(async()=>{complete(new Response(JSON.stringify(fixture),{status:200,headers:{'Content-Type':'application/json'}}));await pending;});
  expect(screen.getByTestId('location').textContent).toContain('analysis_tab=deep');
  expect(screen.getByRole('tab',{name:'深度分析'}).getAttribute('aria-selected')).toBe('true');
 },30000);
 it('keeps the saved result and selected scope when a manual refresh fails',async()=>{
  const fetch=setup('/analysis?own=own',fixture,true);
  await screen.findByRole('tab',{name:'趋势'});
  fetch.mockImplementationOnce(()=>Promise.resolve(new Response(JSON.stringify({error:'更新失败'}),{status:500,headers:{'Content-Type':'application/json'}})));
  fireEvent.change(screen.getByLabelText('作者 ID'),{target:{value:'44'}});
  fireEvent.click(screen.getByRole('button',{name:'更新分析'}));
  expect(await screen.findByText('更新失败')).toBeTruthy();
  expect(screen.getByRole('tab',{name:'趋势'})).toBeTruthy();
  expect(screen.getByTestId('location').textContent).not.toContain('author_id=44');
 },30000);
 it('moves legacy AI links into the workflow while preserving scope and avoiding statistical requests',async()=>{
  const fetch=setup('/analysis?analysis_tab=ai&own=own&author_id=44&ai_view=research&since=2026-10-01#sources',fixture,true);
  await waitFor(()=>expect(screen.getByTestId('location').getAttribute('data-pathname')).toBe('/ai/analysis'));
  const search=new URLSearchParams(screen.getByTestId('location').textContent!);
  expect(Object.fromEntries(search)).toEqual({own:'own',author_id:'44',ai_view:'research',since:'2026-10-01'});
  expect(screen.getByTestId('location').getAttribute('data-hash')).toBe('#sources');
  expect(fetch.mock.calls.some(([url])=>String(url).startsWith('/api/analysis'))).toBe(false);
  expect(screen.queryByLabelText('观察窗口 / 分钟')).toBeNull();
  expect(screen.queryByRole('tab',{name:'趋势'})).toBeNull();
 },30000);
 it('saves and exports the same selected batch and sampling filters as the page',async()=>{
  const fetch=setup('/analysis?analysis_tab=deep&deep_view=coverage&own=own&sample_batch=2026-10-19T00%3A00%3A00Z&sample_status=eligible&focus_post_id=2',fixture,true);
  Object.defineProperty(URL,'createObjectURL',{configurable:true,value:vi.fn(()=> 'blob:analysis')});
  Object.defineProperty(URL,'revokeObjectURL',{configurable:true,value:vi.fn()});
  const anchor=vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(()=>{});
  fireEvent.click(await screen.findByRole('button',{name:'保存快照'}));
  await waitFor(()=>expect(fetch.mock.calls.some(([path])=>path==='/api/analysis/run')).toBe(true));
  const saved=JSON.parse(String(fetch.mock.calls.find(([path])=>path==='/api/analysis/run')?.[1]?.body));
  expect(saved).toMatchObject({sample_batch:'2026-10-19T00:00:00Z',sample_status:'eligible',focus_post_id:2,own:'own',sections:'all'});
  fireEvent.click(await screen.findByRole('button',{name:'导出结果 JSON'}));
  await waitFor(()=>expect(anchor).toHaveBeenCalled());
  const exported=new URLSearchParams(String(fetch.mock.calls.find(([path])=>String(path).includes('sections=all'))?.[0]).split('?')[1]);
  for(const key of ['sample_batch','sample_status','focus_post_id','own','window_minutes','tolerance_minutes'])expect(exported.get(key)).toBe(String(saved[key]));
  anchor.mockRestore();
 },30000);
 it('fetches detail sections on navigation and all sections only when exporting',async()=>{
  const fetch=setup('/analysis?own=own&author_id=44');
  Object.defineProperty(URL,'createObjectURL',{configurable:true,value:vi.fn(()=> 'blob:analysis')});
  Object.defineProperty(URL,'revokeObjectURL',{configurable:true,value:vi.fn()});
  const anchor=vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(()=>{});
  await screen.findByRole('tab',{name:'深度分析'});
  expect(String(fetch.mock.calls[0][0])).toContain('sections=core');
  expect(String(fetch.mock.calls[0][0])).toContain('include_exportable=false');
  fireEvent.click(screen.getByRole('tab',{name:'深度分析'}));
  await waitFor(()=>expect(fetch.mock.calls.some(call=>String(call[0]).includes('sections=trending'))).toBe(true));
  fireEvent.click(screen.getByRole('button',{name:'导出结果 JSON'}));
  await waitFor(()=>expect(anchor).toHaveBeenCalled());
  const full=fetch.mock.calls.find(call=>String(call[0]).includes('sections=all'));
  expect(String(full?.[0])).toContain('own=own');expect(String(full?.[0])).toContain('author_id=44');
  anchor.mockRestore();
 },30000);
 it('keeps the seven original tabs visible and adds deep analysis on the right',async()=>{
  setup('/analysis');
  await screen.findByRole('tab',{name:'深度分析'});
  expect(screen.getAllByRole('tab').map(tab=>tab.textContent)).toEqual(['趋势','账号对比','窗口增长','相关性','词频','互动关系','数据质量','深度分析']);
  expect(screen.getByRole('tab',{name:'趋势'}).getAttribute('aria-selected')).toBe('true');
  expect(screen.queryByText(/更多分析：/)).toBeNull();
  fireEvent.click(screen.getByRole('tab',{name:'深度分析'}));
  expect(await screen.findByText('热门排名分布')).toBeTruthy();
  fireEvent.click(screen.getByRole('tab',{name:'数据质量'}));
  expect(screen.getByRole('tab',{name:'数据质量'}).getAttribute('aria-selected')).toBe('true');
 },30000);

 it('preserves shared focus across views and loads the selected post evidence',async()=>{
  const fetch=setup();
  fireEvent.click(await screen.findByRole('button',{name:'轨迹选择 #2'}));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toContain('focus_post_id=2'));
  expect(screen.getByTestId('location').textContent).toContain('own=own');expect(screen.getByTestId('location').textContent).toContain('window_minutes=30');
  expect(screen.queryByRole('dialog')).toBeNull();
  fireEvent.click(screen.getByText('回复传播',{exact:true}));
  fireEvent.click(await screen.findByRole('button',{name:'树选择 #3'}));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toContain('focus_post_id=3'));
  fireEvent.click(await screen.findByRole('button',{name:'查看原帖'}));
  expect(await screen.findByText('已读取的原帖正文')).toBeTruthy();expect(fetch.mock.calls.some(call=>call[0]==='/api/posts/3')).toBe(true);
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button',{name:'#2'}));
  await waitFor(()=>expect(fetch.mock.calls.some(call=>call[0]==='/api/posts/2')).toBe(true));
  expect(screen.getByRole('dialog').getAttribute('aria-label')).toContain('#2');
 },30000);
 it('filters stages and reasons exclusively while retaining chart scope and supports clearing',async()=>{
  setup('/analysis?analysis_tab=deep&deep_view=coverage&author_id=44&focus_post_id=2');
  fireEvent.click(await screen.findByRole('button',{name:'漏斗阶段'}));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toContain('sample_stage=early_baseline'));
  expect(screen.getByTestId('location').textContent).toContain('focus_post_id=2');expect(screen.getByTestId('location').textContent).toContain('author_id=44');
  fireEvent.click(await screen.findByRole('button',{name:'漏斗原因'}));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toContain('sample_status=missing_metrics'));
  expect(screen.getByTestId('location').textContent).not.toContain('sample_stage');
  fireEvent.click(await screen.findByRole('button',{name:'清除采样筛选'}));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).not.toContain('sample_status'));
  expect(screen.getByTestId('location').textContent).toContain('focus_post_id=2');
  fireEvent.click(await screen.findByRole('button',{name:'清除选择'}));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).not.toContain('focus_post_id'));
  expect(screen.getByTestId('location').textContent).toContain('author_id=44');
 },30000);
 it('selects a coverage batch and window without changing the global time range or focus',async()=>{
  setup('/analysis?analysis_tab=deep&deep_view=coverage&focus_post_id=2&own=own',{...fixture,sampling:{...fixture.sampling,coverage:{cells:[{batch:'2026-10-19T00:00:00Z',minutes:30,eligible:1,mature:2,pending:0,rate:.5}]}}} as typeof fixture);
  fireEvent.click(await screen.findByRole('button',{name:'覆盖单元'}));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toContain('sample_batch='));
  const search=new URLSearchParams(screen.getByTestId('location').textContent!);
  expect(search.get('window_minutes')).toBe('30');expect(search.get('tolerance_minutes')).toBe('6');
  expect(search.get('focus_post_id')).toBe('2');expect(search.get('own')).toBe('own');expect(search.has('since')).toBe(false);
  fireEvent.click(screen.getByText('热门走势',{exact:true}));
  expect(screen.getByTestId('location').textContent).toContain('focus_post_id=2');
 },30000);
 it('moves the sampling table to the focused post instead of hiding it on another page',async()=>{
  const posts=Array.from({length:20},(_,index)=>({post_id:index+1,username:'Focus',body_preview:'page target',status:'eligible'}));
  setup('/analysis?analysis_tab=deep&deep_view=coverage&focus_post_id=11',{...fixture,sampling:{...fixture.sampling,posts,posts_total:20}});
  const selected=await screen.findByRole('button',{name:name=>name.includes('Focus')&&name.includes('#11')&&name.includes('page target')});
  expect(selected.closest('tr')?.className).toBe('analysis-selected-row');
  expect(screen.queryByText('#1',{selector:'small'})).toBeNull();
 },30000);
});
