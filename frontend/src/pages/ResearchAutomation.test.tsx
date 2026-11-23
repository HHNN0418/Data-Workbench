// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter} from 'react-router-dom';
import ResearchAutomation, {type AutomationState} from './ResearchAutomation';

vi.mock('antd',async importOriginal=>{
 const original=await importOriginal<typeof import('antd')>();
 return {...original,Select:({options,value,onChange,'aria-label':label}:any)=><select aria-label={label} value={value??''} onChange={event=>onChange(event.target.value)}><option value="">选择服务</option>{options.map((option:any)=><option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}</select>};
});
beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn().mockImplementation(query=>({matches:false,media:query,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
function setup({configured=true,enabled=false,failedSave=false,running=false,stale=false,interrupted=false}:{configured?:boolean;enabled?:boolean;failedSave?:boolean;running?:boolean;stale?:boolean;interrupted?:boolean}={}){
 let state:AutomationState={settings:{revision:3,enabled,provider_id:enabled?'writer':'',interval_seconds:3600,auto_activate:false,params:{own:'all'},instruction:'',sample_size:100,include_news:true,max_output_tokens:null,mode:'radar'},status:{running:false,last_run_at:null,next_run_at:null,last_result:{outcome:'unchanged'}}};
 if(running)state.status={...state.status,running:true,run_id:'current-round',progress:{stage:'thinking',elapsed_seconds:120,received_bytes:1024}};
 if(stale)state.status={...state.status,running:false,stale:true,lease_healthy:false,run_id:'old-round',progress:{stage:'receiving',started_at:'2026-01-01T00:00:00Z',elapsed_seconds:61,received_bytes:444262}};
 if(interrupted)state.status={...state.status,running:false,interrupted:true,interrupted_run_id:'old-round',last_result:{outcome:'interrupted',run_id:'old-round'}};
 const fetch=vi.fn((input:RequestInfo|URL,options?:RequestInit)=>{
  const path=String(input);let payload:unknown=state,status=200;
  if(path==='/api/ai/providers')payload={providers:configured?[{id:'writer',name:'研究服务',model:'model',base_url:'https://example.com/v1',enabled:true,has_key:true,protocol:'openai_chat'}]:[]};
  else if(path==='/api/bootstrap')payload={csrf_token:'csrf'};
  else if(path==='/api/ai/research/automation/cancel'){state={...state,status:{...state.status,cancel_requested:true}};payload=state;}
  else if(path==='/api/ai/research/automation/retry'){state={...state,status:{...state.status,interrupted:false,interrupted_run_id:null,last_result:{outcome:'retry_pending',run_id:'old-round'}}};payload=state;}
  else if(options?.method==='POST'){
   if(failedSave){payload={error:'配置已更新，请刷新'};status=409;}
   else{const {expected_revision,...settings}=JSON.parse(String(options.body));state={...state,settings:{...settings,revision:expected_revision+1}};payload=state;}
  }
  return Promise.resolve(new Response(JSON.stringify(payload),{status,headers:{'Content-Type':'application/json'}}));
 });
 vi.stubGlobal('fetch',fetch);
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 const view=render(<QueryClientProvider client={client}><MemoryRouter><ResearchAutomation params={{own:'own',author_id:7,window_minutes:30}}/></MemoryRouter></QueryClientProvider>);
 return {fetch,client,...view};
}
describe('automatic research controls',()=>{
 it('freezes abandoned progress and waits for ownership confirmation without showing active analysis',async()=>{
  const {fetch}=setup({enabled:true,stale:true});
  await screen.findByText('运行心跳已中断');
  expect(screen.getByText(/最后记录用时 1 分 1 秒/)).toBeTruthy();
  expect(screen.queryByText('正在分析：正在生成回答')).toBeNull();
  expect(screen.queryByRole('button',{name:'取消本轮分析'})).toBeNull();
  expect(screen.queryByRole('button',{name:'重新分析'})).toBeNull();
  expect(fetch.mock.calls.every(([,options])=>!options?.method)).toBe(true);
 },20000);
 it('retries only a confirmed interrupted round after an explicit click',async()=>{
  const {fetch}=setup({enabled:true,interrupted:true});
  await screen.findByText('上次分析因服务中断而停止');
  expect(fetch.mock.calls.every(([,options])=>!options?.method)).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:'重新分析'}));
  await screen.findByText('已安排重新分析');
  const writes=fetch.mock.calls.filter(([,options])=>options?.method==='POST');
  expect(writes).toHaveLength(1);
  expect(writes[0][0]).toBe('/api/ai/research/automation/retry');
  expect(writes[0][1]?.headers).toMatchObject({'X-CTN-Token':'csrf'});
  expect(JSON.parse(String(writes[0][1]?.body))).toEqual({run_id:'old-round'});
  expect(screen.queryByRole('button',{name:'重新分析'})).toBeNull();
 },20000);
 it('keeps interrupted research paused until automatic analysis is enabled',async()=>{
  setup({interrupted:true});
  await screen.findByText('上次分析因服务中断而停止');
  expect((screen.getByRole('button',{name:'重新分析'}) as HTMLButtonElement).disabled).toBe(true);
 },20000);
 it('shows thinking progress and cancels only the current round',async()=>{
  const {fetch}=setup({enabled:true,running:true});
  await screen.findByText('正在分析：模型正在思考');
  expect(screen.getByText(/已用时 2 分 0 秒/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'取消本轮分析'}));
  await screen.findByText('正在取消本轮分析');
  const writes=fetch.mock.calls.filter(([,options])=>options?.method==='POST');
  expect(writes).toHaveLength(1);
  expect(writes[0][0]).toBe('/api/ai/research/automation/cancel');
  expect(JSON.parse(String(writes[0][1]?.body))).toEqual({run_id:'current-round'});
  expect((screen.getByRole('switch',{name:'后台自动分析'}) as HTMLButtonElement).getAttribute('aria-checked')).toBe('true');
 },20000);
 it('reads settings without model calls and only saves explicit settings with revision and CSRF',async()=>{
  const {fetch}=setup();
  await screen.findByText('资料未变化，已跳过');
  expect(fetch.mock.calls.every(([,options])=>!options?.method)).toBe(true);
  fireEvent.click(screen.getByRole('switch',{name:'后台自动分析'}));
  expect((screen.getByRole('button',{name:'保存自动分析设置'}) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('自动分析文字 AI 服务'),{target:{value:'writer'}});
  fireEvent.click(screen.getByRole('switch',{name:'自动供 Agent 参考'}));
  fireEvent.click(screen.getByRole('button',{name:'保存自动分析设置'}));
  await screen.findByText('自动分析设置已保存');
  const writes=fetch.mock.calls.filter(([,options])=>options?.method==='POST');
  expect(writes.length).toBe(1);
  expect(writes[0][0]).toBe('/api/ai/research/automation');
  expect(writes[0][1]?.headers).toMatchObject({'X-CTN-Token':'csrf'});
  expect(JSON.parse(String(writes[0][1]?.body))).toMatchObject({enabled:true,provider_id:'writer',auto_activate:true,params:{own:'all'},expected_revision:3});
 },20000);
 it('copies current page scope only on request and strips unrelated analysis filters',async()=>{
  const {fetch}=setup();
  await screen.findByText('资料未变化，已跳过');
  fireEvent.click(screen.getByText('研究范围与要求'));
  expect(await screen.findByText('自动分析范围：全部帖子')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'使用当前页面的帖子范围'}));
  fireEvent.click(screen.getByRole('button',{name:'保存自动分析设置'}));
  await screen.findByText('自动分析设置已保存');
  const body=fetch.mock.calls.find(([,options])=>options?.method==='POST')?.[1]?.body;
  expect(JSON.parse(String(body)).params).toEqual({own:'own',author_id:7});
 },20000);
 it('allows pausing when the selected service is no longer available',async()=>{
  const {fetch}=setup({enabled:true,configured:false});
  await screen.findByText('资料未变化，已跳过');
  fireEvent.click(screen.getByRole('switch',{name:'后台自动分析'}));
  fireEvent.click(screen.getByRole('button',{name:'保存自动分析设置'}));
  await screen.findByText('自动分析设置已保存');
  expect(JSON.parse(String(fetch.mock.calls.find(([,options])=>options?.method==='POST')?.[1]?.body)).enabled).toBe(false);
 },20000);
 it('preserves the pending choice after a failed save',async()=>{
  setup({failedSave:true});
  await screen.findByText('资料未变化，已跳过');
  fireEvent.change(screen.getByLabelText('自动分析文字 AI 服务'),{target:{value:'writer'}});
  fireEvent.click(screen.getByRole('button',{name:'保存自动分析设置'}));
  await screen.findByText('配置已更新，请刷新');
  expect((screen.getByLabelText('自动分析文字 AI 服务') as HTMLSelectElement).value).toBe('writer');
  await waitFor(()=>expect(screen.getByText('有未保存的修改')).toBeTruthy());
 },20000);
});
