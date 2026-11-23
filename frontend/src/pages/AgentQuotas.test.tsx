// @vitest-environment jsdom
import {afterEach,beforeAll,describe,expect,it,vi} from 'vitest';
import {act,cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter} from 'react-router-dom';
import AgentQuotas,{type QuotaSnapshot} from './AgentQuotas';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
const post={bucket:'post',default_limit:3,server_limit:3,server_period_seconds:60,server_remaining:1,configured_cap:null,effective_limit:3,remaining:2,available:true,retry_after:0,observed_at:'2026-10-20T04:00:00Z',usage_last_minute:2,pending_actions:3,auto_pending_actions:1};
const fixture:QuotaSnapshot={allocation:'shared_on_demand',scope:'whole_team',usage_window_seconds:60,observed_at:'2026-10-20T04:00:01Z',policy:{limits:{}},buckets:[post,{...post,bucket:'read:get_post',default_limit:300,server_limit:300,effective_limit:300},{...post,bucket:'llm:ctn-official'}],agents:[{agent_id:'maya',name:'Maya',enabled:true,mode:'auto',account_id:12,usage_last_minute:2,pending_actions:4,auto_pending_actions:1,review_pending_actions:1,manual_pending_actions:1,blocked_pending_actions:1,last_attempt_at:'2026-10-20T04:00:00Z'}]};
function setup(options:{snapshot?:QuotaSnapshot;reject?:string}={}){
 let state=options.snapshot||fixture;
 const fetch=vi.fn((input:RequestInfo|URL,init?:RequestInit)=>{
  const url=String(input);
  if(url==='/api/bootstrap')return Promise.resolve(new Response(JSON.stringify({csrf_token:'fixture-token'}),{headers:{'Content-Type':'application/json'}}));
  if(url==='/api/agents/quotas'&&init?.method==='POST'){
   if(options.reject)return Promise.resolve(new Response(JSON.stringify({error:options.reject}),{status:400,headers:{'Content-Type':'application/json'}}));
   const payload=JSON.parse(String(init.body));
   state={...state,buckets:state.buckets.map(row=>row.bucket===payload.bucket?{...row,configured_cap:payload.cap,effective_limit:Math.min(payload.cap??Infinity,(row.server_limit??row.default_limit)*60/(row.server_period_seconds||60))}:row)};
  }
  return Promise.resolve(new Response(JSON.stringify(state),{headers:{'Content-Type':'application/json'}}));
 });
 vi.stubGlobal('fetch',fetch);const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 render(<QueryClientProvider client={client}><MemoryRouter><AgentQuotas/></MemoryRouter></QueryClientProvider>);return fetch;
}
function posted(fetch:ReturnType<typeof vi.fn>){return fetch.mock.calls.filter(([,init])=>init?.method==='POST').map(([,init])=>JSON.parse(String(init!.body)));}
async function customPost(){fireEvent.click(await screen.findByRole('button',{name:'设置发帖、回复与转发上限'}));const dialog=await screen.findByRole('dialog');fireEvent.click(within(dialog).getByLabelText('自定义全队期望上限'));return dialog;}

describe('shared Agent quota management',()=>{
 it('separates server snapshots, local estimates and program attempts without displaying AI quotas or personal allocations',async()=>{
  const fetch=setup();const summary=(await screen.findByText('平台额度池')).parentElement!;
  expect(within(summary).getByText('全队共享')).toBeTruthy();expect(within(summary).getByText('竞赛调整使用机会，不增加平台总额度')).toBeTruthy();
  expect(screen.queryByText('所有 Agent 共用，无固定个人份额')).toBeNull();expect(screen.getByRole('columnheader',{name:'本地可用估算'})).toBeTruthy();
  expect(screen.getAllByText('上次剩余 1')).toHaveLength(2);expect(screen.getByText('帖子详情')).toBeTruthy();expect(screen.queryByText('llm:ctn-official')).toBeNull();
  expect(screen.queryByRole('button',{name:'设置帖子详情上限'})).toBeNull();expect(screen.getByRole('link',{name:'Maya'}).getAttribute('href')).toBe('/ai/agents/maya');
  expect(screen.getByRole('columnheader',{name:'手动待执行'})).toBeTruthy();
  expect(screen.getByText(/请求尝试包含失败与结果未确认的请求，仅统计本程序/)).toBeTruthy();expect(posted(fetch)).toEqual([]);
 },30000);
 it('saves one shared route cap without role settings and uses the returned effective policy',async()=>{
  const fetch=setup(),dialog=await customPost();fireEvent.change(within(dialog).getByRole('spinbutton',{name:'全队期望每分钟最多请求次数'}),{target:{value:'2'}});fireEvent.click(within(dialog).getByRole('button',{name:'保存设置'}));
  expect(await screen.findByText('发帖、回复与转发的期望上限已设为每分钟 2 次，当前实际 2 次/分钟。')).toBeTruthy();expect(posted(fetch)).toEqual([{bucket:'post',cap:2}]);
  expect(screen.getByRole('button',{name:'恢复发帖、回复与转发为官方'})).toBeTruthy();
 },30000);
 it('saves a desired cap above the server rate while keeping the effective limit clamped, including non-minute windows',async()=>{
  const fetch=setup({snapshot:{...fixture,buckets:[{...post,server_limit:1,server_period_seconds:30,effective_limit:2}]}}),dialog=await customPost();
  fireEvent.change(within(dialog).getByRole('spinbutton',{name:'全队期望每分钟最多请求次数'}),{target:{value:'3'}});
  expect(within(dialog).getByText('当前实际有效上限：2 次/分钟')).toBeTruthy();fireEvent.click(within(dialog).getByRole('button',{name:'保存设置'}));
  expect(await screen.findByText('发帖、回复与转发的期望上限已设为每分钟 3 次，当前实际 2 次/分钟。')).toBeTruthy();expect(posted(fetch)).toEqual([{bucket:'post',cap:3}]);
 },30000);
 it('restores the server-following policy with null instead of resetting consumption',async()=>{
  const fetch=setup({snapshot:{...fixture,buckets:[{...post,configured_cap:2,effective_limit:2,remaining:0,retry_after:17}]}});
  fireEvent.click(await screen.findByRole('button',{name:'恢复发帖、回复与转发为官方'}));
  expect(await screen.findByText('发帖、回复与转发已恢复跟随官方。')).toBeTruthy();expect(posted(fetch)).toEqual([{bucket:'post',cap:null}]);
  expect(screen.getByText('约 17 秒后可重试')).toBeTruthy();
 },30000);
 it('keeps the editor and unsaved cap when the server rejects the save',async()=>{
  const fetch=setup({reject:'额度配置保存失败，请重试'}),dialog=await customPost();
  fireEvent.change(within(dialog).getByRole('spinbutton',{name:'全队期望每分钟最多请求次数'}),{target:{value:'2'}});fireEvent.click(within(dialog).getByRole('button',{name:'保存设置'}));
  expect(await within(dialog).findByText('额度配置保存失败，请重试')).toBeTruthy();expect((within(dialog).getByRole('spinbutton',{name:'全队期望每分钟最多请求次数'}) as HTMLInputElement).value).toBe('2');
  await waitFor(()=>expect(posted(fetch)).toEqual([{bucket:'post',cap:2}]));
 },30000);
 it('preserves unsaved input during polling while updating the effective limit preview',async()=>{
  const fetch=setup(),dialog=await customPost();fireEvent.change(within(dialog).getByRole('spinbutton',{name:'全队期望每分钟最多请求次数'}),{target:{value:'2'}});
  await act(async()=>{clients[clients.length-1].setQueryData(['/agents/quotas',{}],{...fixture,buckets:[{...post,server_limit:1,effective_limit:1}]});});
  expect((within(dialog).getByRole('spinbutton',{name:'全队期望每分钟最多请求次数'}) as HTMLInputElement).value).toBe('2');
  expect(await within(dialog).findByText('当前实际有效上限：1 次/分钟')).toBeTruthy();expect(posted(fetch)).toEqual([]);
 },30000);
 it('requires a positive integer for a custom desired cap',async()=>{
  const fetch=setup(),dialog=await customPost();fireEvent.change(within(dialog).getByRole('spinbutton',{name:'全队期望每分钟最多请求次数'}),{target:{value:''}});fireEvent.click(within(dialog).getByRole('button',{name:'保存设置'}));
  expect(await within(dialog).findByText('请输入大于 0 的整数，或选择跟随官方。')).toBeTruthy();expect(posted(fetch)).toEqual([]);
 },30000);
});
