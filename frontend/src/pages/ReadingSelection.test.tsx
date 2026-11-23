// @vitest-environment jsdom
import {afterEach,beforeAll,describe,expect,it,vi} from 'vitest';
import {act,cleanup,configure,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter} from 'react-router-dom';
import ReadingSelection,{type ReadingSelectionState} from './ReadingSelection';
import type {Provider,Settings} from './aiProviders';

vi.mock('antd',async importOriginal=>{
 const original=await importOriginal<typeof import('antd')>();
 return {...original,Select:({options,value,onChange,id,disabled,'aria-label':label}:any)=><select id={id} aria-label={label} disabled={disabled} value={value??''} onChange={event=>onChange(event.target.value)}>{(options||[]).map((option:any)=><option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}</select>};
});
beforeAll(()=>{
 configure({asyncUtilTimeout:10000});
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const original=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>original(element));
});
let client:QueryClient;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});
const provider=(id:string,extra:Partial<Provider>={}):Provider=>({id,name:id==='default'?'工作台文字服务':'独立筛选服务',model:`${id}-model`,protocol:'openai_chat',base_url:'http://127.0.0.1:10001/v1',enabled:true,is_default:id==='default',has_key:true,...extra});
const snapshot=():ReadingSelectionState=>({settings:{provider_id:'',revision:2,updated_at:'2026-10-20T02:00:00Z'},provider:provider('default'),limits:{max_candidates:80,max_selected:8,cache_seconds:300},roles:[],recent_calls:[]});
function setup(initial=snapshot(),settings:Settings={providers:[provider('default'),provider('selector'),provider('disabled',{enabled:false}),provider('embedding',{protocol:'openai_embeddings'})],default_provider_id:'default'},failure?:string){
 let state=initial,currentSettings=settings,fail=failure;
 const fetch=vi.fn(async(input:RequestInfo|URL,options?:RequestInit)=>{const path=String(input),body=options?.body?JSON.parse(String(options.body)):undefined;let data:unknown,status=200;
  if(path==='/api/bootstrap')data={csrf_token:'token'};
  else if(path==='/api/agents/reading-selection'&&options?.method==='POST'){if(fail){data={error:fail};status=409;fail=undefined;}else{const selected=currentSettings.providers.find(item=>item.id===(body.provider_id||currentSettings.default_provider_id));state={...state,settings:{provider_id:body.provider_id,revision:state.settings.revision+(body.provider_id!==state.settings.provider_id?1:0)},provider:selected||null};data=state;}}
  else if(path==='/api/agents/reading-selection')data=state;
  else if(path==='/api/ai/providers')data=currentSettings;
  else if(path==='/api/ai/providers/save'){const created={...provider('created'),...body};currentSettings={...currentSettings,providers:[...currentSettings.providers,created]};data=currentSettings;}
  else throw new Error(`Unexpected request: ${path}`);
  return new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
 });
 vi.stubGlobal('fetch',fetch);client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false},mutations:{retry:false}}});
 render(<QueryClientProvider client={client}><MemoryRouter><ReadingSelection/></MemoryRouter></QueryClientProvider>);
 return {fetch,setState:(next:ReadingSelectionState)=>{state=next;},posted:(path='/api/agents/reading-selection')=>fetch.mock.calls.filter(([url,options])=>url===path&&options?.method==='POST').map(([,options])=>JSON.parse(String(options?.body)))};
}

it('has no reading-model setting and only reads records',async()=>{
 const {fetch}=setup();
 expect(await screen.findByText(/按模型选读的方式已经移除/)).toBeTruthy();
 expect(screen.queryByRole('combobox')).toBeNull();expect(screen.queryByRole('button',{name:/保存筛选设置/})).toBeNull();
 expect(await screen.findByText('尚无角色，先在 Agent 中创建角色。')).toBeTruthy();expect(screen.getByText('尚无筛选记录。角色开始阅读后会在这里留下记录。')).toBeTruthy();
 expect(fetch.mock.calls.every(([url,options])=>String(url)==='/api/agents/reading-selection'&&!options?.method)).toBe(true);
});

it('keeps previews, deep sources, context and historical model evidence distinct',async()=>{
 const state=snapshot();state.roles=[{id:'maya',name:'Maya',enabled:true,mode:'review',last_selection_at:'2026-10-20T02:00:00Z',selection:{status:'selected',candidate_count:12,selected_count:3,cache_hit:true,cache_mode:'handoff',reason:'关注社区交通',settings_revision:1},source_count:5,context:{included_count:3,background_count:2,missing_count:1,deferred_count:0}}];
 state.recent_calls=['empty','fallback','deferred','error','cancelled'].map((status,index)=>({id:`call-${index}`,at:'2026-10-20T02:00:00Z',agent_id:'maya',agent_name:'Maya',status,candidate_count:12,selected_count:status==='fallback'?2:0,cache_hit:status==='empty',provider_name:'历史筛选连接',model:'old-screen-model',settings_revision:1,error:status==='fallback'?'筛选结果无效，已采用本地回退。':undefined}));
 setup(state);await screen.findByRole('heading',{name:'各角色最近一次阅读'});const rolePanel=screen.getByRole('heading',{name:'各角色最近一次阅读'}).closest('section')!;
 expect(within(rolePanel).getByText('候选目录 12 条')).toBeTruthy();expect(within(rolePanel).getByText('筛选选中 3 条')).toBeTruthy();expect(within(rolePanel).getByText('深读来源 5 条')).toBeTruthy();expect(within(rolePanel).getByText('沿用待决策资料')).toBeTruthy();expect(within(rolePanel).getByText(/父帖或根帖背景 2 条 · 缺失上下文 1 处/)).toBeTruthy();
 expect(screen.getByText('本轮未选中')).toBeTruthy();expect(screen.getByText('筛选失败，已回退')).toBeTruthy();expect(screen.getByText('调用暂缓')).toBeTruthy();expect(screen.getByText('调用失败')).toBeTruthy();expect(screen.getByText('已取消')).toBeTruthy();expect(screen.getByText('复用筛选缓存')).toBeTruthy();expect(screen.getAllByText('旧配置 · v1')).toHaveLength(6);expect(screen.queryByText('历史筛选连接')).toBeNull();expect(screen.queryByText(/已读.*12/)).toBeNull();
});

it('labels legacy selection as unversioned and does not invent completed thread context',async()=>{
 const state=snapshot();state.provider_error=null;state.roles=[{id:'old',name:'旧角色',enabled:false,mode:'review',last_selection_at:null,selection:{status:'fallback',candidate_count:4,selected_count:1,reason:'已采用本地回退。',error:'已采用本地回退。'},source_count:null,context:{}},{id:'new',name:'新角色',enabled:false,mode:'auto',last_selection_at:null,selection:null,source_count:null,context:{}}];setup(state);
 await screen.findByRole('heading',{name:'各角色最近一次阅读'});expect(screen.getByText('历史记录 · 未记录筛选配置')).toBeTruthy();expect(screen.getAllByText('尚无本轮深读记录')).toHaveLength(2);expect(screen.queryByText(/纳入目标 0 条/)).toBeNull();expect(screen.getAllByText('已采用本地回退。')).toHaveLength(1);expect(screen.getByText('尚无筛选记录')).toBeTruthy();
});

it('distinguishes fallback reading from model selection and updates the role when selection recovers',async()=>{
 const state=snapshot(),fallback={status:'fallback',candidate_count:48,selected_count:12,settings_revision:2,reason:'选读暂未完成，已使用备用资料目录。'};
 state.roles=[{id:'maya',name:'Maya',enabled:true,mode:'auto',selection:fallback,source_count:13}];
 state.recent_calls=[{...fallback,id:'failed',at:'2026-10-20T02:00:00Z',agent_id:'maya',agent_name:'Maya'}];
 const {setState,posted}=setup(state);await screen.findByRole('heading',{name:'各角色最近一次阅读'});
 const rolePanel=screen.getByRole('heading',{name:'各角色最近一次阅读'}).closest('section')!,callsPanel=screen.getByRole('heading',{name:'最近筛选记录'}).closest('section')!;
 expect(within(rolePanel).getByText('回退选用 12 条')).toBeTruthy();expect(within(callsPanel).getByText(/回退选用 12 条/)).toBeTruthy();expect(screen.queryByText(/筛选选中 12 条/)).toBeNull();
 const recovered={status:'selected',candidate_count:49,selected_count:8,settings_revision:2,reason:'筛选已恢复。'};
 setState({...state,roles:[{...state.roles[0],selection:recovered,source_count:9}],recent_calls:[{...recovered,id:'recovered',at:'2026-10-20T02:01:00Z',agent_id:'maya',agent_name:'Maya'},...state.recent_calls]});
 fireEvent.click(screen.getByRole('button',{name:'刷新记录'}));
 await waitFor(()=>expect(within(rolePanel).getByText('筛选选中 8 条')).toBeTruthy());
 expect(within(rolePanel).getByText('已选中资料')).toBeTruthy();expect(within(rolePanel).queryByText(/回退选用/)).toBeNull();
 expect(within(callsPanel).getByText(/筛选选中 8 条/)).toBeTruthy();expect(within(callsPanel).getByText(/回退选用 12 条/)).toBeTruthy();expect(within(callsPanel).getByText('筛选失败，已回退')).toBeTruthy();expect(posted()).toEqual([]);
});

it('renders string diagnostics as text and safely ignores structured error values',async()=>{
 const state=snapshot(),unsafeMarkup='<img src=x onerror="window.selectionError=true">';
 state.roles=[{id:'maya',name:'Maya',enabled:true,mode:'auto',selection:{status:'fallback',reason:{message:'structured reason'},error:{message:'structured error'}}}];
 state.recent_calls=[{id:'failed',at:'2026-10-20T02:00:00Z',agent_id:'maya',agent_name:'Maya',status:'fallback',reason:unsafeMarkup,error:unsafeMarkup}];
 setup(state);await screen.findByRole('heading',{name:'各角色最近一次阅读'});
 const diagnostics=screen.getByText(unsafeMarkup);expect(diagnostics.textContent).toBe(unsafeMarkup);expect(diagnostics.querySelector('img')).toBeNull();expect(screen.getAllByText(unsafeMarkup)).toHaveLength(1);expect(screen.queryByText('structured reason')).toBeNull();expect(screen.queryByText('structured error')).toBeNull();
});

it('shows read failures with a retry that performs only GET requests',async()=>{
 let fail=true;const fetch=vi.fn(async(input:RequestInfo|URL)=>{const path=String(input),data=path==='/api/ai/providers'?{providers:[]}:fail?{error:'筛选配置暂时无法读取。'}:snapshot();return new Response(JSON.stringify(data),{status:path.includes('reading-selection')&&fail?503:200,headers:{'Content-Type':'application/json'}});});vi.stubGlobal('fetch',fetch);client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><MemoryRouter><ReadingSelection/></MemoryRouter></QueryClientProvider>);expect(await screen.findByText('筛选配置暂时无法读取。')).toBeTruthy();fail=false;fireEvent.click(screen.getByRole('button',{name:/重\s*试/}));await screen.findByRole('heading',{name:'各角色最近一次阅读'});expect(fetch.mock.calls.every(([url])=>String(url)==='/api/ai/providers'||String(url)==='/api/agents/reading-selection')).toBe(true);
});
