// @vitest-environment jsdom
import {afterEach,beforeAll,describe,expect,it,vi} from 'vitest';
import {act,cleanup,configure,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {Link,MemoryRouter,Route,Routes} from 'react-router-dom';
import Agents from './Agents';

vi.mock('antd',async importOriginal=>{
 const original=await importOriginal<typeof import('antd')>();
 return {...original,Select:({options,value,onChange,'aria-label':label}:any)=><select aria-label={label} value={value??''} onChange={event=>onChange(event.target.value)}>{options.map((option:any)=><option key={option.value} value={option.value}>{option.label}</option>)}</select>};
});
beforeAll(()=>{
 configure({asyncUtilTimeout:10000});
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn().mockImplementation(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
 Object.defineProperty(HTMLElement.prototype,'scrollIntoView',{writable:true,value:vi.fn()});
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
const agent={id:'maya',name:'Maya',username:'maya_collins',enabled:false,mode:'review',account_id:12,interval_seconds:1800,allowed_actions:['like'],version:1};
type DraftFixture={id:string;agent_id:string;action:string;body:string;target_post_id:number;status:string;version?:number;reason?:string};
function drafts(count:number,agentId='maya',start=1,status='draft'):DraftFixture[]{
 return Array.from({length:count},(_,index)=>({id:`${agentId}-${start+index}`,agent_id:agentId,action:'like',body:'',target_post_id:1000+start+index,status}));
}
function setup(initialDrafts:DraftFixture[],route='/ai/agents?tab=drafts'){
 let state={settings:{enabled:false,team_side:'Vote Yes',max_accounts:3},agents:[agent,{...agent,id:'ethan',name:'Ethan',username:'ethan_brooks'}],drafts:initialDrafts,events:[],has_team_code:true,sdk_available:true};
 const fetch=vi.fn((input:RequestInfo|URL)=>{
  const url=String(input),data=url==='/api/ai/providers'?{providers:[]}:url==='/api/bootstrap'?{csrf_token:'test'}:state;
  return Promise.resolve(new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}}));
 });
 vi.stubGlobal('fetch',fetch);
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[route]}><Link to="/ai/agents/ethan?tab=drafts">测试切换到 Ethan</Link><Link to="/ai/agents/maya?tab=drafts&draft=maya-301">测试打开指定操作</Link><Routes><Route path="/ai/agents" element={<Agents/>}/><Route path="/ai/agents/:agentId" element={<Agents/>}/></Routes></MemoryRouter></QueryClientProvider>);
 return {fetch,refresh:async(nextDrafts:DraftFixture[])=>{
  state={...state,drafts:nextDrafts};
  await act(async()=>{await client.refetchQueries({queryKey:['/agents',{}],exact:true});});
 }};
}
function top(){return within(screen.getByRole('navigation',{name:'操作记录分页（顶部）'}));}
function bottom(){return within(screen.getByRole('navigation',{name:'操作记录分页（底部）'}));}
function target(id:number){return screen.queryByRole('link',{name:`目标帖子 #${1000+id} ↗`});}
function displayedTargets(){return screen.queryAllByRole('link',{name:/^目标帖子 #/});}

describe('operation record pagination',()=>{
 it('shows ten records per page and keeps the top and bottom controls synchronized',async()=>{
  setup(drafts(25));
  await screen.findByRole('navigation',{name:'操作记录分页（顶部）'});
  expect(displayedTargets()).toHaveLength(10);expect(target(1)).toBeTruthy();expect(target(11)).toBeNull();
  expect(top().getByText('第 1–10 条，共 25 条')).toBeTruthy();
  expect(bottom().getByText('第 1–10 条，共 25 条')).toBeTruthy();
  fireEvent.click(top().getByTitle('Next Page'));
  expect(top().getByText('第 11–20 条，共 25 条')).toBeTruthy();
  expect(bottom().getByText('第 11–20 条，共 25 条')).toBeTruthy();
  expect(target(1)).toBeNull();expect(target(11)).toBeTruthy();expect(target(21)).toBeNull();
  fireEvent.click(bottom().getByTitle('Previous Page'));
  expect(target(1)).toBeTruthy();expect(target(11)).toBeNull();
  const jump=top().getByRole('textbox',{name:'Page'});
  fireEvent.change(jump,{target:{value:'3'}});fireEvent.keyUp(jump,{key:'Enter',code:'Enter',keyCode:13});
  expect(displayedTargets()).toHaveLength(5);expect(target(21)).toBeTruthy();expect(target(25)).toBeTruthy();
  expect(top().getByText('第 21–25 条，共 25 条')).toBeTruthy();
 },30000);

 it('changes page size from a later page and returns to the first page',async()=>{
  setup(drafts(25));
  await screen.findByRole('navigation',{name:'操作记录分页（顶部）'});
  fireEvent.click(top().getByTitle('3'));
  fireEvent.mouseDown(top().getByRole('combobox',{name:'Page Size'}));
  fireEvent.click(await top().findByText('20 / page'));
  expect(top().getByText('第 1–20 条，共 25 条')).toBeTruthy();
  expect(bottom().getByText('第 1–20 条，共 25 条')).toBeTruthy();
  expect(displayedTargets()).toHaveLength(20);expect(target(1)).toBeTruthy();expect(target(21)).toBeNull();
  fireEvent.mouseDown(top().getByRole('combobox',{name:'Page Size'}));
  fireEvent.click(await top().findByText('50 / page'));
  expect(top().getByText('第 1–25 条，共 25 条')).toBeTruthy();expect(displayedTargets()).toHaveLength(25);
  expect(screen.queryByRole('navigation',{name:'操作记录分页（底部）'})).toBeNull();
 },30000);

 it('returns to the first page when role or status filters change and uses the filtered total',async()=>{
  setup([...drafts(21),...drafts(11,'ethan',201),...drafts(2,'ethan',301,'published')]);
  await screen.findByRole('navigation',{name:'操作记录分页（顶部）'});
  fireEvent.click(top().getByTitle('3'));expect(target(21)).toBeTruthy();
  fireEvent.change(screen.getByLabelText('筛选角色草稿'),{target:{value:'ethan'}});
  expect(top().getByText('第 1–10 条，共 11 条')).toBeTruthy();expect(target(201)).toBeTruthy();expect(target(1)).toBeNull();
  fireEvent.click(top().getByTitle('Next Page'));expect(target(211)).toBeTruthy();
  fireEvent.change(screen.getByLabelText('筛选草稿状态'),{target:{value:'published'}});
  expect(top().getByText('第 1–2 条，共 2 条')).toBeTruthy();expect(target(301)).toBeTruthy();expect(target(211)).toBeNull();
  expect(screen.queryByRole('navigation',{name:'操作记录分页（底部）'})).toBeNull();
  fireEvent.change(screen.getByLabelText('筛选草稿状态'),{target:{value:'all'}});
  expect(top().getByText('第 1–10 条，共 13 条')).toBeTruthy();expect(target(201)).toBeTruthy();
  fireEvent.click(top().getByTitle('Next Page'));
  fireEvent.change(screen.getByLabelText('筛选角色草稿'),{target:{value:''}});
  expect(top().getByText('第 1–10 条，共 34 条')).toBeTruthy();expect(target(1)).toBeTruthy();
 },30000);

 it('preserves the current page across data refreshes and clamps it when the list shrinks',async()=>{
  const initial=drafts(21),{refresh}=setup(initial);
  await screen.findByRole('navigation',{name:'操作记录分页（顶部）'});
  fireEvent.click(top().getByTitle('3'));expect(target(21)).toBeTruthy();
  await refresh(initial.map(draft=>({...draft,version:2,reason:`刷新后的操作 ${draft.id}`})));
  await screen.findByText('刷新后的操作 maya-21',{exact:false});
  expect(top().getByText('第 21–21 条，共 21 条')).toBeTruthy();expect(target(21)).toBeTruthy();expect(target(1)).toBeNull();
  await refresh(initial.slice(0,16));
  await waitFor(()=>expect(top().getByText('第 11–16 条，共 16 条')).toBeTruthy());
  expect(displayedTargets()).toHaveLength(6);expect(target(11)).toBeTruthy();expect(target(21)).toBeNull();
  await refresh([]);
  expect(await screen.findByText('当前没有符合条件的操作')).toBeTruthy();
  expect(screen.queryByRole('navigation',{name:'操作记录分页（顶部）'})).toBeNull();
  await refresh(initial);
  await waitFor(()=>expect(top().getByText('第 1–10 条，共 21 条')).toBeTruthy());
 },30000);

 it('paginates within a personal role and keeps focused operation links visible after navigation',async()=>{
  setup([...drafts(11),...drafts(12,'ethan',201),...drafts(1,'maya',301,'published')],'/ai/agents/maya?tab=drafts');
  await screen.findByRole('navigation',{name:'操作记录分页（顶部）'});
  expect(top().getByText('第 1–10 条，共 11 条')).toBeTruthy();expect(screen.queryByLabelText('筛选角色草稿')).toBeNull();
  expect(target(201)).toBeNull();
  fireEvent.click(top().getByTitle('Next Page'));expect(target(11)).toBeTruthy();
  fireEvent.click(screen.getByRole('link',{name:'测试切换到 Ethan'}));
  await waitFor(()=>expect(top().getByText('第 1–10 条，共 12 条')).toBeTruthy());
  expect(target(201)).toBeTruthy();expect(target(11)).toBeNull();
  fireEvent.click(top().getByTitle('Next Page'));expect(target(211)).toBeTruthy();
  fireEvent.click(screen.getByRole('link',{name:'测试打开指定操作'}));
  await waitFor(()=>expect(top().getByText('第 1–1 条，共 1 条')).toBeTruthy());
  expect(target(301)).toBeTruthy();expect(displayedTargets()).toHaveLength(1);expect(target(211)).toBeNull();
  expect(screen.queryByRole('navigation',{name:'操作记录分页（底部）'})).toBeNull();
  fireEvent.change(screen.getByLabelText('筛选草稿状态'),{target:{value:'pending'}});
  expect(top().getByText('第 1–10 条，共 11 条')).toBeTruthy();expect(target(1)).toBeTruthy();expect(target(301)).toBeNull();
 },30000);
});
