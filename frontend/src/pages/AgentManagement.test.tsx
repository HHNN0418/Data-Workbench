// @vitest-environment jsdom
import {afterEach,beforeAll,describe,expect,it,vi} from 'vitest';
import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter,Route,Routes} from 'react-router-dom';
import Agents,{DraftCard} from './Agents';
import AgentLogs,{AgentRelationships} from './AgentLogs';

vi.mock('antd',async importOriginal=>{const original=await importOriginal<typeof import('antd')>();return {...original,Select:({options,value,onChange,'aria-label':label}:any)=><select aria-label={label} value={value||''} onChange={event=>onChange(event.target.value)}>{options.map((option:any)=><option key={option.value} value={option.value}>{option.label}</option>)}</select>};});
beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn().mockImplementation(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
const agent={id:'maya',name:'Maya',username:'maya_collins',enabled:false,mode:'review' as const,account_id:12,daily_limit:6,interval_seconds:1800,allowed_actions:['post','reply','repost','like','skip'],background:'图书馆里的社区工作。',voice:'简单具体。',version:3};
const state={settings:{enabled:false,team_side:'Vote Yes',max_accounts:3},agents:[agent,{...agent,id:'ethan',name:'Ethan'}],drafts:[{id:'maya-action',agent_id:'maya',action:'follow',body:'',target_account_id:91,status:'draft',reason:'讨论过几次共同关心的问题。'},{id:'ethan-action',agent_id:'ethan',action:'post',body:'其他角色的草稿',status:'draft'}],events:[{agent_id:'ethan',message:'全队消息不能混入个人日志'}],has_team_code:true,sdk_available:true};
function setup(element:React.ReactNode,route='/ai/agents/maya',fetcher?:(url:string)=>unknown){
 const fetch=vi.fn((input:RequestInfo|URL)=>{const url=String(input),data=fetcher?.(url)??(url==='/api/ai/providers'?{providers:[{id:'p',enabled:true,protocol:'openai_chat',name:'AI',model:'m'}]}:url==='/api/bootstrap'?{csrf_token:'test'}:state);return Promise.resolve(new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}}));});
 vi.stubGlobal('fetch',fetch);const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[route]}>{element}</MemoryRouter></QueryClientProvider>);return fetch;
}
describe('individual agent management',()=>{
 it('opens the manual operation tab without starting the agent or changing automatic settings',async()=>{
  const fetch=setup(<Routes><Route path="/ai/agents/:agentId" element={<Agents/>}/></Routes>,'/ai/agents/maya?tab=manual');
  expect(await screen.findByRole('heading',{name:'手动管理账号操作'})).toBeTruthy();expect(screen.getByRole('tab',{name:'手动操作'})).toBeTruthy();expect(screen.getByRole('button',{name:'预览并准备操作'})).toBeTruthy();
  expect(fetch.mock.calls.some(([url])=>['/api/agents/run','/api/agents/settings','/api/agents/manual/prepare','/api/agents/publish'].includes(String(url)))).toBe(false);
 },30000);
 it('shows the latest real publication for the exact role and bound account, while preferring an explicit publication time',async()=>{
  const published={agent_id:'maya',account_id:12,action:'post',state:'published',body:'实际发布的内容'};
  const fixture={...state,agents:[agent],drafts:[
   {...published,id:'own-post',published_at:'2026-10-20T01:05:00Z'},
   {...published,id:'own-reply',action:'reply',state:undefined,status:'published',published_at:'2026-10-20T01:06:00Z'},
   {...published,id:'own-repost',action:'repost',published_at:'2026-10-20T01:04:00Z'},
   {...published,id:'following',action:'follow',published_at:'2026-10-20T02:00:00Z'},
   {...published,id:'unliking',action:'unlike',published_at:'2026-10-20T02:10:00Z'},
   {...published,id:'other-role',agent_id:'ethan',published_at:'2026-10-20T02:20:00Z'},
   {...published,id:'old-account',account_id:99,published_at:'2026-10-20T02:30:00Z'},
   {...published,id:'pending',state:'pending',published_at:'2026-10-20T02:40:00Z'},
   {...published,id:'already-reposted',action:'repost',result:{already_applied:true},published_at:'2026-10-20T02:50:00Z'},
   {...published,id:'already-applied',relationship_already_applied:true,published_at:'2026-10-20T03:00:00Z'},
   {...published,id:'unknown-account',account_id:undefined,published_at:'2026-10-20T03:10:00Z'},
  ]};
  setup(<Routes><Route path="/ai/agents/:agentId" element={<Agents/>}/></Routes>,'/ai/agents/maya',url=>url==='/api/agents'?fixture:undefined);
  await screen.findByRole('heading',{name:'Maya · 管理'});
  expect(screen.getByText('最近发布').parentElement?.textContent).toBe('最近发布2026/10/20 12:06');
  await act(async()=>{clients[clients.length-1].setQueryData(['/agents',{}],{...fixture,agents:[{...agent,last_publish_at:'2026-10-20T01:01:00Z'}]});});
  await waitFor(()=>expect(screen.getByText('最近发布').parentElement?.textContent).toBe('最近发布2026/10/20 12:01'));
 },30000);
 it('opens a role workspace and scopes its operation list without showing another role draft',async()=>{
  setup(<Routes><Route path="/ai/agents/:agentId" element={<Agents/>}/></Routes>,'/ai/agents/maya?tab=drafts');
  expect(await screen.findByRole('heading',{name:'Maya · 管理'})).toBeTruthy();expect(screen.getByRole('link',{name:'← 返回全部角色'}).getAttribute('href')).toBe('/ai/agents');
  expect(screen.getByRole('link',{name:'目标账号 #91 ↗'}).getAttribute('href')).toBe('/accounts?q=91');
  expect(screen.getByRole('button',{name:'确认关注'})).toBeTruthy();expect(screen.queryByText('其他角色的草稿')).toBeNull();expect(screen.queryByRole('textbox',{name:'Maya的草稿正文'})).toBeNull();
 },30000);
 it('offers a management link for each role',async()=>{
  setup(<Routes><Route path="/ai/agents" element={<Agents/>}/></Routes>,'/ai/agents');
  const links=await screen.findAllByRole('link',{name:'完整管理'});expect(links.map(link=>link.getAttribute('href'))).toEqual(['/ai/agents/maya','/ai/agents/ethan']);
 },30000);
 it.each(['follow','unfollow','unlike'] as const)('renders %s as a bodyless action and reconciles without a manually supplied post ID',async action=>{
  const publish=vi.fn().mockResolvedValue(true),reconcile=vi.fn().mockResolvedValue(true);
  setup(<DraftCard agent={agent as any} draft={{id:'action',agent_id:'maya',action,status:'uncertain',body:'',target_account_id:action==='unlike'?undefined:91,target_post_id:action==='unlike'?71:undefined}} busy={false} onPublish={publish} onDiscard={vi.fn()} onReconcile={reconcile}/>);
  expect(screen.queryByRole('spinbutton',{name:'核对帖子 ID'})).toBeNull();expect(screen.queryByRole('textbox')).toBeNull();fireEvent.click(screen.getByRole('button',{name:'核对远端操作'}));await waitFor(()=>expect(reconcile).toHaveBeenCalledWith('action',undefined));
 },30000);
});
describe('personal audit and relationship evidence',()=>{
 it('filters and pages exact-role logs independently and exports the same filters',async()=>{
  const fetch=setup(<AgentLogs agentId="maya" agentName="Maya"/>,'/agents/maya?tab=logs',url=>{
   if(!url.startsWith('/api/agents/maya/logs'))return undefined;
   const params=new URL('http://local'+url).searchParams;
   return {scope:'agent',retention:'since_audit_enabled',items:params.get('cursor')?[{id:3,agent_id:'maya',kind:'read',message:'第二页记录'}]:[{id:1,agent_id:'maya',kind:'read',message:'Maya 独立阅读记录',details:{timeout_seconds:60,stage_elapsed_seconds:3.5}},{id:2,agent_id:'ethan',kind:'read',message:'其他角色日志'}],total:2,has_more:!params.get('cursor'),next_cursor:params.get('cursor')?null:'page-2'};
  });
  expect(await screen.findByText('Maya 独立阅读记录')).toBeTruthy();expect(screen.queryByText('其他角色日志')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'Expand row'}));
  expect(screen.getByText('本次请求时限')).toBeTruthy();expect(screen.getByText('当前阶段用时')).toBeTruthy();expect(screen.queryByText('生成总时限')).toBeNull();
  fireEvent.change(screen.getByRole('searchbox',{name:'查找个人日志'}),{target:{value:'社区'}});fireEvent.click(screen.getByRole('button',{name:/查\s*找/}));
  await waitFor(()=>expect(fetch.mock.calls.some(([url])=>String(url).includes('q=%E7%A4%BE%E5%8C%BA'))).toBe(true));
  expect(screen.getByRole('link',{name:'导出当前筛选 CSV'}).getAttribute('href')).toBe('/api/agents/maya/logs/export?format=csv&q=%E7%A4%BE%E5%8C%BA');
  await waitFor(()=>expect((screen.getByRole('button',{name:'下一页'}) as HTMLButtonElement).disabled).toBe(false));fireEvent.click(screen.getByRole('button',{name:'下一页'}));expect(await screen.findByText('第二页记录')).toBeTruthy();fireEvent.click(screen.getByRole('button',{name:'上一页'}));expect(await screen.findByText('Maya 独立阅读记录')).toBeTruthy();
 },30000);
 it('does not display an unscoped response as personal audit history',async()=>{
  setup(<AgentLogs agentId="maya" agentName="Maya"/>,'/agents/maya',()=>({scope:'team',items:[{agent_id:'maya',message:'不允许展示这个未注明范围的记录'}],total:1}));
  expect(await screen.findByText('暂未收到此角色的独立日志')).toBeTruthy();expect(screen.queryByText('不允许展示这个未注明范围的记录')).toBeNull();
 },30000);
 it('distinguishes confirmed absent and unknown relationship state and links real targets',()=>{
  setup(<AgentRelationships relationships={{relations:[{kind:'follow',target_account_id:91,present:false,observed_at:'2026-10-20T03:00:00Z'},{kind:'like',target_post_id:71,present:null}],cooldown_seconds:21600}}/>);
  expect(screen.getByText('上次核实时不存在')).toBeTruthy();expect(screen.getByText('未知')).toBeTruthy();expect(screen.getByRole('link',{name:'账号 #91 ↗'}).getAttribute('href')).toBe('/accounts?q=91');expect(screen.getByRole('link',{name:'帖子 #71 ↗'}).getAttribute('href')).toBe('/posts?post=71');
  expect(screen.queryByText('反向操作冷却：',{exact:false})).toBeNull();
 });
});
