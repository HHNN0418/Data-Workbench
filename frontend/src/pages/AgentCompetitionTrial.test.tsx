// @vitest-environment jsdom
import {afterEach,beforeAll,describe,expect,it,vi} from 'vitest';
import {act,cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter,Route,Routes,useLocation,useNavigate} from 'react-router-dom';
import AgentCompetitionTrial,{type CompetitionSnapshot} from './AgentCompetitionTrial';
import Agents from './Agents';

vi.mock('antd',async importOriginal=>{
 const original=await importOriginal<typeof import('antd')>();
 return {...original,Select:({options,value,onChange,disabled,'aria-label':label}:any)=><select aria-label={label} disabled={disabled} value={value??''} onChange={event=>onChange(event.target.value)}>{(options||[]).map((option:any)=><option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}</select>};
});
beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
const profile={id:'maya',name:'Maya',username:'maya',enabled:true,mode:'auto' as const,provider_id:'base',account_id:12,account_state:'ready',allowed_actions:['post'],interval_seconds:900};
const participant={agent_id:'maya',name:'Maya',account_id:12,status:'off',reasons:[],score:null,known_component_subtotal:null,eligible_posts:0,due_posts:0,coverage:null,qualified:false,share:null,target_share:null,model_tier:0,enhanced_ratio:0,protected_until:null,protection_remaining_seconds:0};
const snapshot:CompetitionSnapshot={kind:'agent_competition',revision:0,settings:{mode:'off',enhanced_provider_id:null,premium_provider_id:null,enhanced_hourly_calls:10,enhanced_max_output_tokens:1800},enabled:false,generated_at:1792483200,last_settled_at:null,allocation_epoch:null,agents:[participant],rounds:[],daily_views:[],notes:[],metric_weights:{views:1,likes:5,reposts:10,replies:15,follows:20,unfollows:-20},excluded_metrics:['follows','unfollows']};
const provider={id:'base',name:'基础服务',base_url:'https://example.test/v1',model:'base-model',protocol:'openai_chat',has_key:true,enabled:true,is_default:true,competition_tier:'standard'};
const providers={default_provider_id:'base',providers:[provider,{...provider,id:'enhanced',name:'增强服务',model:'enhanced-model',is_default:false,competition_tier:'enhanced'},{...provider,id:'premium',name:'顶级服务',model:'premium-model',is_default:false,competition_tier:'premium'},{...provider,id:'embedding',name:'不应出现的向量服务',protocol:'openai_embeddings'},{...provider,id:'missing-key',name:'不应出现的未配密钥',has_key:false}]};
function Location(){const location=useLocation(),navigate=useNavigate();return <><output aria-label="当前地址">{location.pathname+location.search}</output><button onClick={()=>navigate(-1)}>浏览器后退</button></>;}
function setup(options:{snapshot?:CompetitionSnapshot;raw?:unknown;getStatus?:number;reject?:string;route?:string}={}){
 let state=options.snapshot??snapshot;
 const agentState={settings:{enabled:false,team_side:'Vote Yes',max_accounts:10},agents:[profile],drafts:[],events:[],has_team_code:false,sdk_available:true};
 const fetch=vi.fn((input:RequestInfo|URL,init?:RequestInit)=>{
  const path=String(input);
  if(path==='/api/agents/competition'){
   if(init?.method==='POST'){
    if(options.reject)return Promise.resolve(new Response(JSON.stringify({error:options.reject}),{status:409,headers:{'Content-Type':'application/json'}}));
    const {expected_revision:_,...patch}=JSON.parse(String(init.body));
    state={...state,revision:state.revision+1,settings:{...state.settings,...patch},enabled:(patch.mode??state.settings.mode)!=='off'};
   }
   return Promise.resolve(new Response(JSON.stringify(options.raw??state),{status:options.getStatus??200,headers:{'Content-Type':'application/json'}}));
  }
  const data=path==='/api/agents'?agentState:path==='/api/bootstrap'?{csrf_token:'test'}:path==='/api/ai/providers'?providers:path==='/api/agents/quotas'?{scope:'whole_team',allocation:'shared_on_demand',usage_window_seconds:60,agents:[],buckets:[]}:null;
  return Promise.resolve(new Response(JSON.stringify(data??{error:'unexpected endpoint'}),{status:data?200:404,headers:{'Content-Type':'application/json'}}));
 });
 vi.stubGlobal('fetch',fetch);
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[options.route??'/ai/agents?tab=quotas']}>{options.route?<><Location/><Routes><Route path="/ai/agents" element={<Agents/>}/><Route path="/ai/agents/:agentId" element={<Agents/>}/></Routes></>:<AgentCompetitionTrial agents={[profile]} teamEnabled={false}/>}</MemoryRouter></QueryClientProvider>);
 return {fetch,client};
}
function posted(fetch:ReturnType<typeof setup>['fetch']){
 return fetch.mock.calls.filter(([,init])=>init?.method==='POST').map(([path,init])=>({path,payload:JSON.parse(String(init?.body))}));
}
describe('agent competition control',()=>{
 it('loads disabled by default and persists only competition settings when toggled with a paused team',async()=>{
  const {fetch}=setup();
  expect(await screen.findByText('关闭竞赛')).toBeTruthy();
  expect(screen.getByRole('switch',{name:'开启发文竞赛'}).getAttribute('aria-checked')).toBe('false');
  expect(screen.getAllByText('队伍持续运行已暂停').length).toBeGreaterThan(0);
  expect(posted(fetch)).toEqual([]);
  fireEvent.click(screen.getByRole('switch',{name:'开启发文竞赛'}));
  await waitFor(()=>expect(posted(fetch)).toEqual([{path:'/api/agents/competition',payload:{expected_revision:0,mode:'resources'}}]));
  expect(await screen.findByText('按竞赛份额调度')).toBeTruthy();
  expect(screen.getByRole('switch',{name:'开启发文竞赛'}).getAttribute('aria-checked')).toBe('true');
  fireEvent.click(screen.getByRole('switch',{name:'开启发文竞赛'}));
  await waitFor(()=>expect(posted(fetch).at(-1)?.payload).toEqual({expected_revision:1,mode:'off'}));
  expect(posted(fetch).every(row=>row.path==='/api/agents/competition')).toBe(true);
 },30000);
 it('enables model competition when an enhanced connection was already configured',async()=>{
  const {fetch}=setup({snapshot:{...snapshot,settings:{...snapshot.settings,enhanced_provider_id:'enhanced'}}});
  await screen.findByText('关闭竞赛');fireEvent.click(screen.getByRole('switch',{name:'开启发文竞赛'}));
  await waitFor(()=>expect(posted(fetch)[0]?.payload).toEqual({expected_revision:0,mode:'models'}));
 },30000);
 it('saves explicit enhanced and premium connections with bounded shared reward settings',async()=>{
  const {fetch}=setup();await screen.findByText('关闭竞赛');
  fireEvent.click(screen.getByText('竞赛模式与模型奖励设置'));
  fireEvent.change(screen.getByLabelText('竞赛运行模式'),{target:{value:'models'}});
  fireEvent.change(screen.getByLabelText('增强文字模型连接'),{target:{value:'enhanced'}});
  fireEvent.change(screen.getByLabelText('顶级文字模型连接'),{target:{value:'premium'}});
  expect(screen.queryByRole('option',{name:/不应出现的向量服务/})).toBeNull();
  expect(screen.queryByRole('option',{name:/不应出现的未配密钥/})).toBeNull();
  expect(screen.getAllByRole('option',{name:'[顶级] 顶级服务 · premium-model'})).toHaveLength(2);
  fireEvent.change(screen.getByRole('spinbutton',{name:'增强模型每小时调用上限'}),{target:{value:'12'}});
  fireEvent.click(screen.getByRole('button',{name:'保存竞赛设置'}));
  await waitFor(()=>expect(posted(fetch)[0]?.payload).toEqual({expected_revision:0,mode:'models',enhanced_provider_id:'enhanced',premium_provider_id:'premium',enhanced_hourly_calls:12}));
 },30000);
 it('keeps an unknown score distinct from valid zero and displays backend protection and coverage reasons',async()=>{
  const now=Math.floor(Date.now()/1000);
  setup({snapshot:{...snapshot,enabled:true,settings:{...snapshot.settings,mode:'models',enhanced_provider_id:'enhanced',premium_provider_id:'premium'},agents:[
   {...participant,status:'protected',reasons:['首次自动就绪后的保护期'],protected_until:now+1800,protection_remaining_seconds:1800,share:0.4,target_share:0.5},
   {...participant,agent_id:'zero',name:'Zero',status:'eligible',score:0,rank:1,qualified:true,eligible_posts:3,due_posts:3,coverage:1,share:0.6,model_tier:2,enhanced_ratio:0.8},
  ]}});
  await screen.findByText('首次自动就绪后的保护期');
  const protectedRow=screen.getByRole('link',{name:'Maya'}).closest('tr')!;
  expect(within(protectedRow).getByText('待取得完整观测')).toBeTruthy();
  expect(within(protectedRow).queryByText('0',{exact:true})).toBeNull();
  expect(within(protectedRow).getByText(/剩余 29 分|剩余 30 分/)).toBeTruthy();
  const zeroRow=screen.getByRole('link',{name:'Zero'}).closest('tr')!;
  expect(within(zeroRow).getByText('0',{exact:true})).toBeTruthy();
  expect(within(zeroRow).getByText('60%')).toBeTruthy();
  expect(within(zeroRow).getByText('加强 · 奖励调用 80%')).toBeTruthy();
  expect(within(zeroRow).getByText('顶级：顶级服务 · premium-model')).toBeTruthy();
 },30000);
 it('accepts score-only hourly results and keeps current allocation when selecting historical scores',async()=>{
  const result={agent_id:'maya',account_id:12,status:'eligible',reasons:[],score:32,known_component_subtotal:32,eligible_posts:3,due_posts:3,coverage:1,qualified:true,rank:1};
  const oldRound={round_id:'1792476000',start_at:1792476000,end_at:1792479600,sealed_at:1792480200,revision:1,results:[{...result,round_id:'1792476000',score:18}]};
  const latestRound={round_id:'1792479600',start_at:1792479600,end_at:1792483200,sealed_at:1792483800,revision:1,results:[{...result,round_id:'1792479600'}]};
  setup({snapshot:{...snapshot,enabled:true,settings:{...snapshot.settings,mode:'resources'},agents:[{...participant,...result,share:0.65}],rounds:[oldRound,latestRound]}});
  await screen.findByText('小时成绩与当前资源');
  const row=screen.getByRole('link',{name:'Maya'}).closest('tr')!;
  expect(within(row).getByText('32',{exact:true})).toBeTruthy();
  expect(within(row).getByText('65%')).toBeTruthy();
  expect((screen.getByLabelText('选择竞赛小时') as HTMLSelectElement).value).toBe(latestRound.round_id);
  fireEvent.change(screen.getByLabelText('选择竞赛小时'),{target:{value:oldRound.round_id}});
  expect(within(row).getByText('18',{exact:true})).toBeTruthy();
  expect(within(row).getByText('65%')).toBeTruthy();
  expect(screen.queryByText('竞赛状态暂不可用')).toBeNull();
 },30000);
 it('preserves edited settings and their original revision after a configuration conflict',async()=>{
  const {fetch,client}=setup({reject:'竞赛设置已被修改，请刷新'});
  await screen.findByText('关闭竞赛');fireEvent.click(screen.getByText('竞赛模式与模型奖励设置'));
  fireEvent.change(screen.getByLabelText('竞赛运行模式'),{target:{value:'observe'}});
  await act(async()=>{client.setQueryData(['/agents/competition',{}],{...snapshot,revision:4});});
  fireEvent.click(screen.getByRole('button',{name:'保存竞赛设置'}));
  expect(await screen.findByText('竞赛设置已被修改，请刷新')).toBeTruthy();
  expect(posted(fetch)[0].payload.expected_revision).toBe(0);
  expect((screen.getByLabelText('竞赛运行模式') as HTMLSelectElement).value).toBe('observe');
  expect(screen.getByRole('switch',{name:'开启发文竞赛'}).getAttribute('aria-checked')).toBe('false');
 },30000);
 it('rejects an unrelated successful response instead of pretending competition is available',async()=>{
  const {fetch}=setup({raw:{settings:{enabled:false},agents:[]}});
  expect(await screen.findByText('竞赛状态暂不可用')).toBeTruthy();
  expect((screen.getByRole('switch',{name:'开启发文竞赛'}) as HTMLButtonElement).disabled).toBe(true);
  expect(posted(fetch)).toEqual([]);
 },30000);
 it('honors the quota deep link and synchronizes browser back without changing personal tabs',async()=>{
  const {fetch}=setup({route:'/ai/agents?tab=quotas'});
  expect(await screen.findByRole('region',{name:'发文竞赛与资源'})).toBeTruthy();
  expect(screen.getByRole('tab',{name:'额度管理'}).getAttribute('aria-selected')).toBe('true');
  fireEvent.click(screen.getByRole('tab',{name:'队伍设置'}));
  await waitFor(()=>expect(screen.getByLabelText('当前地址').textContent).toBe('/ai/agents?tab=settings'));
  fireEvent.click(screen.getByRole('button',{name:'浏览器后退'}));
  await waitFor(()=>expect(screen.getByRole('tab',{name:'额度管理'}).getAttribute('aria-selected')).toBe('true'));
  expect(posted(fetch)).toEqual([]);
  expect(fetch.mock.calls.some(([path])=>path==='/api/ai/research')).toBe(false);
 },30000);
});
