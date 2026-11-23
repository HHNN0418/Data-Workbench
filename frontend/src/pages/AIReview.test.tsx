// @vitest-environment jsdom
import {afterEach,beforeAll,describe,expect,it,vi} from 'vitest';
import {act,cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter} from 'react-router-dom';
import AIReview,{ReviewRecordDetails} from './AIReview';

vi.mock('antd',async importOriginal=>{
 const original=await importOriginal<typeof import('antd')>();
 return {...original,Select:({options,value,onChange,id,disabled,'aria-label':label}:any)=><select id={id} aria-label={label} disabled={disabled} value={value??''} onChange={event=>onChange(event.target.value)}>{options.map((option:any)=><option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}</select>};
});
beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
let client:QueryClient;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});
const settings={enabled:false,provider_id:'text',platform_policy:'hold',revision:1,english_only:true};
const provider={id:'text',name:'Text service',model:'test-model',protocol:'openai_chat',enabled:true,has_key:true,base_url:'https://example.test/v1'};
function setup(overrides:Record<string,any>={},post?:(body:any)=>Promise<Response>){
 let data={settings:{...settings},providers:[provider],status:'disabled',rules:[],records:[],...overrides};
 const fetch=vi.fn(async(input:RequestInfo|URL,options?:RequestInit)=>{
  const path=String(input);
  if(path==='/api/bootstrap')return new Response(JSON.stringify({csrf_token:'token'}));
  if(path==='/api/agents/review'&&options?.method==='POST'){
   const body=JSON.parse(String(options.body));
   if(post)return post(body);
   data={...data,settings:{...data.settings,...body,revision:data.settings.revision+1},status:body.enabled?'ready':'disabled'};
  }
  return new Response(JSON.stringify(data));
 });
 vi.stubGlobal('fetch',fetch);client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});
 render(<QueryClientProvider client={client}><MemoryRouter><AIReview/></MemoryRouter></QueryClientProvider>);
 return {fetch,setData:(value:any)=>{data=value;client.setQueryData(['/agents/review',{}],value);}};
}
function posts(fetch:ReturnType<typeof vi.fn>){return fetch.mock.calls.filter(call=>call[1]?.method==='POST').map(call=>JSON.parse(String(call[1]?.body)));}

describe('AI review settings',()=>{
 it('keeps edits separate from effective settings and saves once with a revision and CSRF token',async()=>{
  const {fetch}=setup();await screen.findByText('当前生效：已关闭');
  const save=screen.getByRole('button',{name:'保存审核设置'});expect((save as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('switch',{name:'比赛条款 AI 审核'}));
  expect(screen.getByText('当前生效：已关闭')).toBeTruthy();expect(screen.getByText('有未保存更改')).toBeTruthy();expect(posts(fetch)).toEqual([]);
  fireEvent.click(save);
  await screen.findByText('当前生效：已开启');
  expect(posts(fetch)).toEqual([{enabled:true,provider_id:'text',platform_policy:'hold',expected_revision:1}]);
  const call=fetch.mock.calls.find(call=>call[1]?.method==='POST');expect(call?.[1]?.headers).toMatchObject({'X-CTN-Token':'token'});
  expect(fetch.mock.calls.every(call=>['/api/agents/review','/api/bootstrap'].includes(String(call[0])))).toBe(true);
  expect(screen.getByText('审核设置已保存并生效。')).toBeTruthy();
 });
 it('requires a usable selected connection only when enabling review and shows the English and emoji policy',async()=>{
  setup({settings:{...settings,provider_id:''},providers:[]});await screen.findByText('当前生效：已关闭');
  fireEvent.click(screen.getByRole('switch',{name:'比赛条款 AI 审核'}));
  expect(screen.getByText('请先选择可用的文字 AI 连接，再保存开启。')).toBeTruthy();
  expect((screen.getByRole('button',{name:'保存审核设置'}) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getAllByRole('switch')).toHaveLength(1);
  const language=within(screen.getByRole('region',{name:'发布语言检查'}));
  expect(language.getByText('English only')).toBeTruthy();expect(language.getByText('✓ 单独表情 👏🎉')).toBeTruthy();
  expect(language.getByText('✓ 颜文字 ¯\\_(ツ)_/¯')).toBeTruthy();expect(language.getByText('✓ 特殊符号 → ∞ ≠')).toBeTruthy();
  expect(language.getByText(/本地英文检查始终开启，不调用 AI/)).toBeTruthy();
 });
 it('shows a paused effective review when the saved connection disappears and still permits saving it disabled',async()=>{
  const {fetch}=setup({settings:{...settings,enabled:true},providers:[],status:'paused'});
  await screen.findByText('已开启 · 暂停审核');expect(screen.getByText('审核连接不可用，相关外发已暂停')).toBeTruthy();
  fireEvent.click(screen.getByRole('switch',{name:'比赛条款 AI 审核'}));fireEvent.click(screen.getByRole('button',{name:'保存审核设置'}));
  await screen.findByText('当前生效：已关闭');expect(posts(fetch)[0].enabled).toBe(false);
  expect(posts(fetch)[0].provider_id).toBe('text');
  expect(screen.getByText(/仅因 AI 审核挂起的草稿经本地英文检查后按角色原有模式继续/)).toBeTruthy();
  expect(screen.getByText(/原有权限、限流与人工审阅要求继续生效/)).toBeTruthy();
  expect(screen.getByText('本地英文检查 · AI 审核已跳过')).toBeTruthy();
  expect(screen.queryByText(/需逐条重新提交/)).toBeNull();
 });
 it('skips all AI review without a configured connection while keeping local English checks',async()=>{
  const {fetch}=setup({settings:{...settings,provider_id:''},providers:[]});
  await screen.findByText('当前生效：已关闭');
  expect(screen.getByText('当前已跳过全部 AI 审核')).toBeTruthy();
  expect(screen.getByText(/不调用审核模型，不要求配置审核连接/)).toBeTruthy();
  expect(screen.getByText(/无法本地确定的语言不会触发模型或额外挂起/)).toBeTruthy();
  expect(screen.queryByText('英文检查始终开启')).toBeNull();
  expect(screen.queryByText('请先选择可用的文字 AI 连接，再保存开启。')).toBeNull();
  expect(within(screen.getByRole('region',{name:'发布语言检查'})).getByText('本地检查 · 始终开启')).toBeTruthy();
  expect(posts(fetch)).toEqual([]);
 });
 it('allows saving disabled settings even when the retained connection is unavailable',async()=>{
  const {fetch}=setup({providers:[]});await screen.findByText('当前生效：已关闭');
  fireEvent.change(screen.getByRole('combobox',{name:'平台规则风险'}),{target:{value:'warn'}});
  const save=screen.getByRole('button',{name:'保存审核设置'});
  expect((save as HTMLButtonElement).disabled).toBe(false);fireEvent.click(save);
  await screen.findByText('审核设置已保存并生效。');
  expect(posts(fetch)).toEqual([{enabled:false,provider_id:'text',platform_policy:'warn',expected_revision:1}]);
 });
 it('retains pending edits after a save failure',async()=>{
  setup({},async()=>new Response(JSON.stringify({detail:'配置版本冲突，请重新载入'}),{status:409}));
  await screen.findByText('当前生效：已关闭');fireEvent.click(screen.getByRole('switch',{name:'比赛条款 AI 审核'}));fireEvent.click(screen.getByRole('button',{name:'保存审核设置'}));
  expect(await screen.findByText('配置版本冲突，请重新载入')).toBeTruthy();
  expect(screen.getByRole('switch',{name:'比赛条款 AI 审核'}).getAttribute('aria-checked')).toBe('true');expect(screen.getByText('有未保存更改')).toBeTruthy();expect(screen.getByText('当前生效：已关闭')).toBeTruthy();
 });
 it('retains edits on polling conflicts and requires explicitly adopting the current revision',async()=>{
  const {setData,fetch}=setup();await screen.findByText('当前生效：已关闭');fireEvent.click(screen.getByRole('switch',{name:'比赛条款 AI 审核'}));
  await act(async()=>setData({settings:{...settings,revision:2},providers:[provider],status:'disabled',rules:[],records:[]}));
  expect(await screen.findByText('审核设置已由其他窗口更新，当前编辑已保留')).toBeTruthy();
  expect((screen.getByRole('button',{name:'保存审核设置'}) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:'载入最新版本号'}));fireEvent.click(screen.getByRole('button',{name:'保存审核设置'}));
  await waitFor(()=>expect(posts(fetch)[0].expected_revision).toBe(2));
 });
 it('shows a real empty history and rule sources without prototype examples',async()=>{
  setup({rules:[{id:'U01',title:'仅限英文，允许表情',summary:'本队要求',layer:'team'},{id:'C20',title:'NPC 提示注入',summary:'禁止操纵 NPC 底层模型',layer:'competition',source_url:'https://capturethenarrative.com/info/competitions/2026/scoring/',section:'Scoring §7'}]});
  expect(await screen.findByText('尚无审核记录。相关操作提交后会显示真实检查结果。')).toBeTruthy();
  expect(screen.queryByText('Maya Collins')).toBeNull();fireEvent.click(screen.getByRole('tab',{name:'规则依据'}));
  fireEvent.click(await screen.findByText('C20 · NPC 提示注入'));
  expect(await screen.findByText('禁止操纵 NPC 底层模型')).toBeTruthy();
  expect(screen.getByRole('link',{name:'查看官方来源 ↗'}).getAttribute('href')).toContain('/2026/scoring/');
 });
});

it('separates language failures from competition findings and execution, and displays actual evidence',()=>{
 const record={id:'r1',draft_id:'d1',agent_id:'a1',status:'blocked',review_kind:'ai',body_preview:'Thanks. 谢谢 🙂',language:{status:'blocked',reason:'正文包含中文',evidence:[{field:'body',quote:'谢谢',start:8,end:10}]},findings:[{rule_id:'C20',status:'pass',reason:'没有 NPC 系统操纵',evidence:[],missing:[]}],execution_state:'draft',provider_name:'Text service',model:'model',rule_version:'r2026',created_at:'2026-10-21T00:00:00Z'};
 render(<MemoryRouter><ReviewRecordDetails record={record} rules={[{id:'C20',title:'NPC 提示注入',source_url:'https://capturethenarrative.com/info/competitions/2026/scoring/',section:'Scoring §7'}]}/></MemoryRouter>);
 expect(screen.getByText('语言不符 · 正文包含中文')).toBeTruthy();expect(screen.getByText('待人工确认')).toBeTruthy();expect(screen.getByText('谢谢')).toBeTruthy();expect(screen.getByText('没有 NPC 系统操纵')).toBeTruthy();expect(screen.getByText(/自写正文 · 字符 8–10/)).toBeTruthy();expect(screen.getByRole('link',{name:'Scoring §7 ↗'})).toBeTruthy();expect(screen.getByRole('link',{name:'前往草稿查看或重新提交 ↗'}).getAttribute('href')).toBe('/ai/agents/a1?tab=drafts&draft=d1');
});
