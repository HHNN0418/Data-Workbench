// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, configure, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter, useLocation} from 'react-router-dom';
import ContentResearch, {parseResearchState, type ResearchState} from './ContentResearch';
import AIAnalysis from './AIAnalysis';

vi.mock('antd',async importOriginal=>{
 const original=await importOriginal<typeof import('antd')>();
 return {...original,Select:({options,value,onChange,id,disabled,'aria-label':label}:any)=><select id={id} aria-label={label} disabled={disabled} value={value??''} onChange={event=>onChange(event.target.value)}>{(options||[]).map((option:any)=><option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}</select>};
});
beforeAll(()=>{
 configure({asyncUtilTimeout:10000});
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 const originalStyle=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>originalStyle(element));
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn().mockImplementation(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
const provider={id:'writer',name:'已配置写作服务',base_url:'https://ai.example.com/v1',model:'writer-model',protocol:'openai_chat',has_key:true,enabled:true,is_default:true};
function savedState():ResearchState {
 return {kind:'content_research',revision:7,active_brief_id:null,briefs:[{
  brief_id:'brief-1',generated_at:new Date(Date.now()-3600000).toISOString(),expires_at:new Date(Date.now()+3600000).toISOString(),
  provider_id:'writer',model:'writer-model',scope:{own:'own'},instruction:'检查开头',report:'已保存的研究：具体场景开头需要与普通样本对照。',
  sources:[{id:'post:42',post_id:42,title:'证据原帖',text:'已采集的原文'}],summary:{post_sample_count:1},usage:{total_tokens:123},
  citation_status:'valid',citation_errors:[],active:false,expired:false,can_activate:true,sources_available:true
 }],references:[]};
}
function Location(){const location=useLocation();return <output data-testid="research-location">{location.search}</output>;}
function setup({initial=savedState(),configured=true,invalidRead=false,runFailure=false,activationFailure=false,integration=false}:{
 initial?:ResearchState;configured?:boolean;invalidRead?:boolean;runFailure?:boolean;activationFailure?:boolean;integration?:boolean
}={}){
 let data=structuredClone(initial);
 const fetch=vi.fn((input:RequestInfo|URL,options?:RequestInit)=>{
  const path=String(input);
  let payload:unknown=data,status=200;
  if(path==='/api/bootstrap')payload={csrf_token:'test-csrf'};
  else if(path==='/api/ai/providers')payload={providers:configured?[provider,{...provider,id:'embed',protocol:'openai_embeddings'}]:[],default_provider_id:'writer'};
  else if(path==='/api/ai/research'&&invalidRead)payload={providers:[provider]};
  else if(path==='/api/ai/research/active'){
   if(activationFailure){status=409;payload={detail:'研究报告已更新，请刷新'};}
   else{
    const body=JSON.parse(String(options?.body));
    data={...data,revision:data.revision+1,active_brief_id:body.brief_id,briefs:data.briefs.map(brief=>({...brief,active:brief.brief_id===body.brief_id}))};
    payload=data;
   }
  }else if(path==='/api/ai/research/run'){
   if(runFailure){status=502;payload={detail:'模型服务暂时不可用'};}
   else{
    data={...data,revision:data.revision+1,created_brief_id:'brief-2',briefs:[{...savedState().briefs[0],brief_id:'brief-2',report:'新生成的研究报告，等待明确启用。'},...data.briefs]};
    payload=data;
   }
  }
  return Promise.resolve(new Response(JSON.stringify(path==='/api/ai/research/run'&&status===200?{id:'research-test',state:'completed',stage:'已完成',result:payload}:payload),{status:path==='/api/ai/research/run'&&status===200?202:status,headers:{'Content-Type':'application/json'}}));
 });
 vi.stubGlobal('fetch',fetch);
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 client.setQueryData(['/analysis',{own:'own'}],{sentinel:'existing-analysis'});
 const view=render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/analysis?analysis_tab=ai&ai_view=research&own=own']}>{integration?<AIAnalysis params={{ai_view:'research',own:'own'}}/>:<ContentResearch params={{own:'own',author_id:'7',since:'2026-10-01',window_minutes:30,focus_post_id:42}}/>}<Location/></MemoryRouter></QueryClientProvider>);
 return {fetch,client,...view};
}
function posts(fetch:ReturnType<typeof vi.fn>,path:string){return fetch.mock.calls.filter(([url,options])=>url===path&&options?.method==='POST').map(([,options])=>JSON.parse(String(options?.body)));}

describe('persistent content research',()=>{
 it('loads a saved report without starting cloud work or activating it',async()=>{
  const {fetch}=setup();
  expect(await screen.findByText(savedState().briefs[0].report)).toBeTruthy();
  expect(screen.getByText('未启用参考')).toBeTruthy();
  expect(posts(fetch,'/api/ai/research/run')).toEqual([]);
  expect(posts(fetch,'/api/ai/research/active')).toEqual([]);
  expect(posts(fetch,'/api/ai/run')).toEqual([]);
  expect((screen.getByLabelText('研究文字 AI 服务') as HTMLSelectElement).options.length).toBe(2);
  fireEvent.click(screen.getByRole('button',{name:'刷新报告'}));
  await waitFor(()=>expect(fetch.mock.calls.filter(([url])=>url==='/api/ai/research').length).toBeGreaterThan(1));
  expect(posts(fetch,'/api/ai/research/run')).toEqual([]);
  fireEvent.click(screen.getByText('核对引用资料（1 条）'));
  expect((await screen.findByRole('link',{name:'原帖 #42 ↗'})).getAttribute('href')).toBe('/posts?post=42');
 },30000);
 it('requires an explicit activation with the current revision and preserves existing analysis data',async()=>{
  const {fetch,client}=setup();
  fireEvent.click(await screen.findByRole('button',{name:'供发文 Agent 参考'}));
  expect(await screen.findByRole('button',{name:'停用参考'})).toBeTruthy();
  expect(posts(fetch,'/api/ai/research/active')).toEqual([{brief_id:'brief-1',expected_revision:7}]);
  const options=fetch.mock.calls.find(([url])=>url==='/api/ai/research/active')?.[1];
  expect(options?.headers).toMatchObject({'X-CTN-Token':'test-csrf'});
  expect(client.getQueryState(['/analysis',{own:'own'}])?.isInvalidated).toBe(false);
  fireEvent.click(screen.getByRole('button',{name:'停用参考'}));
  await screen.findByRole('button',{name:'供发文 Agent 参考'});
  expect(posts(fetch,'/api/ai/research/active').at(-1)).toEqual({brief_id:null,expected_revision:8});
  expect(screen.getByText(savedState().briefs[0].report)).toBeTruthy();
 },30000);
 it('saves a manual run with supported scope and keeps new reports inactive',async()=>{
  const {fetch}=setup();await screen.findByText(savedState().briefs[0].report);
  fireEvent.change(screen.getByLabelText('内容研究重点'),{target:{value:'同时找出反例'}});
  fireEvent.click(screen.getByRole('button',{name:'开始研究并保存'}));
  expect(await screen.findByText('新生成的研究报告，等待明确启用。')).toBeTruthy();
  expect(posts(fetch,'/api/ai/research/run')).toEqual([{provider_id:'writer',params:{author_id:'7',own:'own',since:'2026-10-01'},mode:'popular',instruction:'同时找出反例',sample_size:100,include_news:true}]);
  expect(posts(fetch,'/api/ai/research/active')).toEqual([]);
  expect(screen.getByText('未启用参考')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('已保存报告'),{target:{value:'brief-1'}});
  expect(screen.getByText(savedState().briefs[0].report)).toBeTruthy();
 },30000);
 it('keeps the saved report visible after a failed model run',async()=>{
  const {fetch}=setup({runFailure:true});await screen.findByText(savedState().briefs[0].report);
  fireEvent.click(screen.getByRole('button',{name:'开始研究并保存'}));
  expect(await screen.findByText('模型服务暂时不可用')).toBeTruthy();
  expect(screen.getByText(savedState().briefs[0].report)).toBeTruthy();
  expect(posts(fetch,'/api/ai/research/active')).toEqual([]);
 },30000);
 it('does not impose or send a local model output budget',async()=>{
  const {fetch}=setup();await screen.findByText(savedState().briefs[0].report);
  expect(screen.queryByRole('spinbutton',{name:'本次输出上限'})).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'开始研究并保存'}));
  await screen.findByText('新生成的研究报告，等待明确启用。');
  expect(posts(fetch,'/api/ai/research/run')[0]).not.toHaveProperty('max_output_tokens');
  expect(posts(fetch,'/api/ai/providers/save')).toEqual([]);
 },30000);
 it('does not enable expired reports even before the next server refresh',async()=>{
  const initial=savedState();initial.briefs[0].expires_at=new Date(Date.now()-1000).toISOString();
  setup({initial});await screen.findByText(initial.briefs[0].report);
  expect(screen.getByText('已到期')).toBeTruthy();
  expect((screen.getByRole('button',{name:'供发文 Agent 参考'}) as HTMLButtonElement).disabled).toBe(true);
 },30000);
 it('retains the report and asks for refresh on an activation revision conflict',async()=>{
  const {fetch}=setup({activationFailure:true});
  fireEvent.click(await screen.findByRole('button',{name:'供发文 Agent 参考'}));
  expect(await screen.findByText(/研究报告已更新，请刷新.*请刷新报告后再试/)).toBeTruthy();
  expect(screen.getByText(savedState().briefs[0].report)).toBeTruthy();
  expect(screen.getByText('未启用参考')).toBeTruthy();
  expect(posts(fetch,'/api/ai/research/active')).toHaveLength(1);
 },30000);
 it('rejects unrelated API payloads instead of showing them as research state',async()=>{
  setup({invalidRead:true});
  expect(await screen.findByText('内容研究数据格式不正确，请刷新后重试。')).toBeTruthy();
  expect(screen.queryByText('尚未生成研究报告')).toBeNull();
  expect((screen.getByRole('button',{name:'开始研究并保存'}) as HTMLButtonElement).disabled).toBe(true);
  expect(()=>parseResearchState({providers:[provider]})).toThrow();
 },30000);
 it('renders report text safely and labels provided records without implying adoption',async()=>{
  const initial=savedState();initial.briefs[0].report='<img src=x onerror="alert(1)">保留为文字';
  initial.references=[{agent_id:'agent-a',brief_id:'brief-1',provided_at:new Date().toISOString(),status:'provided',call_outcome:'failed'}];
  const {container}=setup({initial});
  expect(await screen.findByText(initial.briefs[0].report)).toBeTruthy();
  expect(container.querySelector('.research-report-body img')).toBeNull();
  expect(screen.getByText('参考提供记录')).toBeTruthy();expect(screen.getByText('已提供参考')).toBeTruthy();expect(screen.getByText('调用失败')).toBeTruthy();
  expect(screen.getByRole('columnheader',{name:'失败原因'})).toBeTruthy();expect(screen.getByText('历史记录未保存失败原因')).toBeTruthy();
  expect(screen.queryByText('已采用')).toBeNull();
 },30000);
 it('labels a reasoning-only failure',async()=>{
  const initial=savedState();
  initial.references=[{agent_id:'agent-failed',brief_id:'brief-1',provided_at:new Date().toISOString(),status:'provided',call_outcome:'failed',error:'AI 服务只返回了推理过程，尚无最终回答',error_code:'reasoning_only',diagnostics:{finish_reason:'stop'}}];
  setup({initial});
  expect(await screen.findByText('只返回推理、没有正文')).toBeTruthy();expect(screen.queryByText('输出被截断')).toBeNull();
 },30000);
 it('shows the saved failure reason as text only for failed calls',async()=>{
  const initial=savedState(),error='<img src=x onerror="alert(1)">模型输出达到服务商上限，最终回答未完成。';
  initial.references=[
   {agent_id:'agent-failed',brief_id:'brief-1',provided_at:new Date().toISOString(),status:'provided',call_outcome:'failed',error,error_code:'output_truncated',diagnostics:{finish_reason:'length',model:'actual-model'}},
   {agent_id:'agent-ok',brief_id:'brief-1',provided_at:new Date().toISOString(),status:'provided',call_outcome:'succeeded',error:'成功记录不应展示错误'},
  ];
  const {container}=setup({initial});
  expect(await screen.findByText(error)).toBeTruthy();expect(screen.getByText('输出被截断')).toBeTruthy();
  expect(screen.queryByText('成功记录不应展示错误')).toBeNull();expect(screen.getByText('调用成功')).toBeTruthy();
  expect(container.querySelector('.research-references img')).toBeNull();expect(screen.queryByText('已采用')).toBeNull();
 },30000);
 it('accepts old reference records and validates optional failure fields',()=>{
  const initial=savedState(),reference={agent_id:'agent-a',brief_id:'brief-1',provided_at:new Date().toISOString(),status:'provided',call_outcome:'failed'};
  expect(()=>parseResearchState({...initial,references:[reference]})).not.toThrow();
  expect(()=>parseResearchState({...initial,references:[{...reference,error:'已脱敏的失败说明',error_code:'output_truncated',diagnostics:{finish_reason:'length'}}]})).not.toThrow();
  for(const invalid of [{error:42},{error:null},{error_code:[]},{error_code:4},{diagnostics:[]},{diagnostics:'length'},{diagnostics:null}]){
   expect(()=>parseResearchState({...initial,references:[{...reference,...invalid}]})).toThrow('参考提供记录格式不正确');
  }
 });
 it('explains historical truncation without showing obsolete configuration advice',async()=>{
  const initial=savedState(),legacy='AI 输出预算已耗尽，最终回答未完成；请在高级设置提高输出上限或关闭思考模式';
  initial.references=[
   {agent_id:'agent-old',brief_id:'brief-1',provided_at:new Date().toISOString(),status:'provided',call_outcome:'failed',error:legacy,error_code:'output_truncated'},
   {agent_id:'agent-other',brief_id:'brief-1',provided_at:new Date().toISOString(),status:'provided',call_outcome:'failed',error:`其他错误：${legacy}`,error_code:'service_error'},
  ];
  setup({initial});
  expect(await screen.findByText('当次模型回答因输出长度限制被截断，未获得完整结果。')).toBeTruthy();
  expect(screen.getByText('历史版本错误；当前已取消本地输出上限。')).toBeTruthy();
  expect(screen.queryByText(legacy)).toBeNull();expect(screen.getByText(`其他错误：${legacy}`)).toBeTruthy();
 },30000);
 it('shows an honest empty state and disables generation without configured text models',async()=>{
  const initial=savedState();initial.briefs=[];
  const {fetch}=setup({initial,configured:false});
  expect(await screen.findByText('尚未生成研究报告')).toBeTruthy();
  expect((screen.getByRole('button',{name:'开始研究并保存'}) as HTMLButtonElement).disabled).toBe(true);
  expect(posts(fetch,'/api/ai/research/run')).toEqual([]);
 },30000);
 it('opens the research URL without duplicate service pickers and synchronizes tab URLs',async()=>{
  setup({integration:true});await screen.findByText(savedState().briefs[0].report);
  expect(screen.getByRole('tab',{name:'内容研究'}).getAttribute('aria-selected')).toBe('true');
  expect(screen.queryByLabelText('当前 AI 服务')).toBeNull();
  fireEvent.click(screen.getByRole('tab',{name:'资料聊天'}));
  await waitFor(()=>expect(screen.getByTestId('research-location').textContent).toContain('ai_view=chat'));
  expect(screen.getByLabelText('当前 AI 服务')).toBeTruthy();
  fireEvent.click(screen.getByRole('tab',{name:'内容研究'}));
  await waitFor(()=>expect(screen.getByTestId('research-location').textContent).toContain('ai_view=research'));
 },30000);
});
