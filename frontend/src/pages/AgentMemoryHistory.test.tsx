// @vitest-environment jsdom
import {afterEach,beforeAll,describe,expect,it,vi} from 'vitest';
import {cleanup,configure,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter} from 'react-router-dom';
import AgentMemoryHistory from './AgentMemoryHistory';

vi.mock('antd',async importOriginal=>{
 const original=await importOriginal<typeof import('antd')>();
 return {...original,Select:({options,value,onChange,'aria-label':label}:any)=><select aria-label={label} value={value} onChange={event=>onChange(event.target.value)}>{options.map((option:any)=><option key={option.value} value={option.value}>{option.label}</option>)}</select>};
});
beforeAll(()=>{configure({asyncUtilTimeout:10000});Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn().mockImplementation(query=>({matches:false,media:query,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
function setup(respond:(params:URLSearchParams)=>{body:unknown;status?:number}){
 const fetch=vi.fn((input:RequestInfo|URL)=>{const params=new URL(String(input),'http://localhost').searchParams,result=respond(params);return Promise.resolve(new Response(JSON.stringify(result.body),{status:result.status??200,headers:{'Content-Type':'application/json'}}));});
 vi.stubGlobal('fetch',fetch);const client=new QueryClient({defaultOptions:{queries:{retry:false}}});clients.push(client);
 render(<QueryClientProvider client={client}><MemoryRouter><AgentMemoryHistory agentId="leah" agentName="Leah Patel"/></MemoryRouter></QueryClientProvider>);return fetch;
}
describe('long term memory search',()=>{
 it('searches the selected role, filters and pages persisted memories with source evidence',async()=>{
  const fetch=setup(params=>({body:{items:[{id:`memory-${params.get('offset')}`,kind:'viewpoints',title:'对公交的看法',excerpt:params.get('offset')==='20'?'较早的一段公交回忆。':'公交需要覆盖夜班。',at:'2026-10-21T00:10:00Z',source_ids:['post:94320','news:article-1'],sources:[{source_id:'news:article-1',title:'夜班公交方案'}]}],total:21,offset:Number(params.get('offset')),limit:20,has_more:params.get('offset')==='0',retention:'persistent_since_upgrade'}}));
  expect(await screen.findByText('公交需要覆盖夜班。')).toBeTruthy();
  expect(screen.getByRole('link',{name:'帖子 #94320 ↗'}).getAttribute('href')).toBe('/posts?post=94320');expect(screen.getByRole('link',{name:'新闻：夜班公交方案 ↗'}).getAttribute('href')).toBe('/news?q='+encodeURIComponent('夜班公交方案'));
  expect(screen.getByText('角色的主观理解，会随新资料修正。')).toBeTruthy();expect(screen.getByText(/2026\/10\/21/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText('查找历史回忆'),{target:{value:' 夜班 '}});expect(fetch).toHaveBeenCalledTimes(1);fireEvent.click(screen.getByRole('button',{name:/查\s*找/}));
  await waitFor(()=>expect(fetch.mock.calls.some(([url])=>new URL(String(url),'http://localhost').searchParams.get('q')==='夜班')).toBe(true));
  fireEvent.change(screen.getByLabelText('筛选回忆类型'),{target:{value:'viewpoints'}});
  await waitFor(()=>expect(fetch.mock.calls.some(([url])=>new URL(String(url),'http://localhost').searchParams.get('kind')==='viewpoints')).toBe(true));
  await waitFor(()=>expect((screen.getByRole('button',{name:'下一页'}) as HTMLButtonElement).disabled).toBe(false));fireEvent.click(screen.getByRole('button',{name:'下一页'}));
  expect(await screen.findByText('较早的一段公交回忆。')).toBeTruthy();expect((screen.getByRole('button',{name:'下一页'}) as HTMLButtonElement).disabled).toBe(true);
  const last=new URL(String(fetch.mock.calls.at(-1)?.[0]),'http://localhost');expect(last.pathname).toBe('/api/agents/memory-history');expect(Object.fromEntries(last.searchParams)).toEqual({agent_id:'leah',q:'夜班',kind:'viewpoints',limit:'20',offset:'20'});
  fireEvent.change(screen.getByLabelText('筛选回忆类型'),{target:{value:'people'}});await waitFor(()=>expect(new URL(String(fetch.mock.calls.at(-1)?.[0]),'http://localhost').searchParams.get('offset')).toBe('0'));
 },30000);
 it('shows a clear empty state and keeps retrieval errors distinct from absent memories',async()=>{
  const fetch=setup(params=>params.get('q')?{body:{error:'回忆暂时读取失败'},status:503}:{body:{items:[],total:0,has_more:false}});
  expect(await screen.findByText('还没有保存历史回忆，角色阅读和思考后会逐步积累。')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('查找历史回忆'),{target:{value:'公交'}});fireEvent.click(screen.getByRole('button',{name:/查\s*找/}));
  expect(await screen.findByText('历史回忆读取失败')).toBeTruthy();expect(screen.queryByText('还没有保存历史回忆，角色阅读和思考后会逐步积累。')).toBeNull();expect(screen.getByText('回忆暂时读取失败')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'重新读取历史回忆'}));await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(3));
 },30000);
 it('distinguishes unfinished and uncertain action narratives from verified delivery and manual origin',async()=>{
  setup(()=>({body:{items:[{id:'pending',kind:'outcome',action:'reply',state:'pending',origin:'manual',excerpt:'我准备回复这条消息。'},{id:'failed',kind:'outcome',action:'post',state:'failed',excerpt:'这次发帖失败。'},{id:'uncertain',kind:'outcome',action:'like',state:'uncertain',excerpt:'点赞尚未核实。'},{id:'done',kind:'outcome',action:'post',state:'published',excerpt:'已经核实的发帖。'},{id:'people',kind:'people',excerpt:'这次交流后愿意继续了解。',previous_text:'之前对此人不太信任。',author_id:91}],total:5,has_more:false}}));
  expect(await screen.findByText('待执行，尚未完成')).toBeTruthy();expect(screen.getByText('人工操作')).toBeTruthy();expect(screen.getByText('执行失败')).toBeTruthy();expect(screen.getByText('结果待核对')).toBeTruthy();expect(screen.getByText('已核实完成')).toBeTruthy();
  expect(screen.getAllByText('正文和当时的打算不表示操作已经完成，请以核对结果为准。')).toHaveLength(3);expect(screen.getByText('此前的想法：之前对此人不太信任。')).toBeTruthy();expect(screen.getByRole('link',{name:'查看账号 #91 ↗'}).getAttribute('href')).toBe('/accounts?q=91');
 },30000);
});
