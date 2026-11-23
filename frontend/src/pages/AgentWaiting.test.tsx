// @vitest-environment jsdom
import {afterEach,beforeAll,describe,expect,it,vi} from 'vitest';
import {act,cleanup,render,screen,waitFor} from '@testing-library/react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter,Route,Routes} from 'react-router-dom';
import Agents from './Agents';

vi.mock('./AgentResearchReference',()=>({default:()=>null}));
beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn().mockImplementation(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
const role={id:'maya',name:'Maya',username:'maya_collins',enabled:true,mode:'auto',account_id:12,interval_seconds:1800,busy:false};
const waiting={id:'job-1',status:'waiting',waiting_for:'publish',wait_kind:'budget_wait',wait_message:'队伍发布额度暂不可用，已保留本轮内容。',retry_at:Date.parse('2026-10-20T15:36:20Z')/1000,draft_id:'draft-1'};
const initial={settings:{enabled:true,team_side:'Vote Yes',max_accounts:3,block_agent_interactions:true},agents:[role],drafts:[],events:[],has_team_code:true,sdk_available:true};
function setup(agent:Record<string,unknown>,route='/ai/agents'){
 const state={...initial,agents:[{...role,...agent}]};
 vi.stubGlobal('fetch',vi.fn((input:RequestInfo|URL)=>{
  const url=String(input),data=url==='/api/ai/providers'?{providers:[{id:'p',enabled:true,protocol:'openai_chat',name:'AI',model:'m'}]}:url==='/api/bootstrap'?{csrf_token:'test'}:url==='/api/agents'?state:{};
  return Promise.resolve(new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}}));
 }));
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[route]}><Routes><Route path="/ai/agents" element={<Agents/>}/><Route path="/ai/agents/:agentId" element={<Agents/>}/></Routes></MemoryRouter></QueryClientProvider>);
 return {client,state};
}
function decisionButton(){return screen.getByRole('button',{name:/阅读并决策$/}) as HTMLButtonElement;}

describe('agent waiting state and operation progress',()=>{
 it('shows saved content, the quota reason and retry time without an old failure or thinking spinner',async()=>{
  setup({error:'上一轮的历史错误。',generation:waiting});
  expect(await screen.findByText('内容已生成，等待执行')).toBeTruthy();
  expect(screen.getByText(waiting.wait_message)).toBeTruthy();
  expect(screen.getByText('下次重试时间：2026/10/21 02:36。',{exact:false})).toBeTruthy();
  expect(screen.getByRole('link',{name:'查看待执行操作 ↗'}).getAttribute('href')).toBe('/ai/agents/maya?tab=drafts');
  expect(screen.queryByText('本轮决策失败')).toBeNull();expect(screen.queryByText('上一轮的历史错误。')).toBeNull();
  expect(screen.queryByRole('button',{name:'取消本次决策'})).toBeNull();
  await waitFor(()=>expect(decisionButton().disabled).toBe(false));
  expect(decisionButton().classList.contains('ant-btn-loading')).toBe(false);
 },30000);
 it('distinguishes waiting for a decision from already generated content',async()=>{
  setup({generation:{status:'waiting',waiting_for:'decision',wait_kind:'executor_busy',wait_message:'统一执行器正在执行其他角色的操作。'}});
  expect(await screen.findByText('等待决策机会')).toBeTruthy();
  expect(screen.getByText('统一执行器正在执行其他角色的操作。')).toBeTruthy();
  expect(screen.getByText('下次重试时间待调度确认。',{exact:false})).toBeTruthy();
  expect(screen.queryByText('内容已生成，等待执行')).toBeNull();expect(screen.queryByRole('link',{name:'查看待执行操作 ↗'})).toBeNull();
  expect(screen.queryByRole('button',{name:'取消本次决策'})).toBeNull();
 },30000);
 it.each([
  [{enabled:false},'角色已暂停，恢复运行后再安排重试。'],
  [{mode:'review'},'当前为审阅模式，请到操作记录确认执行。'],
 ])('keeps waiting copy accurate when automatic execution is unavailable: %j',async(overrides,explanation)=>{
  setup({...overrides,generation:waiting});
  expect(await screen.findByText(explanation,{exact:false})).toBeTruthy();
  expect(screen.getByRole('status').textContent).not.toMatch(/自动发布|自动执行|释放后自动恢复操作/);
 },30000);
 it.each(['/ai/agents','/ai/agents/maya'])('disables controls without implying AI generation during an ordinary background operation at %s',async route=>{
  setup({busy:true,busy_reason:'operation',generation:{status:'completed'}},route);
  expect((await screen.findAllByText('正在执行操作')).length).toBeGreaterThan(0);
  expect(decisionButton().disabled).toBe(true);expect(decisionButton().classList.contains('ant-btn-loading')).toBe(false);
  expect((screen.getByRole('button',{name:'只阅读'}) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByRole('button',{name:'取消本次决策'})).toBeNull();
  expect(screen.queryByText('等待旧任务释放')).toBeNull();
 },30000);
 it('continues to show actual failures as errors and only displays the current failure',async()=>{
  setup({error:'上一轮的历史错误。',generation:{status:'failed',error:'本轮 AI 连接失败。'}});
  expect(await screen.findByText('本轮决策失败')).toBeTruthy();
  expect(screen.getByRole('alert').classList.contains('ant-alert-error')).toBe(true);
  expect(screen.getAllByText('本轮 AI 连接失败。')).toHaveLength(1);expect(screen.queryByText('上一轮的历史错误。')).toBeNull();
  expect(screen.queryByText('内容已生成，等待执行')).toBeNull();
 },30000);
 it('preserves the normal response progress and cancellation after twenty seconds',async()=>{
  setup({busy:true,busy_reason:'generation',generation:{status:'running',stage:'body',elapsed_seconds:20}});
  expect(await screen.findByText('已建立响应，等待完整内容。 已等待 20 秒。')).toBeTruthy();
  expect(decisionButton().classList.contains('ant-btn-loading')).toBe(true);expect(decisionButton().disabled).toBe(true);
  expect(screen.getByRole('button',{name:'取消本次决策'})).toBeTruthy();expect(screen.queryByText('本轮决策失败')).toBeNull();
 },30000);
 it('removes the wait after a confirmed retry completes',async()=>{
  const {client,state}=setup({generation:waiting});
  await screen.findByText('内容已生成，等待执行');
  await act(async()=>{client.setQueryData(['/agents',{}],{...state,agents:[{...role,error:'',generation:{id:waiting.id,status:'completed',stage:'complete'}}]});});
  await waitFor(()=>expect(screen.queryByText('内容已生成，等待执行')).toBeNull());
  expect(screen.queryByText('本轮决策失败')).toBeNull();expect(screen.queryByText(waiting.wait_message)).toBeNull();
  expect(decisionButton().disabled).toBe(false);expect(decisionButton().classList.contains('ant-btn-loading')).toBe(false);
 },30000);
});
