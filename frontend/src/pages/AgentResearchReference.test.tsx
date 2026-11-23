// @vitest-environment jsdom
import {afterEach,beforeAll,describe,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter} from 'react-router-dom';
import AgentResearchReference, {isResearch} from './AgentResearchReference';

beforeAll(()=>{
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
const brief={brief_id:'brief-1',generated_at:'2026-10-20T03:00:00Z',expires_at:'2099-10-20T04:00:00Z',provider_id:'p',model:'writing-model',scope:{},instruction:'结合具体讨论选择简洁表达。',report:'研究报告建议应保留具体来源。',summary:'包含来源的研究摘要。',sources:[{id:'post:1'}],active:true,expired:false,can_activate:true,citation_status:'valid'};
const snapshot={kind:'content_research',revision:2,active_brief_id:'brief-1',briefs:[brief],references:[
 {agent_id:'maya',brief_id:'brief-1',provided_at:'2026-10-20T04:00:00Z',status:'provided',call_outcome:'succeeded',draft_id:'draft-1'},
 {agent_id:'maya',brief_id:'brief-old',provided_at:'2026-10-20T03:00:00Z',status:'provided',call_outcome:'failed'},
 {agent_id:'ethan',brief_id:'other-agent-private-history',provided_at:'2026-10-20T04:00:00Z',status:'provided'},
]};
function setup(data:unknown=snapshot,status=200){
 const fetch=vi.fn((_input:RequestInfo|URL,_init?:RequestInit)=>Promise.resolve(new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}})));
 vi.stubGlobal('fetch',fetch);
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 render(<QueryClientProvider client={client}><MemoryRouter><AgentResearchReference agentId="maya"/></MemoryRouter></QueryClientProvider>);
 return fetch;
}
describe('agent research reference evidence',()=>{
 it('loads only when expanded and distinguishes supplied references from adoption',async()=>{
  const fetch=setup();expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('研究参考与提供记录'));
  expect(await screen.findByText('包含来源的研究摘要。')).toBeTruthy();
  expect(screen.getByText('研究报告建议应保留具体来源。')).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(1);expect(fetch.mock.calls[0][0]).toBe('/api/ai/research');
  expect(screen.getByText('模型调用成功')).toBeTruthy();expect(screen.getByText('模型调用失败')).toBeTruthy();
  expect(screen.getByText('历史记录未保存失败原因')).toBeTruthy();
  expect(screen.getAllByText('已提供参考')).toHaveLength(2);
  expect(screen.queryByText('other-agent-private-history')).toBeNull();
  expect(screen.queryByText('已采纳')).toBeNull();
  expect(screen.getByRole('link',{name:'查看关联操作 ↗'}).getAttribute('href')).toBe('/ai/agents/maya?tab=drafts&draft=draft-1');
  expect(screen.getByRole('link',{name:'查看与维护内容研究 ↗'}).getAttribute('href')).toBe('/ai/analysis?ai_view=research');
  expect(screen.queryByRole('button',{name:'启用参考'})).toBeNull();
 });
 it('labels a reasoning-only failure',async()=>{
  setup({...snapshot,references:snapshot.references.map(reference=>reference.call_outcome==='failed'
   ?{...reference,error:'AI 服务只返回了推理过程，尚无最终回答',error_code:'reasoning_only',diagnostics:{finish_reason:'stop'}}:reference)});
  fireEvent.click(screen.getByText('研究参考与提供记录'));
  expect(await screen.findByText('只返回推理、没有正文')).toBeTruthy();expect(screen.queryByText('输出被截断')).toBeNull();
 });
 it('shows saved failure details below the failed call without implying adoption',async()=>{
  const error='<img src=x onerror="alert(1)">模型输出达到服务商上限，最终回答未完成。';
  setup({...snapshot,references:snapshot.references.map(reference=>reference.call_outcome==='failed'
   ?{...reference,error,error_code:'output_truncated',diagnostics:{finish_reason:'length',model:'actual-model'}}
   :{...reference,error:'成功记录不应展示错误'})});
  fireEvent.click(screen.getByText('研究参考与提供记录'));
  expect(await screen.findByText(error)).toBeTruthy();expect(screen.getByText('输出被截断')).toBeTruthy();
  expect(screen.queryByText('历史记录未保存失败原因')).toBeNull();expect(screen.queryByText('成功记录不应展示错误')).toBeNull();
  expect(document.querySelector('.agent-research-reference-list img')).toBeNull();expect(screen.queryByText('已采纳')).toBeNull();
 });
 it('accepts old reference records and rejects invalid optional failure fields',()=>{
  const reference=snapshot.references[1];
  expect(isResearch(snapshot)).toBe(true);
  expect(isResearch({...snapshot,references:[{...reference,error:'已脱敏的失败说明',error_code:'output_truncated',diagnostics:{finish_reason:'length'}}]})).toBe(true);
  for(const invalid of [{error:42},{error:null},{error_code:[]},{error_code:4},{diagnostics:[]},{diagnostics:'length'},{diagnostics:null}]){
   expect(isResearch({...snapshot,references:[{...reference,...invalid}]})).toBe(false);
  }
 });
 it('replaces obsolete Grok budget advice only in historical truncation display',async()=>{
  const legacy='AI 输出预算已耗尽，最终回答未完成；此 Grok 模型不能关闭思考，可调整输出预算';
  setup({...snapshot,references:snapshot.references.map(reference=>reference.call_outcome==='failed'
   ?{...reference,error:legacy,error_code:'output_truncated'}:reference)});
  fireEvent.click(screen.getByText('研究参考与提供记录'));
  expect(await screen.findByText('当次模型回答因输出长度限制被截断，未获得完整结果。')).toBeTruthy();
  expect(screen.getByText('历史版本错误；当前已取消本地输出上限。')).toBeTruthy();expect(screen.queryByText(legacy)).toBeNull();
 });
 it('rejects a generic state mock instead of treating it as an empty valid research response',async()=>{
  setup({agents:[],settings:{enabled:false},briefs:[brief]});
  fireEvent.click(screen.getByText('研究参考与提供记录'));
  expect(await screen.findByText('研究参考数据暂不可用')).toBeTruthy();
  expect(screen.queryByText('包含来源的研究摘要。')).toBeNull();
  expect(screen.queryByText('尚未启用研究参考。')).toBeNull();
 });
 it('shows endpoint errors without claiming there is no reference',async()=>{
  setup({error:'研究读取暂时失败'},503);fireEvent.click(screen.getByText('研究参考与提供记录'));
  expect(await screen.findByText('研究参考读取失败')).toBeTruthy();
  expect(screen.getByText('研究读取暂时失败')).toBeTruthy();
  expect(screen.queryByText('尚未启用研究参考。')).toBeNull();
 });
 it('does not present an expired selected brief as usable while keeping its provided history',async()=>{
  setup({...snapshot,briefs:[{...brief,expired:true,expires_at:'2020-01-01T00:00:00Z'}]});
  fireEvent.click(screen.getByText('研究参考与提供记录'));
  expect(await screen.findByText('当前启用记录暂不可作为有效参考')).toBeTruthy();
  expect(screen.queryByText('可供决策参考')).toBeNull();
  expect(screen.getAllByText('已提供参考')).toHaveLength(2);
 });
 it('honors backend source availability before showing an active brief as usable',async()=>{
  setup({...snapshot,briefs:[{...brief,can_activate:false,sources_available:false}]});
  fireEvent.click(screen.getByText('研究参考与提供记录'));
  expect(await screen.findByText('当前启用记录暂不可作为有效参考')).toBeTruthy();
  expect(screen.queryByText('可供决策参考')).toBeNull();
 });
});
