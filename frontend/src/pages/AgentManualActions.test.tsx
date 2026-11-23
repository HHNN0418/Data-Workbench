// @vitest-environment jsdom
import {afterEach,beforeAll,describe,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter} from 'react-router-dom';
import AgentManualActions from './AgentManualActions';
import {DraftCard} from './Agents';
import type {Row} from '../api';

vi.mock('antd',async importOriginal=>{const original=await importOriginal<typeof import('antd')>();return {...original,Select:({options,value,onChange,disabled,'aria-label':label}:any)=><select aria-label={label} value={value||''} disabled={disabled} onChange={event=>onChange(event.target.value)}>{options.map((option:any)=><option key={option.value} value={option.value}>{option.label}</option>)}</select>};});
beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn().mockImplementation(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
const agent={id:'maya',name:'Maya',username:'maya_collins',account_id:12};
function setup(options:{prepare?:(payload:Row,attempt:number)=>Row|Response|undefined;publish?:(draft:Row)=>Row;inventory?:(params:URLSearchParams)=>Row;drafts?:Row[]}={}){
 let draft:Row|undefined,attempt=0;
 const fetch=vi.fn((input:RequestInfo|URL,init?:RequestInit)=>{
  const path=String(input),payload=init?.body?JSON.parse(String(init.body)):{};let result:Row|Response={csrf_token:'test'};
  if(path==='/api/agents/manual/prepare'){
   attempt++;draft={...payload,id:'manual-one',account_id:12,state:'draft',manual:true};
   const preview={actor:{account_id:12,username:'maya_collins'},...(payload.target_post_id?{target_post:{post_id:payload.target_post_id,author_id:12,username:'maya_collins',body:'目标帖子原始内容。'}}:{}),...(payload.target_account_id?{target_account:{account_id:payload.target_account_id,username:'target_person',display_name:'对方显示名称'}}:{})};
   result=options.prepare?.(payload,attempt)??{draft_id:draft!.id,draft,preview};
  }else if(path==='/api/agents/publish'){draft={...draft,state:'published'};result=options.publish?.(draft)??{agents:[agent],settings:{enabled:true},drafts:[draft]};}
  else if(path.startsWith('/api/agents/maya/social?'))result=options.inventory?.(new URL('http://local'+path).searchParams)??{agent_id:'maya',account_id:12,kind:'posts',scope:'bound_account',items:[],observed_at:'2026-10-20T03:00:00Z'};
  else if(path==='/api/agents/discard')result={agents:[agent],settings:{enabled:true},drafts:[{...draft,state:'discarded'}]};
  return Promise.resolve(result instanceof Response?result:new Response(JSON.stringify(result),{status:200,headers:{'Content-Type':'application/json'}}));
 });
 vi.stubGlobal('fetch',fetch);const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 const tree=(nextAgent=agent)=><QueryClientProvider client={client}><MemoryRouter><AgentManualActions agent={nextAgent} drafts={options.drafts||[]}/></MemoryRouter></QueryClientProvider>;
 const rendered=render(tree());return {fetch,rerenderAgent:(nextAgent:typeof agent)=>rendered.rerender(tree(nextAgent))};
}
function posts(fetch:ReturnType<typeof vi.fn>,path:string){return fetch.mock.calls.filter(([url,init])=>url===path&&init?.body).map(([,init])=>JSON.parse(String(init!.body)));}
function select(action:string){fireEvent.change(screen.getByRole('combobox',{name:'手动动作'}),{target:{value:action}});}
function setPostId(id:number){fireEvent.change(screen.getByRole('spinbutton',{name:'目标帖子 ID'}),{target:{value:String(id)}});}
describe('manual agent actions',()=>{
 it('reads the bound account inventory only on request, pages it, and selects a post without executing anything',async()=>{
  const {fetch}=setup({inventory:params=>({agent_id:'maya',account_id:12,kind:'posts',scope:'bound_account',items:[{post_id:params.get('cursor')?72:71,author_id:12,body:params.get('cursor')?'本人第二页帖子':'本人第一页帖子'}],next_cursor:params.get('cursor')?null:'next-page',observed_at:'2026-10-20T03:00:00Z'})});
  expect(fetch.mock.calls.some(([url])=>String(url).includes('/social?'))).toBe(false);fireEvent.click(screen.getByRole('button',{name:'读取账号现状'}));expect(await screen.findByText('本人第一页帖子')).toBeTruthy();fireEvent.click(screen.getByRole('button',{name:'下一页账号资料'}));expect(await screen.findByText('本人第二页帖子')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'选择删除帖子 #72'}));expect((screen.getByRole('combobox',{name:'手动动作'}) as HTMLSelectElement).value).toBe('delete_post');expect((screen.getByRole('spinbutton',{name:'目标帖子 ID'}) as HTMLInputElement).value).toBe('72');
  expect(fetch.mock.calls.some(([url])=>String(url).includes('cursor=next-page'))).toBe(true);expect(posts(fetch,'/api/agents/manual/prepare')).toHaveLength(0);expect(posts(fetch,'/api/agents/publish')).toHaveLength(0);
 },30000);
 it.each([{kind:'likes',action:'unlike',label:'选择取消点赞帖子 #71',item:{post_id:71,author_id:99,body:'我点赞过的真实目标'}},{kind:'following',action:'unfollow',label:'选择取消关注账号 #91',item:{account_id:91,username:'a_real_person',display_name:'真实关注对象'}}])('selects a target from $kind without sending a social write',async({kind,action,label,item})=>{
  const {fetch}=setup({inventory:()=>({agent_id:'maya',account_id:12,kind,scope:'bound_account',items:[item],next_cursor:null,observed_at:'2026-10-20T03:00:00Z'})});
  fireEvent.change(screen.getByRole('combobox',{name:'账号现状类型'}),{target:{value:kind}});fireEvent.click(screen.getByRole('button',{name:'读取账号现状'}));fireEvent.click(await screen.findByRole('button',{name:label}));
  expect((screen.getByRole('combobox',{name:'手动动作'}) as HTMLSelectElement).value).toBe(action);expect(posts(fetch,'/api/agents/manual/prepare')).toHaveLength(0);expect(posts(fetch,'/api/agents/publish')).toHaveLength(0);
 },30000);
 it('never displays an inventory response for another bound account',async()=>{
  setup({inventory:()=>({agent_id:'maya',account_id:99,kind:'posts',scope:'bound_account',items:[{post_id:71,author_id:99,body:'不属于当前账号的帖子'}]})});fireEvent.click(screen.getByRole('button',{name:'读取账号现状'}));
  expect(await screen.findByText('服务器未返回当前绑定账号的资料，未显示结果。')).toBeTruthy();expect(screen.queryByText('不属于当前账号的帖子')).toBeNull();expect(screen.queryByRole('button',{name:'选择删除帖子 #71'})).toBeNull();
 },30000);
 it('previews the account and written post before any external execution, then publishes only on explicit confirmation',async()=>{
  const {fetch}=setup({drafts:[{id:'manual-one',agent_id:'maya',account_id:12,action:'post',status:'draft',body:'An ordinary observation from the library today.'}]});fireEvent.change(screen.getByRole('textbox',{name:'手动操作正文'}),{target:{value:'An ordinary observation from the library today.'}});fireEvent.click(screen.getByRole('button',{name:'预览并准备操作'}));
  await screen.findByRole('region',{name:'手动操作执行预览'});expect(posts(fetch,'/api/agents/publish')).toHaveLength(0);expect(within(screen.getByRole('region',{name:'手动操作执行预览'})).getByText('An ordinary observation from the library today.')).toBeTruthy();
  const prepared=posts(fetch,'/api/agents/manual/prepare')[0];expect(prepared.agent_id).toBe('maya');expect(prepared.action).toBe('post');expect(prepared.idempotency_key).toMatch(/^[a-f0-9-]{36}$/i);
  fireEvent.click(screen.getByRole('button',{name:'确认执行发帖'}));await screen.findByRole('button',{name:'开始新的手动操作'});
  expect(posts(fetch,'/api/agents/publish')).toEqual([{draft_id:'manual-one',body:'An ordinary observation from the library today.'}]);expect(fetch.mock.calls.some(([url])=>String(url).includes('/ai/')||String(url).includes('/agents/run'))).toBe(false);
 },30000);
 it('uses the same idempotency key when retrying an unresolved preparation request',async()=>{
  const {fetch}=setup({prepare:(_payload,attempt)=>attempt===1?new Response(JSON.stringify({error:'准备请求未完成，请重试'}),{status:503}):undefined});
  select('follow');fireEvent.change(screen.getByRole('spinbutton',{name:'目标账号 ID'}),{target:{value:'91'}});fireEvent.click(screen.getByRole('button',{name:'预览并准备操作'}));await screen.findByText('准备请求未完成，请重试');
  fireEvent.click(screen.getByRole('button',{name:'预览并准备操作'}));await screen.findByRole('region',{name:'手动操作执行预览'});
  const prepared=posts(fetch,'/api/agents/manual/prepare');expect(prepared).toHaveLength(2);expect(prepared[1].idempotency_key).toBe(prepared[0].idempotency_key);expect(prepared[1].body).toBe('');expect(posts(fetch,'/api/agents/publish')).toHaveLength(0);
 },30000);
 it('requires the exact target post ID before deleting the role own post',async()=>{
  const {fetch}=setup();select('delete_post');setPostId(71);fireEvent.click(screen.getByRole('button',{name:'预览并准备操作'}));await screen.findByText('目标帖子原始内容。');
  const execute=screen.getByRole('button',{name:'永久删除这条帖子'}) as HTMLButtonElement;expect(execute.disabled).toBe(true);expect(posts(fetch,'/api/agents/publish')).toHaveLength(0);
  fireEvent.change(screen.getByRole('spinbutton',{name:'输入帖子 ID 确认删除'}),{target:{value:'72'}});expect(execute.disabled).toBe(true);
  fireEvent.change(screen.getByRole('spinbutton',{name:'输入帖子 ID 确认删除'}),{target:{value:'71'}});expect(execute.disabled).toBe(false);fireEvent.click(execute);
  await screen.findByRole('button',{name:'开始新的手动操作'});expect(posts(fetch,'/api/agents/publish')).toEqual([{draft_id:'manual-one',body:'',confirmation_post_id:71}]);
 },30000);
 it('blocks deleting another account post even if a malformed preview is returned',async()=>{
  const {fetch}=setup({prepare:payload=>({draft_id:'manual-one',draft:{...payload,id:'manual-one',account_id:12,state:'draft'},preview:{actor:{account_id:12},target_post:{post_id:71,author_id:99,body:'别人发布的帖子'}}})});
  select('delete_post');setPostId(71);fireEvent.click(screen.getByRole('button',{name:'预览并准备操作'}));await screen.findByText('目标作者不是此角色当前账号，不能删除');fireEvent.change(screen.getByRole('spinbutton',{name:'输入帖子 ID 确认删除'}),{target:{value:'71'}});
  expect((screen.getByRole('button',{name:'永久删除这条帖子'}) as HTMLButtonElement).disabled).toBe(true);expect(posts(fetch,'/api/agents/publish')).toHaveLength(0);
 },30000);
 it('requires preparation again after the role is bound to a different account',async()=>{
  const {fetch,rerenderAgent}=setup();select('like');setPostId(71);fireEvent.click(screen.getByRole('button',{name:'预览并准备操作'}));await screen.findByRole('region',{name:'手动操作执行预览'});rerenderAgent({...agent,account_id:13});
  expect(await screen.findByText('操作账号与当前绑定不一致，请重新准备')).toBeTruthy();expect((screen.getByRole('button',{name:'确认执行点赞'}) as HTMLButtonElement).disabled).toBe(true);expect(posts(fetch,'/api/agents/publish')).toHaveLength(0);
 },30000);
 it('keeps uncertain outcomes for remote reconciliation and never offers another execution',async()=>{
  const {fetch}=setup({publish:draft=>({agents:[agent],settings:{enabled:true},drafts:[{...draft,state:'uncertain'}]})});select('unlike');setPostId(71);fireEvent.click(screen.getByRole('button',{name:'预览并准备操作'}));await screen.findByRole('button',{name:'确认执行取消点赞'});fireEvent.click(screen.getByRole('button',{name:'确认执行取消点赞'}));
  expect(await screen.findByText('结果尚未确认，不能重复执行')).toBeTruthy();expect(screen.queryByRole('button',{name:'确认执行取消点赞'})).toBeNull();expect(screen.getByRole('link',{name:'到操作记录核对远端结果'}).getAttribute('href')).toBe('/ai/agents/maya?tab=drafts&draft=manual-one');expect(posts(fetch,'/api/agents/publish')).toHaveLength(1);
 },30000);
 it('also requires a deletion confirmation in the shared operation review card',async()=>{
  const publish=vi.fn().mockResolvedValue(true);
  render(<MemoryRouter><DraftCard agent={{...agent,enabled:false,mode:'review',daily_limit:6,interval_seconds:1800}} draft={{id:'delete-one',agent_id:'maya',action:'delete_post',status:'draft',body:'',target_post_id:71,manual:true,manual_preview:{target_post:{post_id:71,author_id:12,body:'原始帖子'}}}} busy={false} onPublish={publish} onDiscard={vi.fn()} onReconcile={vi.fn()}/></MemoryRouter>);
  const execute=screen.getByRole('button',{name:'确认删除本人帖子'}) as HTMLButtonElement;expect(execute.disabled).toBe(true);fireEvent.change(screen.getByRole('spinbutton',{name:'确认删除帖子 ID'}),{target:{value:'71'}});expect(execute.disabled).toBe(false);fireEvent.click(execute);await waitFor(()=>expect(publish).toHaveBeenCalledWith('delete-one','',71));
 },30000);
});
